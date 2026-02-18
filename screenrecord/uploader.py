"""Google Drive uploader module for screen recordings.

Handles uploading recordings to Google Drive and deleting local files
after confirmed successful upload.
"""

import logging
import time
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger(__name__)

MIME_TYPES = {
    ".mp4": "video/mp4",
    ".txt": "text/plain",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".json": "application/json",
    ".log": "text/plain",
}

SCOPES = ["https://www.googleapis.com/auth/drive"]

CHUNK_SIZE = 50 * 1024 * 1024  # 50 MB


class DriveUploader:
    """Uploads files to Google Drive using a service account."""

    def __init__(self, config: dict):
        """Authenticate with Google Drive and resolve the employee folder.

        Args:
            config: Dictionary containing google_drive.credentials_file,
                    google_drive.root_folder_id, employee_name, and
                    computer_name.
        """
        drive_cfg = config["google_drive"]
        credentials_file = drive_cfg["credentials_file"]
        self.root_folder_id = drive_cfg["root_folder_id"]
        self.employee_name = config["employee_name"]
        self.computer_name = config["computer_name"]
        self.client_name = config.get("client_name", "")

        logger.info("Authenticating with Google Drive service account.")
        try:
            creds = Credentials.from_service_account_file(
                credentials_file, scopes=SCOPES
            )
            self.service = build("drive", "v3", credentials=creds)
        except Exception:
            logger.exception("Failed to authenticate with Google Drive.")
            raise

        # Build folder hierarchy: Root / Client / Employee-Computer
        parent_id = self.root_folder_id

        # Create client folder if client_name is set
        if self.client_name:
            logger.info(
                "Resolving client folder '%s' under root folder '%s'.",
                self.client_name,
                self.root_folder_id,
            )
            self.client_folder_id = self._find_or_create_folder(
                self.client_name, self.root_folder_id
            )
            parent_id = self.client_folder_id
            logger.info("Client folder ID: %s", self.client_folder_id)
        else:
            self.client_folder_id = None

        folder_name = f"{self.employee_name} - {self.computer_name}"
        logger.info(
            "Resolving employee folder '%s' under parent '%s'.",
            folder_name,
            parent_id,
        )
        self.employee_folder_id = self._find_or_create_folder(
            folder_name, parent_id
        )
        logger.info("Employee folder ID: %s", self.employee_folder_id)

    # ------------------------------------------------------------------
    # Folder helpers
    # ------------------------------------------------------------------

    def _find_or_create_folder(self, name: str, parent_id: str) -> str:
        """Return the ID of an existing folder or create a new one.

        Args:
            name: Folder name to search for / create.
            parent_id: ID of the parent folder in Google Drive.

        Returns:
            The Google Drive folder ID.
        """
        query = (
            f"name='{name}' and '{parent_id}' in parents "
            f"and mimeType='application/vnd.google-apps.folder' "
            f"and trashed=false"
        )
        try:
            results = (
                self.service.files()
                .list(
                    q=query, spaces="drive", fields="files(id, name)",
                    supportsAllDrives=True, includeItemsFromAllDrives=True,
                )
                .execute()
            )
            files = results.get("files", [])
            if files:
                folder_id = files[0]["id"]
                logger.info("Found existing folder '%s' (%s).", name, folder_id)
                return folder_id
        except HttpError:
            logger.exception("Error searching for folder '%s'.", name)
            raise

        # Folder does not exist yet -- create it.
        file_metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        try:
            folder = (
                self.service.files()
                .create(body=file_metadata, fields="id", supportsAllDrives=True)
                .execute()
            )
            folder_id = folder["id"]
            logger.info("Created new folder '%s' (%s).", name, folder_id)
            return folder_id
        except HttpError:
            logger.exception("Error creating folder '%s'.", name)
            raise

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    def upload_file(
        self, local_path: str | Path, delete_after: bool = True
    ) -> str:
        """Upload a single file to the employee folder on Google Drive.

        Uses resumable upload with 50 MB chunks so large recordings are
        handled reliably.

        Args:
            local_path: Path to the local file.
            delete_after: If True, delete the local file after confirmed
                          upload.

        Returns:
            The Google Drive file ID of the uploaded file.
        """
        local_path = Path(local_path)
        if not local_path.exists():
            raise FileNotFoundError(f"Local file not found: {local_path}")

        mime_type = MIME_TYPES.get(local_path.suffix.lower(), "application/octet-stream")
        file_size = local_path.stat().st_size

        logger.info(
            "Uploading '%s' (%s, %.2f MB) to folder %s.",
            local_path.name,
            mime_type,
            file_size / (1024 * 1024),
            self.employee_folder_id,
        )

        file_metadata = {
            "name": local_path.name,
            "parents": [self.employee_folder_id],
        }

        media = MediaFileUpload(
            str(local_path),
            mimetype=mime_type,
            resumable=True,
            chunksize=CHUNK_SIZE,
        )

        request = self.service.files().create(
            body=file_metadata, media_body=media, fields="id",
            supportsAllDrives=True,
        )

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                progress_pct = status.progress() * 100
                logger.info("Upload progress: %.1f%%", progress_pct)

        file_id = response.get("id")
        if file_id is None:
            raise RuntimeError(
                f"Upload of '{local_path.name}' did not return a file ID."
            )

        logger.info(
            "Upload complete. File '%s' -> Drive ID %s.", local_path.name, file_id
        )

        if delete_after:
            try:
                local_path.unlink()
                logger.info("Deleted local file '%s'.", local_path)
            except OSError:
                logger.exception(
                    "Failed to delete local file '%s' after upload.", local_path
                )

        return file_id

    # ------------------------------------------------------------------
    # Retry wrapper
    # ------------------------------------------------------------------

    def upload_with_retry(
        self, local_path: str | Path, delete_after: bool = True
    ) -> str:
        """Upload a file with automatic retry on transient failures.

        Retries up to 3 times with exponential backoff (5 s, 15 s, 45 s).
        HTTP 429 (quota exceeded) uses a longer backoff of 60 s per attempt.

        Args:
            local_path: Path to the local file.
            delete_after: If True, delete the local file after confirmed
                          upload.

        Returns:
            The Google Drive file ID of the uploaded file.
        """
        max_retries = 3
        backoff_times = [5, 15, 45]

        for attempt in range(1, max_retries + 1):
            try:
                return self.upload_file(local_path, delete_after=delete_after)
            except HttpError as exc:
                if exc.resp.status == 429:
                    wait = 60 * attempt
                    logger.warning(
                        "Quota exceeded (429) on attempt %d/%d. "
                        "Waiting %d s before retry.",
                        attempt,
                        max_retries,
                        wait,
                    )
                else:
                    wait = backoff_times[attempt - 1]
                    logger.warning(
                        "HttpError (status %s) on attempt %d/%d. "
                        "Waiting %d s before retry.",
                        exc.resp.status,
                        attempt,
                        max_retries,
                        wait,
                    )
                if attempt == max_retries:
                    logger.error(
                        "Upload failed after %d attempts.", max_retries
                    )
                    raise
                time.sleep(wait)
            except (ConnectionError, TimeoutError) as exc:
                wait = backoff_times[attempt - 1]
                logger.warning(
                    "%s on attempt %d/%d. Waiting %d s before retry.",
                    type(exc).__name__,
                    attempt,
                    max_retries,
                    wait,
                )
                if attempt == max_retries:
                    logger.error(
                        "Upload failed after %d attempts.", max_retries
                    )
                    raise
                time.sleep(wait)

        # Should never be reached, but satisfies type checkers.
        raise RuntimeError("Upload failed: exhausted all retries.")

    # ------------------------------------------------------------------
    # Sharing
    # ------------------------------------------------------------------

    def get_shareable_link(self, file_id: str) -> str:
        """Make a file readable by anyone with the link and return the URL.

        Args:
            file_id: Google Drive file ID.

        Returns:
            A shareable web link to the file.
        """
        permission = {"type": "anyone", "role": "reader"}
        try:
            self.service.permissions().create(
                fileId=file_id, body=permission, fields="id",
                supportsAllDrives=True,
            ).execute()
            logger.info("Set 'anyone with link' read permission on %s.", file_id)
        except HttpError:
            logger.exception(
                "Failed to set sharing permission on file %s.", file_id
            )
            raise

        try:
            file_meta = (
                self.service.files()
                .get(
                    fileId=file_id, fields="webViewLink, webContentLink",
                    supportsAllDrives=True,
                )
                .execute()
            )
        except HttpError:
            logger.exception(
                "Failed to retrieve shareable link for file %s.", file_id
            )
            raise

        link = file_meta.get("webContentLink") or file_meta.get("webViewLink")
        if link is None:
            raise RuntimeError(
                f"Could not obtain a shareable link for file {file_id}."
            )

        logger.info("Shareable link for %s: %s", file_id, link)
        return link
