"""Heartbeat module for the screen recording service.

Periodically writes a status JSON file to Google Drive so the monitoring
dashboard can track which machines are actively recording.  The heartbeat
file is uploaded to a ``_heartbeats`` subfolder under the Drive root folder.
"""

import io
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]

HEARTBEAT_FOLDER_NAME = "_heartbeats"
HEARTBEAT_INTERVAL = 300  # 5 minutes


class HeartbeatSender:
    """Writes periodic heartbeat JSON files to Google Drive.

    Each recorder instance creates a single ``heartbeat_{computer_name}.json``
    file inside the ``_heartbeats`` folder.  The file is overwritten (updated)
    on every tick so the dashboard always sees fresh data.

    Args:
        config: The full application configuration dictionary.
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.employee_name: str = config.get("employee_name", "Unknown")
        self.computer_name: str = config.get("computer_name", "Unknown")
        self.client_name: str = config.get("client_name", "Default")

        drive_cfg = config["google_drive"]
        self.root_folder_id: str = drive_cfg["root_folder_id"]

        credentials_file = drive_cfg["credentials_file"]
        creds = Credentials.from_service_account_file(
            credentials_file, scopes=SCOPES
        )
        self.service = build("drive", "v3", credentials=creds)

        self._heartbeat_folder_id: Optional[str] = None
        self._heartbeat_file_id: Optional[str] = None

        # Counters
        self._segments_uploaded: int = 0
        self._started_at: str = datetime.now(timezone.utc).isoformat()
        self._status: str = "recording"

        # Threading
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background heartbeat thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Heartbeat thread is already running.")
            return

        self._stop_event.clear()
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._status = "recording"

        self._thread = threading.Thread(
            target=self._run, name="heartbeat", daemon=True
        )
        self._thread.start()
        logger.info("Heartbeat thread started (interval=%ds).", HEARTBEAT_INTERVAL)

    def stop(self) -> None:
        """Stop the heartbeat thread and send a final 'stopped' heartbeat."""
        self._status = "stopped"
        self._stop_event.set()

        # Send one last heartbeat with status=stopped
        try:
            self._send_heartbeat()
        except Exception:
            logger.exception("Failed to send final 'stopped' heartbeat.")

        if self._thread is not None:
            self._thread.join(timeout=10)
        logger.info("Heartbeat thread stopped.")

    def increment_segments(self, count: int = 1) -> None:
        """Increment the segments-uploaded counter.

        Call this from the main processing pipeline each time a segment
        is successfully uploaded to Google Drive.
        """
        self._segments_uploaded += count

    def set_status(self, status: str) -> None:
        """Update the current status string (e.g. 'recording', 'error')."""
        self._status = status

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Background loop that sends heartbeats at a fixed interval."""
        # Send an immediate heartbeat on start
        self._send_heartbeat_safe()

        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=HEARTBEAT_INTERVAL)
            if not self._stop_event.is_set():
                self._send_heartbeat_safe()

    def _send_heartbeat_safe(self) -> None:
        """Send a heartbeat, catching and logging any errors."""
        try:
            self._send_heartbeat()
        except Exception:
            logger.exception("Failed to send heartbeat.")

    def _send_heartbeat(self) -> None:
        """Build the heartbeat payload and upload/update it on Drive."""
        now = datetime.now(timezone.utc)

        # Calculate uptime
        started_dt = datetime.fromisoformat(self._started_at)
        uptime_seconds = (now - started_dt).total_seconds()
        uptime_hours = round(uptime_seconds / 3600, 2)

        payload = {
            "employee_name": self.employee_name,
            "computer_name": self.computer_name,
            "client_name": self.client_name,
            "status": self._status,
            "last_heartbeat": now.isoformat(),
            "segments_uploaded": self._segments_uploaded,
            "started_at": self._started_at,
            "uptime_hours": uptime_hours,
        }

        data = json.dumps(payload, indent=2).encode("utf-8")
        filename = f"heartbeat_{self.computer_name}.json"

        # Ensure the _heartbeats folder exists
        if self._heartbeat_folder_id is None:
            self._heartbeat_folder_id = self._find_or_create_folder(
                HEARTBEAT_FOLDER_NAME, self.root_folder_id
            )

        media = MediaIoBaseUpload(
            io.BytesIO(data), mimetype="application/json", resumable=False
        )

        if self._heartbeat_file_id is None:
            # Check if the file already exists from a previous run
            self._heartbeat_file_id = self._find_file(
                filename, self._heartbeat_folder_id
            )

        if self._heartbeat_file_id is not None:
            # Update the existing file
            try:
                self.service.files().update(
                    fileId=self._heartbeat_file_id,
                    media_body=media,
                    supportsAllDrives=True,
                ).execute()
                logger.debug("Heartbeat updated: %s", filename)
            except HttpError as exc:
                if exc.resp.status == 404:
                    # File was deleted externally; re-create it
                    logger.warning("Heartbeat file was deleted; re-creating.")
                    self._heartbeat_file_id = None
                    self._send_heartbeat()  # Recurse once to create
                else:
                    raise
        else:
            # Create a new heartbeat file
            file_metadata = {
                "name": filename,
                "parents": [self._heartbeat_folder_id],
            }
            result = (
                self.service.files()
                .create(
                    body=file_metadata, media_body=media, fields="id",
                    supportsAllDrives=True,
                )
                .execute()
            )
            self._heartbeat_file_id = result["id"]
            logger.info(
                "Heartbeat file created: %s (id=%s)",
                filename,
                self._heartbeat_file_id,
            )

    # ------------------------------------------------------------------
    # Drive helpers
    # ------------------------------------------------------------------

    def _find_or_create_folder(self, name: str, parent_id: str) -> str:
        """Return the ID of an existing folder or create a new one."""
        query = (
            f"name='{name}' and '{parent_id}' in parents "
            f"and mimeType='application/vnd.google-apps.folder' "
            f"and trashed=false"
        )
        results = (
            self.service.files()
            .list(
                q=query, spaces="drive", fields="files(id)",
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = results.get("files", [])
        if files:
            return files[0]["id"]

        file_metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        folder = (
            self.service.files()
            .create(body=file_metadata, fields="id", supportsAllDrives=True)
            .execute()
        )
        folder_id = folder["id"]
        logger.info("Created heartbeat folder '%s' (id=%s).", name, folder_id)
        return folder_id

    def _find_file(self, name: str, parent_id: str) -> Optional[str]:
        """Find an existing file by name in a folder. Returns ID or None."""
        query = (
            f"name='{name}' and '{parent_id}' in parents "
            f"and trashed=false"
        )
        results = (
            self.service.files()
            .list(
                q=query, spaces="drive", fields="files(id)",
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = results.get("files", [])
        if files:
            return files[0]["id"]
        return None
