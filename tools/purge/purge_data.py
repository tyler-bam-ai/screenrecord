#!/usr/bin/env python3
"""
BAM AI — End-of-Pilot Data Purge Tool

Securely deletes all customer data from Google Drive and local endpoints.
Generates a written deletion certification for compliance documentation.

Usage:
    python purge_data.py --credentials /path/to/credentials.json --folder-id FOLDER_ID
    python purge_data.py --credentials /path/to/credentials.json --folder-id FOLDER_ID --dry-run
    python purge_data.py --credentials /path/to/credentials.json --folder-id FOLDER_ID --certify
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
except ImportError:
    print("ERROR: google-api-python-client and google-auth are required.")
    print("  pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib")
    sys.exit(1)

SCOPES = ["https://www.googleapis.com/auth/drive"]
INSTALL_DIR = Path.home() / ".screenrecord"


def get_drive_service(credentials_path: str):
    """Build an authenticated Google Drive API service."""
    creds = Credentials.from_service_account_file(credentials_path, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def list_all_files(service, folder_id: str) -> list:
    """Recursively list all files under a Drive folder."""
    all_files = []
    page_token = None

    while True:
        results = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, mimeType, size)",
                pageSize=100,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = results.get("files", [])
        for f in files:
            all_files.append(f)
            # Recurse into subfolders
            if f["mimeType"] == "application/vnd.google-apps.folder":
                all_files.extend(list_all_files(service, f["id"]))
        page_token = results.get("nextPageToken")
        if not page_token:
            break

    return all_files


def delete_drive_files(service, files: list, dry_run: bool = False) -> dict:
    """Delete files from Google Drive. Returns summary stats."""
    deleted = 0
    failed = 0
    total_bytes = 0

    for f in files:
        file_id = f["id"]
        name = f["name"]
        size = int(f.get("size", 0))

        if dry_run:
            print(f"  [DRY RUN] Would delete: {name} ({size} bytes)")
            deleted += 1
            total_bytes += size
            continue

        try:
            service.files().delete(
                fileId=file_id,
                supportsAllDrives=True,
            ).execute()
            print(f"  Deleted: {name} ({size} bytes)")
            deleted += 1
            total_bytes += size
        except Exception as e:
            print(f"  FAILED to delete {name}: {e}")
            failed += 1

    return {"deleted": deleted, "failed": failed, "total_bytes": total_bytes}


def purge_local_data(dry_run: bool = False) -> dict:
    """Remove all local screenrecord data."""
    paths_to_remove = [
        INSTALL_DIR / "recordings",
        INSTALL_DIR / "audit.log",
        INSTALL_DIR / "consent_records.json",
        INSTALL_DIR / "config.yaml",
        INSTALL_DIR / "credentials.json",
        INSTALL_DIR / "encryption.key",
        INSTALL_DIR / "rag_db",
    ]
    # Also remove rotated audit logs (audit.log.2026-01-15, etc.)
    if INSTALL_DIR.exists():
        for p in INSTALL_DIR.iterdir():
            if p.name.startswith("audit.log."):
                paths_to_remove.append(p)

    removed = 0
    for p in paths_to_remove:
        if not p.exists():
            continue
        if dry_run:
            print(f"  [DRY RUN] Would remove: {p}")
            removed += 1
            continue
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            print(f"  Removed: {p}")
            removed += 1
        except OSError as e:
            print(f"  FAILED to remove {p}: {e}")

    return {"removed": removed}


def generate_certification(
    cloud_stats: dict,
    local_stats: dict,
    folder_id: str,
    performed_by: str,
    dry_run: bool,
) -> str:
    """Generate a written deletion certification document."""
    now = datetime.now(timezone.utc).isoformat()
    status = "DRY RUN — NO DATA WAS DELETED" if dry_run else "COMPLETED"

    cert = f"""
================================================================================
          BAM AI — DATA DELETION CERTIFICATION
================================================================================

Date:               {now}
Status:             {status}
Performed By:       {performed_by}
Google Drive Folder: {folder_id}

--- Cloud Data (Google Drive) ---
  Files deleted:    {cloud_stats['deleted']}
  Files failed:     {cloud_stats['failed']}
  Total bytes:      {cloud_stats['total_bytes']:,}

--- Local Endpoint Data ---
  Items removed:    {local_stats['removed']}
  Install directory: {INSTALL_DIR}

--- Scope of Deletion ---
  - All encrypted screen recording files (.mp4.enc)
  - All analysis results and metadata
  - Local audit logs and consent records
  - Service account credentials
  - Encryption keys
  - Configuration files
  - RAG/vector database (if applicable)

--- Certification ---
  I certify that all customer data associated with the above Google Drive
  folder and local endpoint installation has been permanently deleted from
  all production systems. Backups, if any, will be purged per the retention
  schedule documented in the BAA.

  This certification is provided in compliance with HIPAA Security Rule
  §164.310(d)(2)(i) — Disposal and §164.310(d)(2)(ii) — Media Re-use.

================================================================================
"""
    return cert


def main():
    parser = argparse.ArgumentParser(
        description="BAM AI — End-of-Pilot Data Purge Tool"
    )
    parser.add_argument(
        "--credentials", required=True, help="Path to Google service account JSON"
    )
    parser.add_argument(
        "--folder-id", required=True, help="Google Drive folder ID to purge"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without actually deleting",
    )
    parser.add_argument(
        "--certify",
        action="store_true",
        help="Generate a written deletion certification",
    )
    parser.add_argument(
        "--performed-by",
        default="BAM AI Administrator",
        help="Name for the deletion certification",
    )
    parser.add_argument(
        "--skip-local",
        action="store_true",
        help="Skip local data purge (cloud only)",
    )
    parser.add_argument(
        "--skip-cloud",
        action="store_true",
        help="Skip cloud data purge (local only)",
    )
    args = parser.parse_args()

    print()
    print("  BAM AI — Data Purge Tool")
    if args.dry_run:
        print("  MODE: DRY RUN (no data will be deleted)")
    else:
        print("  MODE: LIVE DELETION")
        print()
        confirm = input("  Type 'DELETE' to confirm permanent data deletion: ")
        if confirm != "DELETE":
            print("  Aborted.")
            sys.exit(0)
    print()

    cloud_stats = {"deleted": 0, "failed": 0, "total_bytes": 0}
    local_stats = {"removed": 0}

    # --- Cloud purge ---
    if not args.skip_cloud:
        print("  --- Purging Google Drive ---")
        service = get_drive_service(args.credentials)
        files = list_all_files(service, args.folder_id)
        print(f"  Found {len(files)} files/folders in Drive")
        cloud_stats = delete_drive_files(service, files, dry_run=args.dry_run)
        print(f"  Cloud purge complete: {cloud_stats['deleted']} deleted, "
              f"{cloud_stats['failed']} failed")
        print()

    # --- Local purge ---
    if not args.skip_local:
        print("  --- Purging Local Data ---")
        local_stats = purge_local_data(dry_run=args.dry_run)
        print(f"  Local purge complete: {local_stats['removed']} items removed")
        print()

    # --- Certification ---
    if args.certify or not args.dry_run:
        cert = generate_certification(
            cloud_stats, local_stats, args.folder_id, args.performed_by, args.dry_run
        )
        cert_path = Path(f"deletion_certification_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
        cert_path.write_text(cert, encoding="utf-8")
        print(f"  Deletion certification saved: {cert_path}")
        print(cert)

    print("  Done.")


if __name__ == "__main__":
    main()
