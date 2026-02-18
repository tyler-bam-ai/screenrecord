#!/usr/bin/env python3
"""
Decryption tool for Screen Recording files.
Run this on the Mac mini to decrypt downloaded recordings from Google Drive.

Usage:
    python3 decrypt_tool.py --key encryption.key --input encrypted_dir/ --output decrypted_dir/
    python3 decrypt_tool.py --key encryption.key --input single_file.mp4.enc
"""

import argparse
import base64
import os
import struct
import sys
from pathlib import Path

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    print("Error: 'cryptography' package is required.")
    print("Install it with: pip3 install cryptography")
    sys.exit(1)


def load_key(key_path: str) -> bytes:
    """Load a base64-encoded AES-256 encryption key from a file."""
    try:
        with open(key_path, "rb") as f:
            key_data = f.read().strip()
        key = base64.b64decode(key_data)
        if len(key) != 32:
            print(f"Error: Key must be 32 bytes (AES-256). Got {len(key)} bytes.")
            sys.exit(1)
        return key
    except FileNotFoundError:
        print(f"Error: Key file not found: {key_path}")
        sys.exit(1)
    except Exception as e:
        print(f"Error loading key: {e}")
        sys.exit(1)


HEADER_MAGIC = b"ENCRV1"  # Must match encryption.py


def decrypt_file(input_path: Path, output_path: Path, aesgcm: AESGCM) -> int:
    """
    Decrypt a single .enc file.

    File format:
        [6 bytes: magic "ENCRV1"]
        [4 bytes: chunk count as big-endian uint32]
        [for each chunk:
            12 bytes nonce
            4 bytes encrypted_length as big-endian uint32
            encrypted_data (encrypted_length bytes)
        ]

    Returns the size of the decrypted output in bytes.
    """
    with open(input_path, "rb") as fin:
        # Read and validate magic header
        magic = fin.read(len(HEADER_MAGIC))
        if magic != HEADER_MAGIC:
            raise ValueError(
                f"Invalid encrypted file (bad magic header). "
                f"Expected {HEADER_MAGIC!r}, got {magic!r}"
            )

        # Read chunk count
        header = fin.read(4)
        if len(header) < 4:
            raise ValueError("File too short - missing chunk count header")
        chunk_count = struct.unpack(">I", header)[0]

        # Ensure output directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        total_written = 0
        with open(output_path, "wb") as fout:
            for i in range(chunk_count):
                # Read 12-byte nonce
                nonce = fin.read(12)
                if len(nonce) < 12:
                    raise ValueError(f"Chunk {i}: truncated nonce (got {len(nonce)} bytes)")

                # Read 4-byte encrypted data length
                len_bytes = fin.read(4)
                if len(len_bytes) < 4:
                    raise ValueError(f"Chunk {i}: truncated length field")
                encrypted_length = struct.unpack(">I", len_bytes)[0]

                # Read encrypted data
                encrypted_data = fin.read(encrypted_length)
                if len(encrypted_data) < encrypted_length:
                    raise ValueError(
                        f"Chunk {i}: truncated data (expected {encrypted_length}, "
                        f"got {len(encrypted_data)} bytes)"
                    )

                # Decrypt
                try:
                    plaintext = aesgcm.decrypt(nonce, encrypted_data, None)
                except Exception:
                    raise ValueError(
                        f"Chunk {i}: decryption failed (wrong key or corrupted data)"
                    )

                fout.write(plaintext)
                total_written += len(plaintext)

    return total_written


def find_enc_files(input_path: Path) -> list:
    """Recursively find all .enc files in a directory."""
    enc_files = []
    for root, _dirs, files in os.walk(input_path):
        for filename in sorted(files):
            if filename.endswith(".enc"):
                enc_files.append(Path(root) / filename)
    return enc_files


def derive_output_path(enc_file: Path, input_base: Path, output_dir: Path) -> Path:
    """
    Derive the output path for a decrypted file, preserving directory structure.
    Strips the .enc extension from the filename.
    """
    relative = enc_file.relative_to(input_base)
    # Remove the .enc extension
    out_name = relative.with_suffix("")
    return output_dir / out_name


def format_size(size_bytes: int) -> str:
    """Format a byte count as a human-readable size string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def main():
    parser = argparse.ArgumentParser(
        description="Decrypt screen recording files encrypted with AES-256-GCM.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python3 decrypt_tool.py --key encryption.key --input encrypted_dir/ --output decrypted_dir/
    python3 decrypt_tool.py --key encryption.key --input single_file.mp4.enc
    python3 decrypt_tool.py --key encryption.key --input recordings/ --delete-encrypted
        """,
    )
    parser.add_argument(
        "--key",
        required=True,
        help="Path to the encryption key file (base64-encoded 32-byte AES-256 key)",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to a single .enc file or a directory containing .enc files",
    )
    parser.add_argument(
        "--output",
        default="decrypted/",
        help="Output directory for decrypted files (default: decrypted/)",
    )
    parser.add_argument(
        "--delete-encrypted",
        action="store_true",
        help="Delete .enc files after successful decryption",
    )

    args = parser.parse_args()

    # Load encryption key
    key = load_key(args.key)
    aesgcm = AESGCM(key)

    input_path = Path(args.input)
    output_dir = Path(args.output)

    if not input_path.exists():
        print(f"Error: Input path does not exist: {input_path}")
        sys.exit(1)

    # Build list of files to decrypt
    if input_path.is_file():
        if not input_path.name.endswith(".enc"):
            print(f"Warning: Input file does not have .enc extension: {input_path.name}")
        enc_files = [input_path]
        input_base = input_path.parent
    elif input_path.is_dir():
        enc_files = find_enc_files(input_path)
        input_base = input_path
        if not enc_files:
            print(f"No .enc files found in: {input_path}")
            sys.exit(0)
    else:
        print(f"Error: Input path is not a file or directory: {input_path}")
        sys.exit(1)

    print(f"Found {len(enc_files)} encrypted file(s)")
    print()

    # Decrypt each file
    success_count = 0
    fail_count = 0
    total_bytes = 0
    deleted_files = []

    for enc_file in enc_files:
        out_path = derive_output_path(enc_file, input_base, output_dir)
        enc_size = enc_file.stat().st_size

        print(
            f"Decrypting: {enc_file.name} -> {out_path.name} ({format_size(enc_size)})",
            end="",
            flush=True,
        )

        try:
            decrypted_size = decrypt_file(enc_file, out_path, aesgcm)
            total_bytes += decrypted_size
            success_count += 1
            print(f" -> {format_size(decrypted_size)} decrypted")

            if args.delete_encrypted:
                enc_file.unlink()
                deleted_files.append(enc_file)

        except ValueError as e:
            fail_count += 1
            print(f" FAILED: {e}")
            # Clean up partial output file
            if out_path.exists():
                out_path.unlink()
        except Exception as e:
            fail_count += 1
            print(f" FAILED: {e}")
            if out_path.exists():
                out_path.unlink()

    # Print summary
    print()
    print("=" * 50)
    print(f"Decrypted {success_count} file(s), {format_size(total_bytes)} total")
    if fail_count > 0:
        print(f"Failed: {fail_count} file(s)")
    if deleted_files:
        print(f"Deleted {len(deleted_files)} encrypted source file(s)")
    print(f"Output directory: {output_dir.resolve()}")

    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
