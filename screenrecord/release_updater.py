"""Manifest-based updater for managed packaged builds.

macOS package replacement is handled by the root LaunchDaemon installed by the
pkg. Windows can update itself in the user's profile: the agent downloads a new
ScreenRecorder.exe, verifies the hash, exits, and a detached PowerShell helper
swaps the executable and restarts it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from .version import current_platform_version

logger = logging.getLogger(__name__)

DEFAULT_WINDOWS_MANIFEST_URL = (
    "https://github.com/tyler-bam-ai/screenrecord/releases/download/"
    "windows-latest/update-windows.json"
)
DEFAULT_MAC_TRIGGER_PATH = Path("/Users/Shared/ScreenRecorder_update_now")
STATUS_FILENAME = "updater_status.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _data_dir() -> Path:
    return Path.home() / ".screenrecord"


def _version_parts(value: str) -> tuple:
    parts = []
    for raw in str(value or "").replace("-", ".").split("."):
        try:
            parts.append(int("".join(ch for ch in raw if ch.isdigit()) or "0"))
        except ValueError:
            parts.append(0)
    return tuple(parts + [0] * (4 - len(parts)))


def _remote_is_newer(remote: str, local: str) -> bool:
    return _version_parts(remote) > _version_parts(local)


def read_update_status(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the most recent updater status for heartbeat/diagnostics."""
    candidates = [_data_dir() / STATUS_FILENAME]
    if sys.platform == "darwin":
        candidates = [
            Path("/Users/Shared/ScreenRecorder/ScreenRecorder_updater_status.json"),
            Path("/Library/Logs/ScreenRecorder/updater_status.json"),
            Path("/Users/Shared/ScreenRecorder_updater_status.json"),
        ] + candidates
    for path in candidates:
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
        except Exception:
            logger.debug("Could not read updater status %s", path, exc_info=True)
    return {}


