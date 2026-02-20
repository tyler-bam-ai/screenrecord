"""Auto-update module that pulls latest code from GitHub.

On startup and every hour, checks the GitHub repo for new commits on the
main branch. If a newer commit is found, downloads the repo zip, extracts
the updated ``screenrecord/`` package and root-level config files, and
signals for an automatic restart.

No manual packaging or uploading required — every ``git push`` to main
auto-deploys to all running agents.

Safety guarantees:
    * A timestamped backup of the current installation is kept before applying.
    * All errors are caught so a failed update never crashes the recorder.
    * Local data (config.yaml, credentials, keys, recordings, logs) is preserved.
"""

import json
import logging
import os
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# GitHub repository coordinates
GITHUB_OWNER = "tyler-bam-ai"
GITHUB_REPO = "screenrecord"
GITHUB_BRANCH = "main"

GITHUB_API_URL = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/commits/{GITHUB_BRANCH}"
)
GITHUB_ZIP_URL = (
    f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/archive/refs/heads/{GITHUB_BRANCH}.zip"
)

# Files/dirs that should NEVER be overwritten by an update
PRESERVE = frozenset({
    "config.yaml",
    "credentials.json",
    "encryption.key",
    "recordings",
    "logs",
    "audit.log",
    "consent_records.json",
    "rag_db",
    "python",
    "bin",
    ".commit_sha",
})


