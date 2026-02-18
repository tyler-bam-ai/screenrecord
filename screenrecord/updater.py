"""Remote update module for the screen recording service.

Checks for updates published to a Google Drive ``_update/`` folder and
applies them safely.  The admin pushes updates using ``push_update.py``;
each deployed recorder periodically calls ``check_and_apply()`` to pull
and install new versions.

Update package layout on Google Drive (inside the shared root folder):

    _update/
        version.json          - ``{"version": "X.Y.Z", "url": "update_X.Y.Z.tar.gz", "sha256": "..."}``
        update_X.Y.Z.tar.gz  - tar.gz archive containing the updated ``screenrecord/`` package files

Safety guarantees:
    * SHA-256 hash is verified before extraction.
    * A timestamped backup of the old installation is kept.
    * All errors are caught so a failed update never crashes the recorder.
"""

import hashlib
import io
import json
import logging
import os
import shutil
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]

# Re-export __version__ from the package for convenience.
from screenrecord import __version__ as LOCAL_VERSION


def _parse_version(version_str: str) -> Tuple[int, ...]:
    """Parse a semver-style version string into a comparable tuple.

    Handles versions like ``"0.1.0"``, ``"1.2.3"``, etc.

    Args:
        version_str: Dot-separated version string.

    Returns:
        Tuple of integer components, e.g. ``(0, 1, 0)``.
    """
    try:
        return tuple(int(p) for p in version_str.strip().split("."))
    except (ValueError, AttributeError):
        return (0, 0, 0)