class ReleaseUpdater:
    """Check release manifests and stage/apply packaged updates."""

    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.platform = "windows" if sys.platform == "win32" else (
            "mac" if sys.platform == "darwin" else sys.platform
        )
        self.local_version = current_platform_version()
        updater_cfg = config.get("updater", {}) if isinstance(config.get("updater"), dict) else {}
        self.manifest_url = updater_cfg.get("manifest_url") or DEFAULT_WINDOWS_MANIFEST_URL
        self.check_interval_seconds = int(updater_cfg.get("check_interval_seconds", 3600) or 3600)
        self._staged_script: Optional[Path] = None
        self._status_path = _data_dir() / STATUS_FILENAME

    @staticmethod
    def enabled_for_platform(config: Dict[str, Any]) -> bool:
        updater_cfg = config.get("updater", {}) if isinstance(config.get("updater"), dict) else {}
        if updater_cfg.get("enabled") is False:
            return False
        return sys.platform == "win32"

    @staticmethod
    def request_external_update() -> str:
        """Ask the macOS root helper to check immediately."""
        if sys.platform != "darwin":
            return ""
        DEFAULT_MAC_TRIGGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        DEFAULT_MAC_TRIGGER_PATH.write_text(_now_iso() + "\n", encoding="utf-8")
        try:
            DEFAULT_MAC_TRIGGER_PATH.chmod(0o666)
        except OSError:
            pass
        return str(DEFAULT_MAC_TRIGGER_PATH)

    def check_and_stage(self, *, force: bool = False) -> bool:
        """Return True when an update was staged and should now be applied."""
        if self.platform != "windows":
            self._write_status("skipped", "ReleaseUpdater self-apply is Windows-only.")
            return False

        self._write_status("checking", "Checking for update.")
        manifest = self._fetch_manifest()
        if not manifest:
            self._write_status("check_failed", "Could not fetch update manifest.")
            return False

        remote_version = str(manifest.get("version") or "")
        url = str(manifest.get("url") or "")
        sha256 = str(manifest.get("sha256") or "").lower()
        manifest_force = bool(manifest.get("force", False))

        if not remote_version or not url or not sha256:
            self._write_status(
                "manifest_invalid",
                "Manifest is missing version, url, or sha256.",
                remote_version=remote_version,
            )
            return False

        if not (force or manifest_force or _remote_is_newer(remote_version, self.local_version)):
            self._write_status(
                "up_to_date",
                f"Already at {self.local_version}.",
                remote_version=remote_version,
            )
            return False

        new_exe = self._download_and_verify(remote_version, url, sha256)
        if not new_exe:
            return False

        script = self._write_apply_script(new_exe, remote_version)
        self._staged_script = script
        self._write_status(
            "staged",
            f"Update {remote_version} staged.",
            remote_version=remote_version,
            staged_path=str(new_exe),
        )
        return True

    def launch_staged_update(self) -> bool:
        """Launch the staged Windows swapper script. Caller should then exit."""
        if self.platform != "windows" or self._staged_script is None:
            return False
        current_exe = Path(sys.executable).resolve()
        log_path = _data_dir() / "windows_updater.log"
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self._staged_script),
            "-TargetPid",
            str(os.getpid()),
            "-TargetExe",
            str(current_exe),
            "-LogPath",
            str(log_path),
        ]
        flags = 0
        startupinfo = None
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
        try:
            subprocess.Popen(
                cmd,
                close_fds=True,
                creationflags=flags,
                startupinfo=startupinfo,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._write_status("applying", "Updater helper launched; process will exit.")
            return True
        except Exception as exc:
            logger.exception("Failed to launch updater helper")
            self._write_status("apply_launch_failed", str(exc)[:500])
            return False

    def _fetch_manifest(self) -> Dict[str, Any]:
        try:
            req = Request(
                self.manifest_url,
                headers={"User-Agent": "BAM-AI-ScreenRecorder-ReleaseUpdater"},
            )
            with urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data if isinstance(data, dict) else {}
        except (URLError, OSError, json.JSONDecodeError) as exc:
            logger.warning("Update manifest fetch failed: %s", exc)
            return {}

    def _download_and_verify(self, version: str, url: str, expected_sha: str) -> Optional[Path]:
        updates_dir = _data_dir() / "updates"
        updates_dir.mkdir(parents=True, exist_ok=True)
        target = updates_dir / f"ScreenRecorder-{version}.exe"
        tmp = updates_dir / f".ScreenRecorder-{version}.{os.getpid()}.tmp"
        try:
            self._write_status("downloading", f"Downloading update {version}.", remote_version=version)
            req = Request(url, headers={"User-Agent": "BAM-AI-ScreenRecorder-ReleaseUpdater"})
            hasher = hashlib.sha256()
            with urlopen(req, timeout=600) as resp, tmp.open("wb") as fh:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    fh.write(chunk)
            actual = hasher.hexdigest().lower()
            if actual != expected_sha:
                self._write_status(
                    "hash_mismatch",
                    "Downloaded update did not match manifest hash.",
                    remote_version=version,
                    expected_sha256=expected_sha,
                    actual_sha256=actual,
                )
                tmp.unlink(missing_ok=True)
                return None
            tmp.replace(target)
            return target
        except Exception as exc:
            logger.exception("Failed to download/verify update")
            self._write_status("download_failed", str(exc)[:500], remote_version=version)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return None

    def _write_apply_script(self, new_exe: Path, version: str) -> Path:
        updates_dir = _data_dir() / "updates"
        script = updates_dir / "apply_screenrecorder_update.ps1"
        body = f"""param(
  [Parameter(Mandatory=$true)][int]$TargetPid,
  [Parameter(Mandatory=$true)][string]$TargetExe,
  [Parameter(Mandatory=$true)][string]$LogPath
)
$ErrorActionPreference = "Stop"
function Log($m) {{
  $dir = Split-Path -Parent $LogPath
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  Add-Content -Path $LogPath -Value ("$(Get-Date -Format o) " + $m)
}}
$NewExe = "{str(new_exe)}"
$status = Join-Path (Split-Path -Parent $LogPath) "updater_status.json"
try {{
  Log "Applying ScreenRecorder update {version} to $TargetExe"
  try {{ Wait-Process -Id $TargetPid -Timeout 90 -ErrorAction SilentlyContinue }} catch {{ }}
  if (Get-Process -Id $TargetPid -ErrorAction SilentlyContinue) {{
    Log "Existing process still alive after wait; stopping it."
    Stop-Process -Id $TargetPid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
  }}
  Stop-Process -Name ScreenRecorder -Force -ErrorAction SilentlyContinue
  Stop-Process -Name ffmpeg -Force -ErrorAction SilentlyContinue
  Copy-Item -LiteralPath $NewExe -Destination $TargetExe -Force
  Start-Process -FilePath $TargetExe
  @{{status="updated"; version="{version}"; updated_at=(Get-Date).ToUniversalTime().ToString("o"); message="Updated and restarted."}} |
    ConvertTo-Json -Compress | Set-Content -Path $status -Encoding UTF8
  Log "Update {version} applied and restarted."
}} catch {{
  Log ("Update failed: " + $_.Exception.Message)
  @{{status="apply_failed"; version="{version}"; failed_at=(Get-Date).ToUniversalTime().ToString("o"); message=$_.Exception.Message}} |
    ConvertTo-Json -Compress | Set-Content -Path $status -Encoding UTF8
  try {{ Start-Process -FilePath $TargetExe }} catch {{ }}
  throw
}}
"""
        script.write_text(body, encoding="utf-8")
        return script

    def _write_status(self, status: str, message: str, **extra: Any) -> None:
        payload: Dict[str, Any] = {
            "status": status,
            "message": message,
            "platform": self.platform,
            "local_version": self.local_version,
            "last_checked": _now_iso(),
            "manifest_url": self.manifest_url,
        }
        payload.update(extra)
        try:
            self._status_path.parent.mkdir(parents=True, exist_ok=True)
            self._status_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            logger.debug("Could not write updater status", exc_info=True)
