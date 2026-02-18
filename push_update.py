#!/usr/bin/env python3
"""Admin helper script to push a screen-recorder update to Google Drive.

This script bundles the local ``screenrecord/`` package into a tar.gz
archive, computes its SHA-256 hash, writes a ``version.json`` manifest,
and uploads both files to the ``_update/`` folder on Google Drive.

Usage::

    python3 push_update.py \\
        --credentials credentials.json \\
        --drive-folder-id "ROOT_FOLDER_ID" \\
        --version "0.2.0"

After running this command, every deployed recorder will pick up the
new version during its next periodic update check.
"""

import argparse
import hashlib
import io
import json
import os
import sys
import tarfile
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaInMemoryUpload

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

UPDATE_FOLDER_NAME = "_update"


def _authenticate(credentials_file: str):
    """Build and return an authenticated Google Drive service object."""
    creds = Credentials.from_service_account_file(credentials_file, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def _find_or_create_folder(service, name: str, parent_id: str) -> str:
    """Find an existing Drive folder or create a new one.

    Args:
        service: Authenticated Google Drive API service.
        name: Folder name.
        parent_id: Parent folder ID on Drive.

    Returns:
        The Drive folder ID.
    """
    query = (
        f"name='{name}' and '{parent_id}' in parents "
        f"and mimeType='application/vnd.google-apps.folder' "
        f"and trashed=false"
    )
    results = (
        service.files()
        .list(q=query, spaces="drive", fields="files(id, name)")
        .execute()
    )
    files = results.get("files", [])
    if files:
        folder_id = files[0]["id"]
        print(f"  Found existing '{name}' folder ({folder_id}).")
        return folder_id

    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=metadata, fields="id").execute()
    folder_id = folder["id"]
    print(f"  Created '{name}' folder ({folder_id}).")
    return folder_id


def _upload_or_replace(
    service, folder_id: str, filename: str, data: bytes, mime_type: str
) -> str:
    """Upload a file to Drive, replacing it if it already exists.

    Args:
        service: Authenticated Google Drive API service.
        folder_id: Target folder ID on Drive.
        filename: Name of the file on Drive.
        data: File content as bytes.
        mime_type: MIME type for the upload.

    Returns:
        The Drive file ID of the uploaded (or updated) file.
    """
    # Check if the file already exists.
    query = (
        f"name='{filename}' and '{folder_id}' in parents "
        f"and trashed=false"
    )
    results = (
        service.files()
        .list(q=query, spaces="drive", fields="files(id, name)")
        .execute()
    )
    existing = results.get("files", [])

    media = MediaInMemoryUpload(data, mimetype=mime_type, resumable=True)

    if existing:
        # Update existing file.
        file_id = existing[0]["id"]
        updated = (
            service.files()
            .update(fileId=file_id, media_body=media, fields="id")
            .execute()
        )
        print(f"  Updated existing file '{filename}' ({updated['id']}).")
        return updated["id"]

    # Create new file.
    metadata = {"name": filename, "parents": [folder_id]}
    created = (
        service.files()
        .create(body=metadata, media_body=media, fields="id")
        .execute()
    )
    print(f"  Uploaded new file '{filename}' ({created['id']}).")
    return created["id"]


def _build_archive(source_dir: Path, version: str) -> tuple:
    """Create a tar.gz archive of the screenrecord package.

    Args:
        source_dir: Path to the ``screenrecord/`` package directory.
        version: Version string (used in the archive filename).

    Returns:
        Tuple of (archive_filename, archive_bytes, sha256_hex).
    """
    archive_name = f"update_{version}.tar.gz"
    buf = io.BytesIO()

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for item in sorted(source_dir.rglob("*")):
            # Skip __pycache__, .pyc files, and hidden files.
            rel = item.relative_to(source_dir.parent)
            parts = rel.parts
            if any(
                p.startswith(".") or p == "__pycache__" for p in parts
            ):
                continue
            if item.suffix == ".pyc":
                continue

            tar.add(str(item), arcname=str(rel))

    archive_bytes = buf.getvalue()
    sha256_hex = hashlib.sha256(archive_bytes).hexdigest()

    return archive_name, archive_bytes, sha256_hex


