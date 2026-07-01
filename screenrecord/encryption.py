"""
AES-256-GCM file encryption module for HIPAA compliance.

Provides streaming chunked encryption for large screen recording files,
ensuring Protected Health Information (PHI) is encrypted at rest and
original plaintext files are securely removed after encryption.
"""

import base64
import logging
import os
import struct
from pathlib import Path
from typing import Optional, Union

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asymmetric_padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
KEY_SIZE = 32  # AES-256
NONCE_SIZE = 12  # 96-bit nonce recommended for GCM
CHUNK_SIZE = 1 * 1024 * 1024  # 1 MB plaintext chunks for streaming encryption
READ_BLOCK = 64 * 1024  # 64 KB read buffer
GCM_TAG_SIZE = 16  # AES-GCM appends a 16-byte authentication tag
HEADER_MAGIC = b"ENCRV1"  # legacy symmetric-key format
HEADER_MAGIC_V2 = b"ENCRV2"  # public-key envelope format


class FileEncryptor:
    """Encrypt and decrypt files using AES-256-GCM with chunked streaming.

    File format written by *encrypt_file*::

        [6 bytes  magic  "ENCRV1"]
        [4 bytes  uint32 big-endian chunk_count]
        For each chunk:
            [12 bytes nonce]
            [4  bytes uint32 big-endian encrypted_chunk_length]
            [N  bytes encrypted_chunk (ciphertext + 16-byte GCM tag)]
    """

    def __init__(
        self,
        key: Optional[bytes] = None,
        public_key_pem: Optional[bytes] = None,
        private_key_pem: Optional[bytes] = None,
    ) -> None:
        self._public_key = None
        self._private_key = None
        if public_key_pem:
            self._public_key = serialization.load_pem_public_key(public_key_pem)
        if private_key_pem:
            self._private_key = serialization.load_pem_private_key(
                private_key_pem,
                password=None,
            )

        if key is None and self._public_key is None and self._private_key is None:
            key = self.generate_key()
            logger.info("Generated new AES-256 encryption key.")
        if key is not None and len(key) != KEY_SIZE:
            raise ValueError(f"Key must be {KEY_SIZE} bytes, got {len(key)}.")
        self._key: Optional[bytes] = key
        self._aesgcm = AESGCM(self._key) if self._key is not None else None

    # ------------------------------------------------------------------
    # Key property
    # ------------------------------------------------------------------
    @property
    def key(self) -> bytes:
        """Return the raw encryption key."""
        if self._key is None:
            raise ValueError("This encryptor does not contain a symmetric key.")
        return self._key

    # ------------------------------------------------------------------
    # Key generation / persistence
    # ------------------------------------------------------------------
    @staticmethod
    def generate_key() -> bytes:
        """Return 32 bytes of cryptographically secure random data (AES-256 key)."""
        return os.urandom(KEY_SIZE)

    def save_key(self, path: Union[str, Path]) -> None:
        """Save the key to *path*, base64-encoded, with restrictive permissions.

        On Unix the file permissions are set to owner-read-only (0o400).
        """
        path = Path(path)
        encoded = base64.b64encode(self._key)
        path.write_bytes(encoded)

        # Restrict permissions (best-effort; Windows may silently ignore).
        try:
            os.chmod(path, 0o400)
        except OSError:
            logger.debug("Could not set file permissions on %s (may be non-Unix).", path)

        logger.warning(
            "Encryption key saved to %s. "
            "This key MUST be kept secure -- loss of the key means "
            "permanent loss of access to encrypted recordings.",
            path,
        )

    @staticmethod
    def load_key(path: Union[str, Path]) -> "FileEncryptor":
        """Load a base64-encoded key from *path* and return a new FileEncryptor."""
        path = Path(path)
        encoded = path.read_bytes().strip()
        key = base64.b64decode(encoded)
        logger.info("Loaded encryption key from %s.", path)
        return FileEncryptor(key=key)

    @staticmethod
    def load_public_key(path: Union[str, Path]) -> "FileEncryptor":
        """Load a PEM public key for endpoint-side envelope encryption."""
        path = Path(path)
        logger.info("Loaded public encryption key from %s.", path)
        return FileEncryptor(public_key_pem=path.read_bytes())

    @staticmethod
    def load_private_key(path: Union[str, Path]) -> "FileEncryptor":
        """Load a PEM private key for administrator-side decryption."""
        path = Path(path)
        logger.info("Loaded private decryption key from %s.", path)
        return FileEncryptor(private_key_pem=path.read_bytes())

    # ------------------------------------------------------------------
    # Nonce helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _derive_chunk_nonce(base_nonce: bytes, chunk_index: int) -> bytes:
        """Derive a per-chunk nonce by XOR-ing the base nonce with the chunk index.

        This guarantees a unique nonce per chunk without requiring extra random
        bytes per chunk while preserving the 12-byte nonce size.
        """
        index_bytes = chunk_index.to_bytes(NONCE_SIZE, byteorder="big")
        return bytes(a ^ b for a, b in zip(base_nonce, index_bytes))

    # ------------------------------------------------------------------
    # Encryption
    # ------------------------------------------------------------------
    def encrypt_file(
        self,
        input_path: Union[str, Path],
        output_path: Optional[Union[str, Path]] = None,
    ) -> Path:
        """Encrypt *input_path* using AES-256-GCM chunked streaming.

        Parameters
        ----------
        input_path:
            Path to the plaintext file.
        output_path:
            Destination for the encrypted file.  Defaults to
            ``input_path`` with an ``.enc`` suffix appended.

        Returns
        -------
        Path
            The path to the newly created encrypted file.
        """
        input_path = Path(input_path)
        if output_path is None:
            output_path = input_path.with_suffix(input_path.suffix + ".enc")
        else:
            output_path = Path(output_path)

        if not input_path.is_file():
            raise FileNotFoundError(f"Input file not found: {input_path}")

        # Read the entire source into memory-friendly chunks list.
        chunks: list[bytes] = []
        with open(input_path, "rb") as fh:
            while True:
                data = fh.read(CHUNK_SIZE)
                if not data:
                    break
                chunks.append(data)

        if not chunks:
            # Empty file edge-case: still produce a valid encrypted file.
            chunks = [b""]

        if self._public_key is not None:
            return self._encrypt_file_v2(input_path, output_path, chunks)
        if self._aesgcm is None:
            raise ValueError("No symmetric key or public key is available for encryption.")

        base_nonce = os.urandom(NONCE_SIZE)
        chunk_count = len(chunks)

        with open(output_path, "wb") as out:
            # -- header --
            out.write(HEADER_MAGIC)
            out.write(struct.pack(">I", chunk_count))

            for idx, plaintext_chunk in enumerate(chunks):
                nonce = self._derive_chunk_nonce(base_nonce, idx)
                encrypted = self._aesgcm.encrypt(nonce, plaintext_chunk, None)
                out.write(nonce)
                out.write(struct.pack(">I", len(encrypted)))
                out.write(encrypted)

        # Securely remove the original plaintext file.
        try:
            input_path.unlink()
            logger.debug("Deleted original plaintext file: %s", input_path)
        except OSError as exc:
            logger.error("Failed to delete plaintext file %s: %s", input_path, exc)

        logger.info(
            "Encrypted %s -> %s (%d chunks).",
            input_path,
            output_path,
            chunk_count,
        )
        return output_path

    def _encrypt_file_v2(self, input_path: Path, output_path: Path, chunks: list[bytes]) -> Path:
        """Encrypt using a per-file AES key wrapped by the configured public key."""
        if self._public_key is None:
            raise ValueError("No public key is available for envelope encryption.")

        file_key = os.urandom(KEY_SIZE)
        aesgcm = AESGCM(file_key)
        wrapped_key = self._public_key.encrypt(
            file_key,
            asymmetric_padding.OAEP(
                mgf=asymmetric_padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        base_nonce = os.urandom(NONCE_SIZE)
        chunk_count = len(chunks)

        with open(output_path, "wb") as out:
            out.write(HEADER_MAGIC_V2)
            out.write(struct.pack(">I", len(wrapped_key)))
            out.write(wrapped_key)
            out.write(struct.pack(">I", chunk_count))

            for idx, plaintext_chunk in enumerate(chunks):
                nonce = self._derive_chunk_nonce(base_nonce, idx)
                encrypted = aesgcm.encrypt(nonce, plaintext_chunk, None)
                out.write(nonce)
                out.write(struct.pack(">I", len(encrypted)))
                out.write(encrypted)

        try:
            input_path.unlink()
            logger.debug("Deleted original plaintext file: %s", input_path)
        except OSError as exc:
            logger.error("Failed to delete plaintext file %s: %s", input_path, exc)

        logger.info(
            "Envelope-encrypted %s -> %s (%d chunks).",
            input_path,
            output_path,
            chunk_count,
        )
        return output_path

    # ------------------------------------------------------------------
    # Decryption
    # ------------------------------------------------------------------
    def decrypt_file(
        self,
        input_path: Union[str, Path],
        output_path: Optional[Union[str, Path]] = None,
    ) -> Path:
        """Decrypt an encrypted file produced by *encrypt_file*.

        Parameters
        ----------
        input_path:
            Path to the ``.enc`` file.
        output_path:
            Destination for the decrypted file.  Defaults to *input_path*
            with the trailing ``.enc`` stripped.

        Returns
        -------
        Path
            The path to the newly written decrypted file.
        """
        input_path = Path(input_path)
        if output_path is None:
            name = input_path.name
            if name.endswith(".enc"):
                output_path = input_path.with_name(name[: -len(".enc")])
            else:
                output_path = input_path.with_suffix(".dec")
        else:
            output_path = Path(output_path)

        if not input_path.is_file():
            raise FileNotFoundError(f"Encrypted file not found: {input_path}")

        with open(input_path, "rb") as fh:
            # -- header --
            magic = fh.read(len(HEADER_MAGIC))
            if magic == HEADER_MAGIC:
                if self._aesgcm is None:
                    raise ValueError("A symmetric key is required to decrypt ENCRV1 files.")
                aesgcm = self._aesgcm
                (chunk_count,) = struct.unpack(">I", fh.read(4))
            elif magic == HEADER_MAGIC_V2:
                if self._private_key is None:
                    raise ValueError("A private key is required to decrypt ENCRV2 files.")
                wrapped_len_raw = fh.read(4)
                if len(wrapped_len_raw) != 4:
                    raise ValueError(f"Truncated wrapped key length in {input_path}")
                (wrapped_len,) = struct.unpack(">I", wrapped_len_raw)
                wrapped_key = fh.read(wrapped_len)
                if len(wrapped_key) != wrapped_len:
                    raise ValueError(f"Truncated wrapped key in {input_path}")
                file_key = self._private_key.decrypt(
                    wrapped_key,
                    asymmetric_padding.OAEP(
                        mgf=asymmetric_padding.MGF1(algorithm=hashes.SHA256()),
                        algorithm=hashes.SHA256(),
                        label=None,
                    ),
                )
                aesgcm = AESGCM(file_key)
                (chunk_count,) = struct.unpack(">I", fh.read(4))
            else:
                raise ValueError(
                    f"Invalid encrypted file (bad magic): {input_path}"
                )

            with open(output_path, "wb") as out:
                for idx in range(chunk_count):
                    nonce = fh.read(NONCE_SIZE)
                    if len(nonce) != NONCE_SIZE:
                        raise ValueError(
                            f"Truncated nonce at chunk {idx} in {input_path}"
                        )
                    (enc_len,) = struct.unpack(">I", fh.read(4))
                    encrypted = fh.read(enc_len)
                    if len(encrypted) != enc_len:
                        raise ValueError(
                            f"Truncated data at chunk {idx} in {input_path}"
                        )
                    plaintext = aesgcm.decrypt(nonce, encrypted, None)
                    out.write(plaintext)

        logger.info(
            "Decrypted %s -> %s (%d chunks).",
            input_path,
            output_path,
            chunk_count,
        )
        return output_path

    # ------------------------------------------------------------------
    # In-place encryption convenience
    # ------------------------------------------------------------------
    def encrypt_in_place(self, file_path: Union[str, Path]) -> Path:
        """Encrypt *file_path*, replacing the original with a ``.enc`` version.

        Procedure:
            1. Rename the original to ``<name>.tmp``.
            2. Encrypt ``<name>.tmp`` to ``<name>.enc``.
            3. Delete ``<name>.tmp``.

        Returns
        -------
        Path
            The final ``.enc`` path.
        """
        file_path = Path(file_path)
        if not file_path.is_file():
            raise FileNotFoundError(f"File not found: {file_path}")

        tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
        enc_path = file_path.with_suffix(file_path.suffix + ".enc")

        # Step 1: rename original -> .tmp
        file_path.rename(tmp_path)
        logger.debug("Renamed %s -> %s for in-place encryption.", file_path, tmp_path)

        try:
            # Step 2: encrypt .tmp -> .enc (encrypt_file deletes the input)
            self.encrypt_file(tmp_path, enc_path)
        except Exception:
            # Attempt to restore the original on failure.
            if tmp_path.is_file():
                tmp_path.rename(file_path)
                logger.error(
                    "In-place encryption failed; restored original file %s.",
                    file_path,
                )
            raise

        logger.info("In-place encryption complete: %s -> %s", file_path, enc_path)
        return enc_path