class UpdateChecker:
    """Checks for and applies remote updates from Google Drive.

    Usage::

        updater = UpdateChecker(config)
        updater.check_and_apply()

    The *config* dictionary must contain:

    * ``google_drive.credentials_file`` -- path to the service-account JSON
    * ``google_drive.root_folder_id``   -- ID of the shared root Drive folder
    * ``install_dir`` (optional)        -- path to the ``screenrecord/`` package
      directory on disk; defaults to the directory containing this module.
    """

    # Name of the sub-folder that holds update artefacts on Drive.
    UPDATE_FOLDER_NAME = "_update"
    VERSION_FILE_NAME = "version.json"

    def __init__(self, config: Dict[str, Any]) -> None:
        drive_cfg = config["google_drive"]
        credentials_file = drive_cfg["credentials_file"]
        self.root_folder_id = drive_cfg["root_folder_id"]

        # Where the screenrecord package is installed on this machine.
        self.install_dir = Path(
            config.get("install_dir", Path(__file__).resolve().parent)
        )

        logger.info("Updater: authenticating with Google Drive service account.")
        try:
            creds = Credentials.from_service_account_file(
                credentials_file, scopes=SCOPES
            )
            self.service = build("drive", "v3", credentials=creds)
        except Exception:
            logger.exception("Updater: failed to authenticate with Google Drive.")
            raise

        # Lazily resolved Drive folder ID for ``_update/``.
        self._update_folder_id: Optional[str] = None

        # Cached remote version metadata from the last successful check.
        self._remote_meta: Optional[Dict[str, str]] = None

    # ------------------------------------------------------------------
    # Drive helpers
    # ------------------------------------------------------------------

    def _resolve_update_folder(self) -> Optional[str]:
        """Find the ``_update/`` folder under the root Drive folder.

        Returns:
            The Drive folder ID, or ``None`` if the folder does not exist.
        """
        if self._update_folder_id is not None:
            return self._update_folder_id

        query = (
            f"name='{self.UPDATE_FOLDER_NAME}' "
            f"and '{self.root_folder_id}' in parents "
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
        except HttpError:
            logger.exception("Updater: error searching for _update folder.")
            return None

        files = results.get("files", [])
        if not files:
            logger.debug("Updater: _update folder not found; no updates available.")
            return None

        self._update_folder_id = files[0]["id"]
        logger.debug("Updater: found _update folder (%s).", self._update_folder_id)
        return self._update_folder_id

    def _find_file_in_folder(
        self, filename: str, folder_id: str
    ) -> Optional[str]:
        """Return the Drive file ID of *filename* inside *folder_id*, or ``None``."""
        query = (
            f"name='{filename}' "
            f"and '{folder_id}' in parents "
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
        except HttpError:
            logger.exception("Updater: error searching for file '%s'.", filename)
            return None

        files = results.get("files", [])
        return files[0]["id"] if files else None

    def _download_file(self, file_id: str) -> bytes:
        """Download the full contents of a Drive file into memory.

        Args:
            file_id: Google Drive file ID.

        Returns:
            Raw bytes of the file.
        """
        request = self.service.files().get_media(fileId=file_id)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                logger.debug(
                    "Updater: download progress %.1f%%",
                    status.progress() * 100,
                )
        return buffer.getvalue()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_for_update(self) -> bool:
        """Check whether a newer version is available on Google Drive.

        Reads ``_update/version.json`` from Drive and compares the remote
        version string against the locally installed ``__version__``.

        Returns:
            ``True`` if an update is available, ``False`` otherwise.
        """
        self._remote_meta = None  # Reset from any prior call.

        folder_id = self._resolve_update_folder()
        if folder_id is None:
            return False

        # Find version.json
        version_file_id = self._find_file_in_folder(
            self.VERSION_FILE_NAME, folder_id
        )
        if version_file_id is None:
            logger.debug("Updater: version.json not found in _update folder.")
            return False

        # Download and parse version.json
        try:
            raw = self._download_file(version_file_id)
            meta = json.loads(raw)
        except (json.JSONDecodeError, HttpError):
            logger.exception("Updater: failed to read version.json.")
            return False

        remote_version = meta.get("version", "")
        remote_url = meta.get("url", "")
        remote_sha = meta.get("sha256", "")

        if not remote_version or not remote_url or not remote_sha:
            logger.warning(
                "Updater: version.json is incomplete: %s", meta
            )
            return False

        local_tuple = _parse_version(LOCAL_VERSION)
        remote_tuple = _parse_version(remote_version)

        logger.info(
            "Updater: local version %s, remote version %s.",
            LOCAL_VERSION,
            remote_version,
        )

        if remote_tuple > local_tuple:
            logger.info("Updater: update available (%s -> %s).", LOCAL_VERSION, remote_version)
            self._remote_meta = meta
            return True

        logger.info("Updater: already up to date.")
        return False

    def apply_update(self) -> bool:
        """Download and apply the update that was found by ``check_for_update``.

        This method:
        1. Downloads the tar.gz archive from Drive.
        2. Verifies the SHA-256 hash.
        3. Creates a timestamped backup of the current installation.
        4. Extracts the archive into the install directory.

        Returns:
            ``True`` if the update was applied successfully, ``False`` otherwise.
        """
        if self._remote_meta is None:
            logger.error("Updater: apply_update called without a pending update.")
            return False

        remote_version = self._remote_meta["version"]
        archive_name = self._remote_meta["url"]
        expected_sha = self._remote_meta["sha256"]

        folder_id = self._resolve_update_folder()
        if folder_id is None:
            logger.error("Updater: _update folder disappeared during apply.")
            return False

        # ----------------------------------------------------------
        # 1. Download the archive
        # ----------------------------------------------------------
        archive_file_id = self._find_file_in_folder(archive_name, folder_id)
        if archive_file_id is None:
            logger.error(
                "Updater: archive '%s' not found in _update folder.", archive_name
            )
            return False

        logger.info("Updater: downloading update archive '%s'...", archive_name)
        try:
            archive_bytes = self._download_file(archive_file_id)
        except Exception:
            logger.exception("Updater: failed to download archive '%s'.", archive_name)
            return False

        # ----------------------------------------------------------
        # 2. Verify SHA-256
        # ----------------------------------------------------------
        actual_sha = hashlib.sha256(archive_bytes).hexdigest()
        if actual_sha != expected_sha:
            logger.error(
                "Updater: SHA-256 mismatch! expected=%s actual=%s. "
                "Update aborted.",
                expected_sha,
                actual_sha,
            )
            return False
        logger.info("Updater: SHA-256 verified successfully.")

        # ----------------------------------------------------------
        # 3. Create a backup of the current installation
        # ----------------------------------------------------------
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = self.install_dir.parent / f"screenrecord_backup_{timestamp}"
        try:
            shutil.copytree(self.install_dir, backup_dir)
            logger.info("Updater: backed up current installation to %s.", backup_dir)
        except Exception:
            logger.exception(
                "Updater: failed to create backup at %s. Update aborted.",
                backup_dir,
            )
            return False

        # ----------------------------------------------------------
        # 4. Extract the archive into the install directory
        # ----------------------------------------------------------
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                archive_file = tmp_path / archive_name
                archive_file.write_bytes(archive_bytes)

                with tarfile.open(archive_file, "r:gz") as tar:
                    # Security: check for path traversal attacks.
                    for member in tar.getmembers():
                        member_path = os.path.normpath(member.name)
                        if member_path.startswith("..") or os.path.isabs(member_path):
                            logger.error(
                                "Updater: archive contains unsafe path '%s'. "
                                "Update aborted.",
                                member.name,
                            )
                            # Restore from backup
                            self._restore_backup(backup_dir)
                            return False

                    tar.extractall(path=tmp_path)

                # The archive should contain a ``screenrecord/`` directory
                # (or the files directly). Detect and copy appropriately.
                extracted_pkg = tmp_path / "screenrecord"
                if extracted_pkg.is_dir():
                    source_dir = extracted_pkg
                else:
                    # Files were extracted flat into tmp_path; use tmp_path
                    # but skip the archive file itself.
                    source_dir = tmp_path

                # Overwrite existing files in install_dir with updated ones.
                for item in source_dir.iterdir():
                    if item.name == archive_name:
                        # Skip the archive itself if extracted flat.
                        continue
                    dest = self.install_dir / item.name
                    if item.is_dir():
                        if dest.exists():
                            shutil.rmtree(dest)
                        shutil.copytree(item, dest)
                    else:
                        shutil.copy2(item, dest)

            logger.info(
                "Updater: update to version %s applied successfully.",
                remote_version,
            )
            return True

        except Exception:
            logger.exception(
                "Updater: failed to extract/apply update. Restoring backup."
            )
            self._restore_backup(backup_dir)
            return False

    def check_and_apply(self) -> bool:
        """Convenience method: check for an update and apply it if available.

        This is the primary entry point called periodically by the main
        service loop.

        Returns:
            ``True`` if an update was applied (caller should restart),
            ``False`` otherwise.
        """
        try:
            if not self.check_for_update():
                return False
            return self.apply_update()
        except Exception:
            logger.exception("Updater: unexpected error during check_and_apply.")
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _restore_backup(self, backup_dir: Path) -> None:
        """Restore the installation from a backup directory.

        Args:
            backup_dir: Path to the backup created before the failed update.
        """
        try:
            if backup_dir.exists():
                # Remove the (possibly corrupted) install dir.
                if self.install_dir.exists():
                    shutil.rmtree(self.install_dir)
                shutil.copytree(backup_dir, self.install_dir)
                logger.info(
                    "Updater: restored installation from backup %s.", backup_dir
                )
        except Exception:
            logger.exception(
                "Updater: CRITICAL -- failed to restore backup from %s. "
                "Manual intervention required.",
                backup_dir,
            )
