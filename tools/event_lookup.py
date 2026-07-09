#!/usr/bin/env python3
"""Find an input event, its paired recording, and optionally its video frame.

This is an administrator-side, read-only lookup tool. It works with both the
older Mac JSONL bundles and the newer Windows bundles that also contain a
``logbook.json``. Raw recordings remain encrypted in Drive; the tool downloads
and decrypts only the selected bundle/video into a local output directory.

Examples::

    python3 tools/event_lookup.py --machine Rebekahs-iMac --event-type mouse_click
    python3 tools/event_lookup.py --machine TYLERYOUNG4035 --seq 7 --extract-frame
    python3 tools/event_lookup.py --machine Mac --at 2026-07-09T20:00:00Z \
        --event-type key_sequence --extract-frame

Key text is never printed by default. ``--query-text`` may be used to locate a
known typed phrase, but the matching text remains redacted from command output.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from screenrecord.encryption import FileEncryptor


CREDENTIALS = os.path.expanduser("~/.screenrecord/credentials.json")
LEGACY_KEY = os.path.expanduser("~/.screenrecord/encryption.key")
PRIVATE_KEY = os.environ.get(
    "SCREENRECORD_PRIVATE_KEY",
    os.path.expanduser("~/.screenrecord/keys/screenrecord_envelope_private_key.pem"),
)
DRIVE_ID = "0ANdodpyQPc2tUk9PVA"
DRIVE_EXTRA = {
    "includeItemsFromAllDrives": True,
    "supportsAllDrives": True,
    "corpora": "drive",
    "driveId": DRIVE_ID,
}
EVENT_SUFFIX = ".events.zip.enc"
VIDEO_SUFFIX = ".mp4.enc"


def _drive_service():
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _load_encryptor() -> FileEncryptor:
    legacy = None
    if os.path.isfile(LEGACY_KEY):
        legacy = base64.b64decode(Path(LEGACY_KEY).read_bytes().strip())
    if PRIVATE_KEY and os.path.isfile(PRIVATE_KEY):
        return FileEncryptor(
            key=legacy,
            private_key_pem=Path(PRIVATE_KEY).read_bytes(),
        )
    if legacy:
        return FileEncryptor(key=legacy)
    raise FileNotFoundError("No ScreenRecorder private or legacy decryption key found.")


def _drive_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _list_event_bundles(drive, machine: str, limit: int) -> list[dict[str, Any]]:
    prefix = machine + "_"
    query = (
        f"name contains {_drive_literal(prefix)} and "
        f"name contains {_drive_literal(EVENT_SUFFIX)} and trashed=false"
    )
    result = drive.files().list(
        q=query,
        orderBy="createdTime desc",
        pageSize=min(max(limit * 3, 25), 1000),
        fields="files(id,name,size,createdTime,modifiedTime)",
        **DRIVE_EXTRA,
    ).execute()
    return [
        item for item in result.get("files", [])
        if item["name"].startswith(prefix) and item["name"].endswith(EVENT_SUFFIX)
    ][:limit]


def _find_exact_file(drive, name: str) -> Optional[dict[str, Any]]:
    result = drive.files().list(
        q=f"name={_drive_literal(name)} and trashed=false",
        pageSize=10,
        fields="files(id,name,size,createdTime,modifiedTime)",
        **DRIVE_EXTRA,
    ).execute()
    files = result.get("files", [])
    return files[0] if files else None


def _download(drive, file_id: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    with io.FileIO(destination, "wb") as handle:
        downloader = MediaIoBaseDownload(handle, request, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def _read_events(bundle_path: Path, encryptor: FileEncryptor) -> list[dict[str, Any]]:
    zip_path = encryptor.decrypt_file(bundle_path)
    with zipfile.ZipFile(zip_path) as archive:
        event_name = next(
            (name for name in archive.namelist() if name.endswith(".events.jsonl")),
            None,
        )
        if event_name is None:
            return []
        return [
            json.loads(line)
            for line in archive.read(event_name).decode("utf-8", "replace").splitlines()
            if line.strip()
        ]


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _matches(
    event: dict[str, Any],
    *,
    event_type: Optional[str],
    seq: Optional[int],
    query_text: Optional[str],
) -> bool:
    if event_type and event.get("event_type") != event_type:
        return False
    if seq is not None and int(event.get("seq", -1)) != seq:
        return False
    if query_text:
        text = str((event.get("details") or {}).get("text", ""))
        if query_text.casefold() not in text.casefold():
            return False
    return True


def select_event(
    events: Iterable[dict[str, Any]],
    *,
    event_type: Optional[str] = None,
    seq: Optional[int] = None,
    query_text: Optional[str] = None,
    at: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    candidates = [
        event for event in events
        if _matches(event, event_type=event_type, seq=seq, query_text=query_text)
    ]
    if not candidates:
        return None
    if at is not None:
        return min(
            candidates,
            key=lambda event: abs((_parse_time(event["ts_utc"]) - at).total_seconds()),
        )
    return max(candidates, key=lambda event: _parse_time(event["ts_utc"]))


def _safe_event(event: dict[str, Any]) -> dict[str, Any]:
    details = dict(event.get("details") or {})
    if "text" in details:
        details["text"] = "<redacted>"
    if "keys" in details:
        details["keys"] = "<redacted>"
    return {
        "seq": event.get("seq"),
        "ts_utc": event.get("ts_utc"),
        "event_type": event.get("event_type"),
        "video_file": event.get("video_file"),
        "video_offset_sec": event.get("video_offset_sec"),
        "details": details,
    }


def _frame_filename(machine: str, event: dict[str, Any]) -> str:
    safe_machine = re.sub(r"[^A-Za-z0-9._-]+", "-", machine).strip("-") or "machine"
    safe_time = str(event.get("ts_utc", "unknown")).replace(":", "-")
    return f"{safe_machine}_event-{event.get('seq', 'unknown')}_{safe_time}.png"


def _extract_frame(video: Path, offset: float, destination: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-ss", f"{offset:.3f}",
            "-i", str(video), "-frames:v", "1", "-update", "1", str(destination),
        ],
        check=True,
    )


def lookup(args: argparse.Namespace) -> dict[str, Any]:
    drive = _drive_service()
    encryptor = _load_encryptor()
    target_time = _parse_time(args.at) if args.at else None
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    selected = None
    selected_bundle = None
    with tempfile.TemporaryDirectory(prefix="screenrecord-event-") as temp_name:
        temp = Path(temp_name)
        for bundle in _list_event_bundles(drive, args.machine, args.bundle_limit):
            encrypted_bundle = temp / bundle["name"]
            _download(drive, bundle["id"], encrypted_bundle)
            events = _read_events(encrypted_bundle, encryptor)
            candidate = select_event(
                events,
                event_type=args.event_type,
                seq=args.seq,
                query_text=args.query_text,
                at=target_time,
            )
            if candidate is None:
                continue
            if selected is None:
                selected, selected_bundle = candidate, bundle
            elif target_time is not None:
                old = abs((_parse_time(selected["ts_utc"]) - target_time).total_seconds())
                new = abs((_parse_time(candidate["ts_utc"]) - target_time).total_seconds())
                if new < old:
                    selected, selected_bundle = candidate, bundle
            else:
                break

        if selected is None or selected_bundle is None:
            raise LookupError("No matching event found in the searched bundles.")

        stem = selected_bundle["name"][:-len(EVENT_SUFFIX)]
        video_name = str(selected.get("video_file") or (stem + ".mp4"))
        encrypted_video_name = video_name + ".enc"
        video = _find_exact_file(drive, encrypted_video_name)
        if video is None:
            raise LookupError(f"Paired encrypted video not found: {encrypted_video_name}")

        result: dict[str, Any] = {
            "machine": args.machine,
            "event": _safe_event(selected),
            "pairing": {
                "join_key": stem,
                "event_bundle_name": selected_bundle["name"],
                "event_bundle_drive_id": selected_bundle["id"],
                "event_bundle_drive_link": (
                    f"https://drive.google.com/file/d/{selected_bundle['id']}/view"
                ),
                "encrypted_video_name": video["name"],
                "encrypted_video_drive_id": video["id"],
                "encrypted_video_drive_link": (
                    f"https://drive.google.com/file/d/{video['id']}/view"
                ),
            },
        }

        if args.extract_frame:
            encrypted_video = temp / video["name"]
            _download(drive, video["id"], encrypted_video)
            decrypted_video = encryptor.decrypt_file(encrypted_video)
            frame = output_dir / _frame_filename(args.machine, selected)
            _extract_frame(
                decrypted_video,
                float(selected.get("video_offset_sec", 0.0)),
                frame,
            )
            result["frame_path"] = str(frame)

    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--machine", required=True, help="Exact dashboard computer name")
    parser.add_argument(
        "--event-type",
        choices=["mouse_click", "mouse_scroll", "key_sequence", "key_press"],
    )
    parser.add_argument("--seq", type=int, help="Exact event sequence number")
    parser.add_argument("--query-text", help="Find a known typed phrase; output stays redacted")
    parser.add_argument("--at", help="Select the closest matching UTC timestamp")
    parser.add_argument("--bundle-limit", type=int, default=24)
    parser.add_argument("--extract-frame", action="store_true")
    parser.add_argument("--output-dir", default="work/event-lookup")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        result = lookup(args)
    except (FileNotFoundError, LookupError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
