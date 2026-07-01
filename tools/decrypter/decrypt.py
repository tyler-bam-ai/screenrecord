#!/usr/bin/env python3
"""
Screen Recording Decrypter — Standalone Tool

Decrypts .mp4.enc files produced by the screen recording service.
Files use ENCRV2 public-key envelope encryption for new builds, with legacy
ENCRV1 AES-256-GCM support for older captures.

Setup:
    pip3 install cryptography

Usage:
    # Decrypt a single file
    python3 decrypt.py recording.mp4.enc

    # Decrypt with a specific key file
    python3 decrypt.py --key /path/to/encryption.key recording.mp4.enc

    # Decrypt an entire folder of .enc files
    python3 decrypt.py --input /path/to/encrypted/ --output /path/to/decrypted/

    # Decrypt and delete the encrypted originals
    python3 decrypt.py --input recordings/ --delete-after

    # Provide the key as a base64 string directly
    python3 decrypt.py --key-b64 "b3BkL0dw..." recording.mp4.enc

    # Decrypt new ENCRV2 files with the private key
    python3 decrypt.py --private-key ~/.screenrecord/keys/screenrecord_envelope_private_key.pem recording.mp4.enc
"""

import argparse
import base64
import os
import struct
import sys
import time
from pathlib import Path

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding as asymmetric_padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    print("Missing dependency: cryptography")
    print()
    print("Install it with:")
    print("    pip3 install cryptography")
    print()
    print("Or install all requirements:")
    print("    pip3 install -r requirements.txt")
    sys.exit(1)

# ── Constants ────────────────────────────────────────────────────────────────

HEADER_MAGIC = b"ENCRV1"
HEADER_MAGIC_V2 = b"ENCRV2"
NONCE_SIZE = 12  # 96-bit GCM nonce
CHUNK_HEADER_SIZE = NONCE_SIZE + 4  # nonce + uint32 length

# Default key file locations (searched in order)
DEFAULT_KEY_PATHS = [
    "encryption.key",
    os.path.expanduser("~/.screenrecord/encryption.key"),
]
DEFAULT_PRIVATE_KEY_PATHS = [
    "screenrecord_envelope_private_key.pem",
    os.path.expanduser("~/.screenrecord/keys/screenrecord_envelope_private_key.pem"),
]


# ── Key Loading ──────────────────────────────────────────────────────────────

def load_key_from_file(path: str) -> bytes:
    """Load a base64-encoded AES-256 key from a file."""
    with open(path, "rb") as f:
        raw = f.read().strip()
    key = base64.b64decode(raw)
    if len(key) != 32:
        raise ValueError(f"Key must be 32 bytes (AES-256), got {len(key)}")
    return key


def load_key_from_b64(b64_string: str) -> bytes:
    """Decode a base64 key string."""
    key = base64.b64decode(b64_string)
    if len(key) != 32:
        raise ValueError(f"Key must be 32 bytes (AES-256), got {len(key)}")
    return key


def find_key(key_path: str = None, key_b64: str = None, required: bool = False) -> bytes:
    """Resolve the encryption key from arguments or default locations."""
    if key_b64:
        return load_key_from_b64(key_b64)

    if key_path:
        return load_key_from_file(key_path)

    # Search default locations
    for path in DEFAULT_KEY_PATHS:
        if os.path.isfile(path):
            print(f"  Using key: {path}")
            return load_key_from_file(path)

    if required:
        print("Error: No legacy encryption key found.")
        print()
        print("Provide one with:")
        print("  --key /path/to/encryption.key")
        print("  --key-b64 <base64-encoded-key>")
        print()
        print(f"Or place encryption.key in: {', '.join(DEFAULT_KEY_PATHS)}")
        sys.exit(1)
    return None


def find_private_key(private_key_path: str = None):
    """Resolve the ENCRV2 private key from arguments or default locations."""
    candidates = [private_key_path] if private_key_path else DEFAULT_PRIVATE_KEY_PATHS
    for path in candidates:
        if path and os.path.isfile(path):
            print(f"  Using private key: {path}")
            return serialization.load_pem_private_key(
                Path(path).read_bytes(),
                password=None,
            )
    return None