def main():
    parser = argparse.ArgumentParser(
        description="Push a screen-recorder update to Google Drive.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            '  python3 push_update.py --credentials credentials.json \\\n'
            '      --drive-folder-id "1aBcDeFgHiJkLmNoPqRsTuVwXyZ" \\\n'
            '      --version "0.2.0"'
        ),
    )
    parser.add_argument(
        "--credentials",
        required=True,
        help="Path to the Google service account credentials JSON file.",
    )
    parser.add_argument(
        "--drive-folder-id",
        required=True,
        help="ID of the root Google Drive folder (shared with the service account).",
    )
    parser.add_argument(
        "--version",
        required=True,
        help="Version string for this update (e.g. '0.2.0').",
    )
    parser.add_argument(
        "--source-dir",
        default=None,
        help=(
            "Path to the screenrecord/ package directory to bundle. "
            "Defaults to ./screenrecord/ relative to this script."
        ),
    )

    args = parser.parse_args()

    # Resolve source directory.
    if args.source_dir:
        source_dir = Path(args.source_dir).resolve()
    else:
        source_dir = (Path(__file__).resolve().parent / "screenrecord")

    if not source_dir.is_dir():
        print(f"Error: source directory not found: {source_dir}", file=sys.stderr)
        sys.exit(1)

    init_file = source_dir / "__init__.py"
    if not init_file.exists():
        print(
            f"Error: {init_file} not found. Is '{source_dir}' the right package?",
            file=sys.stderr,
        )
        sys.exit(1)

    version = args.version

    # ----------------------------------------------------------------
    # Step 1: Build the archive
    # ----------------------------------------------------------------
    print(f"Building update archive for version {version}...")
    archive_name, archive_bytes, sha256_hex = _build_archive(source_dir, version)

    archive_size_mb = len(archive_bytes) / (1024 * 1024)
    print(f"  Archive: {archive_name} ({archive_size_mb:.2f} MB)")
    print(f"  SHA-256: {sha256_hex}")

    # ----------------------------------------------------------------
    # Step 2: Build version.json
    # ----------------------------------------------------------------
    version_meta = {
        "version": version,
        "url": archive_name,
        "sha256": sha256_hex,
    }
    version_json_bytes = json.dumps(version_meta, indent=2).encode("utf-8")
    print(f"  version.json: {json.dumps(version_meta)}")

    # ----------------------------------------------------------------
    # Step 3: Authenticate and upload
    # ----------------------------------------------------------------
    print("\nAuthenticating with Google Drive...")
    try:
        service = _authenticate(args.credentials)
    except Exception as exc:
        print(f"Error: failed to authenticate: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Resolving '{UPDATE_FOLDER_NAME}' folder under root {args.drive_folder_id}...")
    try:
        update_folder_id = _find_or_create_folder(
            service, UPDATE_FOLDER_NAME, args.drive_folder_id
        )
    except HttpError as exc:
        print(f"Error: failed to find/create _update folder: {exc}", file=sys.stderr)
        sys.exit(1)

    print("Uploading update archive...")
    try:
        _upload_or_replace(
            service,
            update_folder_id,
            archive_name,
            archive_bytes,
            "application/gzip",
        )
    except HttpError as exc:
        print(f"Error: failed to upload archive: {exc}", file=sys.stderr)
        sys.exit(1)

    print("Uploading version.json...")
    try:
        _upload_or_replace(
            service,
            update_folder_id,
            "version.json",
            version_json_bytes,
            "application/json",
        )
    except HttpError as exc:
        print(f"Error: failed to upload version.json: {exc}", file=sys.stderr)
        sys.exit(1)

    # ----------------------------------------------------------------
    # Done
    # ----------------------------------------------------------------
    print(f"\nUpdate v{version} pushed successfully!")
    print(
        "Deployed recorders will pick it up on their next update check."
    )


if __name__ == "__main__":
    main()
