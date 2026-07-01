#!/usr/bin/env python3
"""DOM events processor: surface captured input events + screenshots on the
dashboard.

For each ``*.events.zip.enc`` bundle in the Shared Drive this:
  1. downloads + decrypts it (chunked ENCRV1 via the shared key),
  2. archives the full-res screenshots in a ``_dom_screenshots`` Drive folder,
  3. appends one row per event to the ``dom_events`` Sheet tab the dashboard
     reads — action detail + video tie-in metadata + an inline screenshot
     thumbnail (base64, so no external image hosting is needed).

The data is shown as captured (no redaction/aliasing) — handling of PHI is
covered by the signed BAA. Raw bundles stay encrypted in Drive; this just makes
them viewable. Idempotent: a bundle whose stem already has rows is skipped.

Run:  python3 tools/dom_processor.py [--limit N]
"""

import argparse
import base64
import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from PIL import Image

from screenrecord.encryption import FileEncryptor

CRED = os.path.expanduser("~/.screenrecord/credentials.json")
KEY = os.path.expanduser("~/.screenrecord/encryption.key")
SHEET_ID = "1ujcQshvE7Gu_i_42kwgjQCpfmeID_EyZtpMC35g2bFU"
DRIVE_ID = "0ANdodpyQPc2tUk9PVA"
SHOTS_FOLDER = "_dom_screenshots"
DOM_TAB = "dom_events"
DOM_HEADERS = [
    "ts_utc", "computer_name", "video_file", "video_offset_sec",
    "event_type", "detail", "screenshot_drive_id", "stem", "seq", "thumb",
]
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]
DRIVE_EXTRA = dict(includeItemsFromAllDrives=True, supportsAllDrives=True,
                   corpora="drive", driveId=DRIVE_ID)
CELL_LIMIT = 48000  # keep the inline thumbnail under Sheets' 50k cell cap


def _drive_sheets():
    creds = service_account.Credentials.from_service_account_file(CRED, scopes=SCOPES)
    return (build("drive", "v3", credentials=creds, cache_discovery=False),
            build("sheets", "v4", credentials=creds, cache_discovery=False))


def _find_or_create_folder(drv, name, parent):
    q = ("name='%s' and mimeType='application/vnd.google-apps.folder' "
         "and '%s' in parents and trashed=false" % (name, parent))
    r = drv.files().list(q=q, fields="files(id)", **DRIVE_EXTRA).execute().get("files", [])
    if r:
        return r[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent]}
    return drv.files().create(body=meta, fields="id", supportsAllDrives=True).execute()["id"]


def _ensure_dom_tab(sht):
    meta = sht.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    tabs = {s["properties"]["title"] for s in meta["sheets"]}
    if DOM_TAB not in tabs:
        sht.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={
            "requests": [{"addSheet": {"properties": {"title": DOM_TAB}}}]
        }).execute()
        sht.spreadsheets().values().update(
            spreadsheetId=SHEET_ID, range=f"{DOM_TAB}!A1",
            valueInputOption="RAW", body={"values": [DOM_HEADERS]}).execute()
        return set()
    vals = sht.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"{DOM_TAB}!H2:H").execute().get("values", [])
    return {v[0] for v in vals if v}


def _list_bundles(drv, limit):
    r = drv.files().list(
        q="name contains '.events.zip.enc' and trashed=false",
        orderBy="createdTime desc", pageSize=limit,
        fields="files(id,name)", **DRIVE_EXTRA).execute()
    return r.get("files", [])


def _download(drv, fid, dst):
    fh = io.FileIO(dst, "wb")
    dl = MediaIoBaseDownload(fh, drv.files().get_media(fileId=fid, supportsAllDrives=True))
    done = False
    while not done:
        _, done = dl.next_chunk()
    fh.close()


def _upload_png(drv, path, parent, upload_name=None):
    """Archive the full-res screenshot in Drive (kept private)."""
    meta = {"name": upload_name or Path(path).name, "parents": [parent]}
    media = MediaFileUpload(path, mimetype="image/png")
    return drv.files().create(body=meta, media_body=media, fields="id",
                              supportsAllDrives=True).execute()["id"]


def _thumb_data_uri(png_path: Path) -> str:
    """Inline JPEG data URI of a screenshot for the dashboard grid. Steps the
    width/quality down until it fits Sheets' cell limit."""
    img = Image.open(png_path).convert("RGB")
    for w, q in [(640, 55), (560, 48), (480, 42), (400, 38)]:
        im = img.resize((w, max(1, int(img.height * w / img.width))))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=q, optimize=True)
        uri = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        if len(uri) < CELL_LIMIT:
            return uri
    return uri  # smallest attempt, even if slightly over


def _event_detail(ev: dict) -> str:
    et = ev.get("event_type", "?")
    d = ev.get("details") or {}
    if et == "key_sequence":
        count = d.get("key_count", "")
        text = str(d.get("text", "") or "")
        if text and text != "<redacted>":
            return f"{count} key(s) typed: {text}"
        return f"{count} key(s) typed"
    if et == "key_press":
        return str(d.get("key", ""))
    if "click" in et:
        return f"{d.get('button', '')} click @ ({d.get('x')},{d.get('y')})".strip()
    return et


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()

    enc = FileEncryptor.load_key(KEY)
    drv, sht = _drive_sheets()
    done_stems = _ensure_dom_tab(sht)
    shots_folder = _find_or_create_folder(drv, SHOTS_FOLDER, DRIVE_ID)
    bundles = _list_bundles(drv, args.limit)
    print(f"{len(bundles)} bundle(s) found; {len(done_stems)} stem(s) already processed.")

    new_rows = []
    for b in bundles:
        stem = b["name"].replace(".events.zip.enc", "")
        if stem in done_stems:
            print(f"  skip (done): {stem}")
            continue
        computer = stem.split("_")[0]
        work = tempfile.mkdtemp()
        encp = os.path.join(work, b["name"])
        _download(drv, b["id"], encp)
        ex = os.path.join(work, "x"); os.makedirs(ex)
        zipfile.ZipFile(enc.decrypt_file(encp)).extractall(ex)
        ev_file = next(Path(ex).glob("*.events.jsonl"), None)
        shots = Path(ex) / f"{stem}.events"
        if ev_file is None:
            print(f"  no events.jsonl in {stem}; skip"); continue

        events = []
        for line in ev_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
        if not events:
            print(f"  no events in {stem}; skip"); continue

        id_by_png, thumb_by_png = {}, {}
        if shots.is_dir():
            for png in sorted(shots.glob("*.png")):
                upload_name = f"{stem}__{png.name}"
                id_by_png[png.name] = _upload_png(
                    drv, str(png), shots_folder, upload_name=upload_name,
                )
                thumb_by_png[png.name] = _thumb_data_uri(png)

        for ev in events:
            shot = ev.get("screenshot")
            new_rows.append([
                ev.get("ts_utc", ""), computer, ev.get("video_file", ""),
                ev.get("video_offset_sec", ""), ev.get("event_type", ""),
                _event_detail(ev), id_by_png.get(shot, ""), stem, ev.get("seq", ""),
                thumb_by_png.get(shot, ""),
            ])
        print(f"  processed {stem}: {len(events)} events, {len(id_by_png)} screenshots")

    if new_rows:
        new_rows.sort(key=lambda row: row[0])
        sht.spreadsheets().values().append(
            spreadsheetId=SHEET_ID, range=f"{DOM_TAB}!A1",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": new_rows}).execute()
    print(f"\nAppended {len(new_rows)} event row(s) to '{DOM_TAB}'.")


if __name__ == "__main__":
    main()