# ── Decryption ───────────────────────────────────────────────────────────────

def decrypt_file(input_path: Path, output_path: Path, key: bytes = None, private_key=None) -> int:
    """Decrypt a single ENCRV1 or ENCRV2 file.

    Format:
        [6 bytes]  Magic: "ENCRV1"
        [4 bytes]  Chunk count (uint32 big-endian)
        Per chunk:
            [12 bytes] Nonce
            [4 bytes]  Encrypted data length (uint32 big-endian)
            [N bytes]  Encrypted data (ciphertext + 16-byte GCM auth tag)

    Returns total decrypted bytes written.
    """
    with open(input_path, "rb") as fin:
        magic = fin.read(len(HEADER_MAGIC))
        if magic == HEADER_MAGIC:
            if key is None:
                raise ValueError("ENCRV1 file requires --key or --key-b64")
            aesgcm = AESGCM(key)
            chunk_count_raw = fin.read(4)
            if len(chunk_count_raw) < 4:
                raise ValueError("File truncated: missing chunk count")
            chunk_count = struct.unpack(">I", chunk_count_raw)[0]
        elif magic == HEADER_MAGIC_V2:
            if private_key is None:
                raise ValueError("ENCRV2 file requires --private-key")
            wrapped_len_raw = fin.read(4)
            if len(wrapped_len_raw) < 4:
                raise ValueError("File truncated: missing wrapped key length")
            wrapped_len = struct.unpack(">I", wrapped_len_raw)[0]
            wrapped_key = fin.read(wrapped_len)
            if len(wrapped_key) < wrapped_len:
                raise ValueError("File truncated: missing wrapped key")
            file_key = private_key.decrypt(
                wrapped_key,
                asymmetric_padding.OAEP(
                    mgf=asymmetric_padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=None,
                ),
            )
            aesgcm = AESGCM(file_key)
            chunk_count_raw = fin.read(4)
            if len(chunk_count_raw) < 4:
                raise ValueError("File truncated: missing chunk count")
            chunk_count = struct.unpack(">I", chunk_count_raw)[0]
        else:
            raise ValueError(
                f"Not an ENCRV1/ENCRV2 file (header: {magic!r}). "
                f"Is this file actually encrypted?"
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)

        total = 0
        with open(output_path, "wb") as fout:
            for i in range(chunk_count):
                nonce = fin.read(NONCE_SIZE)
                if len(nonce) < NONCE_SIZE:
                    raise ValueError(f"Chunk {i}/{chunk_count}: truncated nonce")

                len_raw = fin.read(4)
                if len(len_raw) < 4:
                    raise ValueError(f"Chunk {i}/{chunk_count}: truncated length")
                enc_len = struct.unpack(">I", len_raw)[0]

                enc_data = fin.read(enc_len)
                if len(enc_data) < enc_len:
                    raise ValueError(
                        f"Chunk {i}/{chunk_count}: truncated data "
                        f"(expected {enc_len}, got {len(enc_data)})"
                    )

                try:
                    plaintext = aesgcm.decrypt(nonce, enc_data, None)
                except Exception:
                    raise ValueError(
                        f"Chunk {i}/{chunk_count}: decryption failed "
                        f"(wrong key or corrupted data)"
                    )

                fout.write(plaintext)
                total += len(plaintext)

    return total


# ── File Discovery ───────────────────────────────────────────────────────────

def find_enc_files(path: Path) -> list:
    """Recursively find all .enc files under a directory."""
    results = []
    for root, _dirs, files in os.walk(path):
        for name in sorted(files):
            if name.endswith(".enc"):
                results.append(Path(root) / name)
    return results


def output_path_for(enc_file: Path, input_base: Path, output_dir: Path) -> Path:
    """Derive the decrypted output path, stripping the .enc extension."""
    rel = enc_file.relative_to(input_base)
    return output_dir / rel.with_suffix("")


# ── Formatting ───────────────────────────────────────────────────────────────

def fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