class UpdateChecker:
    """Checks for and applies updates from GitHub.

    Usage::

        updater = UpdateChecker(config)
        if updater.check_and_apply():
            # update was applied — restart the service
            ...

    The *config* dictionary should contain:

    * ``install_dir`` (optional) — path to ``~/.screenrecord``;
      defaults to the parent of the directory containing this module.
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        # The install directory is the parent of the screenrecord/ package.
        # e.g. if this file is ~/.screenrecord/screenrecord/updater.py,
        # then install_dir is ~/.screenrecord/
        default_install = Path(__file__).resolve().parent.parent
        self.install_dir = Path(config.get("install_dir", default_install))
        self._sha_file = self.install_dir / ".commit_sha"
        self._local_sha = self._read_local_sha()

        logger.info(
            "Updater: initialized (install_dir=%s, local_sha=%s)",
            self.install_dir,
            self._local_sha[:8] if self._local_sha else "none",
        )

    # ------------------------------------------------------------------
    # SHA tracking
    # ------------------------------------------------------------------

    def _read_local_sha(self) -> str:
        """Read the locally stored commit SHA."""
        try:
            if self._sha_file.exists():
                return self._sha_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        return ""

    def _write_local_sha(self, sha: str) -> None:
        """Persist the current commit SHA to disk."""
        try:
            self._sha_file.write_text(sha + "\n", encoding="utf-8")
            self._local_sha = sha
        except OSError:
            logger.warning("Updater: could not write SHA file.")

    # ------------------------------------------------------------------
    # GitHub helpers
    # ------------------------------------------------------------------

    def _get_remote_sha(self) -> Optional[str]:
        """Fetch the latest commit SHA from GitHub API.

        Returns:
            The 40-character commit SHA, or None on failure.
        """
        try:
            req = Request(GITHUB_API_URL, headers={
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "BAM-AI-ScreenRecorder-Updater",
            })
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                sha = data.get("sha", "")
                if sha:
                    return sha
        except (URLError, json.JSONDecodeError, KeyError, OSError) as exc:
            logger.debug("Updater: could not fetch remote SHA: %s", exc)
        return None

    def _download_zip(self) -> Optional[bytes]:
        """Download the repo zip from GitHub.

        Returns:
            Raw zip bytes, or None on failure.
        """
        try:
            req = Request(GITHUB_ZIP_URL, headers={
                "User-Agent": "BAM-AI-ScreenRecorder-Updater",
            })
            with urlopen(req, timeout=120) as resp:
                return resp.read()
        except (URLError, OSError) as exc:
            logger.error("Updater: failed to download zip: %s", exc)
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_for_update(self) -> bool:
        """Check whether a newer commit exists on GitHub.

        Returns:
            True if an update is available, False otherwise.
        """
        remote_sha = self._get_remote_sha()
        if remote_sha is None:
            logger.debug("Updater: could not reach GitHub; skipping check.")
            return False

        if remote_sha == self._local_sha:
            logger.info("Updater: already up to date (%s).", remote_sha[:8])
            return False

        logger.info(
            "Updater: new commit available (%s -> %s).",
            self._local_sha[:8] if self._local_sha else "none",
            remote_sha[:8],
        )
        self._pending_sha = remote_sha
        return True

    def apply_update(self) -> bool:
        """Download and apply the latest code from GitHub.

        1. Downloads the repo zip.
        2. Creates a timestamped backup of the screenrecord/ package.
        3. Extracts updated files, preserving local data.
        4. Saves the new commit SHA.

        Returns:
            True if the update was applied successfully, False otherwise.
        """
        pending_sha = getattr(self, "_pending_sha", None)
        if not pending_sha:
            logger.error("Updater: apply_update called without a pending update.")
            return False

        # ----------------------------------------------------------
        # 1. Download the zip
        # ----------------------------------------------------------
        logger.info("Updater: downloading update from GitHub...")
        zip_bytes = self._download_zip()
        if zip_bytes is None:
            return False
        logger.info("Updater: downloaded %.1f MB.", len(zip_bytes) / 1024 / 1024)

        # ----------------------------------------------------------
        # 2. Backup current screenrecord/ package
        # ----------------------------------------------------------
        pkg_dir = self.install_dir / "screenrecord"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = self.install_dir / f"_backup_{timestamp}"

        if pkg_dir.exists():
            try:
                shutil.copytree(pkg_dir, backup_dir / "screenrecord")
                logger.info("Updater: backed up to %s.", backup_dir)
            except Exception:
                logger.exception("Updater: failed to create backup. Aborting.")
                return False

        # ----------------------------------------------------------
        # 3. Extract updated files
        # ----------------------------------------------------------
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                zip_file = tmp_path / "repo.zip"
                zip_file.write_bytes(zip_bytes)

                with zipfile.ZipFile(zip_file, "r") as zf:
                    zf.extractall(tmp_path)

                # GitHub zips contain a top-level folder like screenrecord-main/
                extracted_dirs = [
                    d for d in tmp_path.iterdir()
                    if d.is_dir() and d.name != "__MACOSX"
                ]
                if not extracted_dirs:
                    logger.error("Updater: zip extraction produced no directories.")
                    self._restore_backup(backup_dir, pkg_dir)
                    return False

                repo_root = extracted_dirs[0]

                # Copy the screenrecord/ package (the Python code)
                new_pkg = repo_root / "screenrecord"
                if new_pkg.is_dir():
                    if pkg_dir.exists():
                        shutil.rmtree(pkg_dir)
                    shutil.copytree(new_pkg, pkg_dir)
                    logger.info("Updater: replaced screenrecord/ package.")
                else:
                    logger.error("Updater: no screenrecord/ dir found in zip.")
                    self._restore_backup(backup_dir, pkg_dir)
                    return False

                # Copy root-level files that aren't in the PRESERVE list
                # (e.g. requirements.txt, requirements-core.txt, etc.)
                for item in repo_root.iterdir():
                    if item.name in PRESERVE:
                        continue
                    if item.name == "screenrecord":
                        continue  # already handled above
                    if item.name.startswith("."):
                        continue  # skip .git, .gitignore, etc.
                    dest = self.install_dir / item.name
                    if item.is_dir():
                        if dest.exists():
                            shutil.rmtree(dest)
                        shutil.copytree(item, dest)
                    else:
                        shutil.copy2(item, dest)

                logger.info("Updater: root-level files updated.")

            # ----------------------------------------------------------
            # 4. Save new SHA
            # ----------------------------------------------------------
            self._write_local_sha(pending_sha)
            logger.info(
                "Updater: update applied successfully (now at %s).",
                pending_sha[:8],
            )

            # Clean up old backups (keep last 3)
            self._cleanup_old_backups()

            return True

        except Exception:
            logger.exception("Updater: failed to apply update. Restoring backup.")
            self._restore_backup(backup_dir, pkg_dir)
            return False

    def check_and_apply(self) -> bool:
        """Check for an update and apply it if available.

        This is the primary entry point called by the main service loop.

        Returns:
            True if an update was applied (caller should restart).
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

    def _restore_backup(self, backup_dir: Path, pkg_dir: Path) -> None:
        """Restore the screenrecord/ package from a backup."""
        try:
            backup_pkg = backup_dir / "screenrecord"
            if backup_pkg.exists():
                if pkg_dir.exists():
                    shutil.rmtree(pkg_dir)
                shutil.copytree(backup_pkg, pkg_dir)
                logger.info("Updater: restored from backup %s.", backup_dir)
        except Exception:
            logger.exception(
                "Updater: CRITICAL — failed to restore backup. "
                "Manual intervention required."
            )

    def _cleanup_old_backups(self, keep: int = 3) -> None:
        """Remove old backup directories, keeping only the most recent *keep*."""
        try:
            backups = sorted(
                [
                    d for d in self.install_dir.iterdir()
                    if d.is_dir() and d.name.startswith("_backup_")
                ],
                key=lambda p: p.name,
                reverse=True,
            )
            for old in backups[keep:]:
                shutil.rmtree(old)
                logger.debug("Updater: removed old backup %s.", old.name)
        except Exception:
            logger.debug("Updater: could not clean up old backups.")