def fmt_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s"


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Decrypt screen recording .mp4.enc files (ENCRV2 envelope / legacy ENCRV1)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  %(prog)s recording.mp4.enc
  %(prog)s --key encryption.key recording.mp4.enc
  %(prog)s --input encrypted/ --output decrypted/
  %(prog)s --key-b64 "b3BkL0dw..." --input recordings/
  %(prog)s --private-key ~/.screenrecord/keys/screenrecord_envelope_private_key.pem --input recordings/
  %(prog)s --input recordings/ --delete-after
""",
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="One or more .enc files to decrypt",
    )
    parser.add_argument(
        "--key",
        metavar="FILE",
        help="Path to encryption key file (base64-encoded 32-byte key)",
    )
    parser.add_argument(
        "--key-b64",
        metavar="STRING",
        help="Encryption key as a base64 string",
    )
    parser.add_argument(
        "--private-key",
        metavar="FILE",
        help="Private key PEM for ENCRV2 files",
    )
    parser.add_argument(
        "--input", "-i",
        metavar="DIR",
        help="Directory containing .enc files to decrypt",
    )
    parser.add_argument(
        "--output", "-o",
        metavar="DIR",
        default="decrypted",
        help="Output directory (default: decrypted/)",
    )
    parser.add_argument(
        "--delete-after",
        action="store_true",
        help="Delete .enc files after successful decryption",
    )

    args = parser.parse_args()

    # Must provide either positional files or --input
    if not args.files and not args.input:
        parser.print_help()
        print()
        print("Error: Provide .enc files as arguments or use --input DIR")
        sys.exit(1)

    # Load decrypt material. ENCRV1 needs the legacy symmetric key; ENCRV2
    # needs the private key. Supplying both lets one batch process mixed files.
    key = find_key(args.key, args.key_b64, required=False)
    private_key = find_private_key(args.private_key)

    # Build file list
    enc_files = []
    input_base = Path(".")

    if args.input:
        input_path = Path(args.input)
        if not input_path.exists():
            print(f"Error: Input path does not exist: {input_path}")
            sys.exit(1)
        if input_path.is_dir():
            enc_files = find_enc_files(input_path)
            input_base = input_path
        elif input_path.is_file():
            enc_files = [input_path]
            input_base = input_path.parent
    else:
        for f in args.files:
            p = Path(f)
            if not p.exists():
                print(f"Warning: File not found, skipping: {f}")
                continue
            if p.is_dir():
                enc_files.extend(find_enc_files(p))
            else:
                enc_files.append(p)
        input_base = Path(".")

    if not enc_files:
        print("No .enc files found.")
        sys.exit(0)

    output_dir = Path(args.output)

    print()
    print(f"  Decrypting {len(enc_files)} file(s) -> {output_dir}/")
    print()

    # Process files
    ok_count = 0
    fail_count = 0
    total_bytes = 0
    start_time = time.time()

    for enc_file in enc_files:
        out_path = output_path_for(enc_file, input_base, output_dir)
        enc_size = enc_file.stat().st_size
        label = enc_file.name

        sys.stdout.write(f"  {label} ({fmt_size(enc_size)}) ... ")
        sys.stdout.flush()

        try:
            dec_size = decrypt_file(enc_file, out_path, key=key, private_key=private_key)
            total_bytes += dec_size
            ok_count += 1
            print(f"OK -> {out_path.name} ({fmt_size(dec_size)})")

            if args.delete_after:
                enc_file.unlink()

        except ValueError as e:
            fail_count += 1
            print(f"FAILED: {e}")
            if out_path.exists():
                out_path.unlink()
        except Exception as e:
            fail_count += 1
            print(f"FAILED: {e}")
            if out_path.exists():
                out_path.unlink()

    elapsed = time.time() - start_time

    # Summary
    print()
    print("  " + "-" * 48)
    print(f"  Done: {ok_count} decrypted, {fail_count} failed")
    print(f"  Total: {fmt_size(total_bytes)} in {fmt_duration(elapsed)}")
    if ok_count > 0:
        print(f"  Output: {output_dir.resolve()}")
    if args.delete_after and ok_count > 0:
        print(f"  Deleted {ok_count} encrypted source file(s)")
    print()

    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
