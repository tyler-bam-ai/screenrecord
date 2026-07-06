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
    # Truncate any pre-release/build suffix ("1.0.25-rc2" -> "1.0.25") so a
    # tagged manifest version and the clean baked version normalize EQUAL after
    # a successful swap. Otherwise the '-rc2' leaked into the tuple and
    # _remote_is_newer stayed True forever -> the update loop never terminated.
    head = str(value or "").split("-", 1)[0].split("+", 1)[0]
    parts = []
    for raw in head.split("."):
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

        # Honor manifest `force` ONLY when it points at a version different from
        # what we're running. A `force:true` left set on the manifest for the
        # CURRENT version would otherwise re-download + re-swap + restart every
        # cycle forever (the same hourly-loop symptom, config-triggered).
        version_differs = _version_parts(remote_version) != _version_parts(self.local_version)
        should_update = (
            force
            or _remote_is_newer(remote_version, self.local_version)
            or (manifest_force and version_differs)
        )
        if not should_update:
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
        # Resolve powershell.exe by absolute path — a background onefile agent may
        # have a stripped PATH, and a bare "powershell.exe" that fails to resolve
        # was a plausible reason the helper never ran (no windows_updater.log,
        # status stuck at "applying").
        powershell = os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"),
            "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
        )
        if not os.path.exists(powershell):
            powershell = "powershell.exe"
        cmd = [
            powershell,
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
        # Python-side breadcrumb: proves the launch was reached even if PowerShell
        # never starts (helps distinguish "helper didn't run" from "copy failed").
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(f"{_now_iso()} [python] launching helper via {powershell}\n")
        except OSError:
            pass
        flags = 0
        startupinfo = None
        if os.name == "nt":
            # DETACHED_PROCESS + NEW_PROCESS_GROUP + BREAKAWAY_FROM_JOB so the helper
            # survives this process's os._exit and any job object it lives in.
            # (CREATE_NO_WINDOW is intentionally NOT set — it is mutually exclusive
            # with DETACHED_PROCESS; STARTF_USESHOWWINDOW below already hides the
            # window.)
            flags = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
            )
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
        try:
            try:
                proc = subprocess.Popen(
                    cmd,
                    close_fds=True,
                    creationflags=flags,
                    startupinfo=startupinfo,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                # BREAKAWAY_FROM_JOB fails if the job forbids it; retry without it.
                flags &= ~0x01000000
                proc = subprocess.Popen(
                    cmd,
                    close_fds=True,
                    creationflags=flags,
                    startupinfo=startupinfo,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            self._write_status(
                "applying", "Updater helper launched; process will exit.",
                helper_pid=proc.pid,
            )
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
        # A running PyInstaller onefile .exe keeps ScreenRecorder.exe locked, so it
        # CANNOT be overwritten (the old `Copy-Item -Force` silently failed and the
        # helper relaunched the stale exe -> the hourly update loop). Windows *does*
        # allow a locked exe to be RENAMED, so we move the old exe aside, then copy
        # the new one into the freed path. Every step retries, verifies, and — most
        # importantly — restores the backup if the copy fails, so a machine can
        # never be left with no exe.
        # Emit NewExe/Version as SINGLE-quoted PowerShell literals (with '' escaping)
        # so a '$' or backtick in the install path (e.g. C:\\Users\\$name\\...) is
        # NOT expanded, and a stray quote can't break the script. TargetExe/LogPath
        # arrive via argv and are already safe.
        new_exe_lit = str(new_exe).replace("'", "''")
        version_lit = str(version).replace("'", "''")
        body = f"""param(
  [Parameter(Mandatory=$true)][int]$TargetPid,
  [Parameter(Mandatory=$true)][string]$TargetExe,
  [Parameter(Mandatory=$true)][string]$LogPath
)
$ErrorActionPreference = "Stop"
$NewExe    = '{new_exe_lit}'
$Version   = '{version_lit}'
$StatusDir = Split-Path -Parent $LogPath
$Status    = Join-Path $StatusDir "updater_status.json"
$Backup    = "$TargetExe.$PID.old"

function Log($m) {{
  try {{
    New-Item -ItemType Directory -Force -Path $StatusDir | Out-Null
    Add-Content -Path $LogPath -Value ("$(Get-Date -Format o) " + $m)
  }} catch {{ }}
}}
function Write-Status($s, $msg) {{
  try {{
    @{{status=$s; version=$Version; at=(Get-Date).ToUniversalTime().ToString("o"); message=$msg}} |
      ConvertTo-Json -Compress | Set-Content -Path $Status -Encoding UTF8
  }} catch {{ }}
}}
function Stop-Recorder {{
  Get-Process -Name ScreenRecorder -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
  Get-Process -Name ffmpeg -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}}

try {{
  Log "Applying ScreenRecorder update $Version to $TargetExe (new=$NewExe)"

  # 1. Wait for the launching process to exit, then force it if needed.
  try {{ Wait-Process -Id $TargetPid -Timeout 90 -ErrorAction SilentlyContinue }} catch {{ }}
  if (Get-Process -Id $TargetPid -ErrorAction SilentlyContinue) {{
    Log "PID $TargetPid still alive after wait; forcing stop."
    Stop-Process -Id $TargetPid -Force -ErrorAction SilentlyContinue
  }}

  # 2. Kill any lingering/relaunched instances so the exe file unlocks.
  for ($i = 0; $i -lt 10; $i++) {{
    Stop-Recorder
    Start-Sleep -Milliseconds 500
    if (-not (Get-Process -Name ScreenRecorder -ErrorAction SilentlyContinue)) {{ break }}
  }}

  if (-not (Test-Path -LiteralPath $NewExe)) {{ throw "Staged exe missing: $NewExe" }}
  $srcLen = (Get-Item -LiteralPath $NewExe).Length

  # Sweep any stale backups from earlier failed runs (unique per-PID names mean
  # a locked leftover can never collide with or block this run's rename).
  Get-ChildItem -LiteralPath (Split-Path -Parent $TargetExe) -Filter ((Split-Path -Leaf $TargetExe) + ".*.old") -ErrorAction SilentlyContinue |
    ForEach-Object {{ Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }}

  # 3. Rename-then-replace: move the (possibly still-locked) exe aside first.
  $moved = $false
  for ($i = 0; $i -lt 30; $i++) {{
    try {{
      if (Test-Path -LiteralPath $TargetExe) {{ Move-Item -LiteralPath $TargetExe -Destination $Backup -Force }}
      $moved = $true; break
    }} catch {{ Stop-Recorder; Start-Sleep -Seconds 1 }}
  }}
  if (-not $moved) {{ throw "Could not move existing exe aside after retries." }}

  # 4. Copy the new exe into place, then verify size INSIDE the try so a
  #    partial/corrupt write counts as a failure. On ANY failure, clear whatever
  #    landed at the target and restore the backup -> the target path is NEVER
  #    left empty or truncated.
  try {{
    Copy-Item -LiteralPath $NewExe -Destination $TargetExe -Force
    $dstLen = (Get-Item -LiteralPath $TargetExe).Length
    if ($dstLen -ne $srcLen) {{ throw "Post-copy size mismatch ($dstLen != $srcLen)." }}
  }} catch {{
    Log ("Copy/verify failed: " + $_.Exception.Message + " - restoring previous exe.")
    Remove-Item -LiteralPath $TargetExe -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $Backup) {{ Move-Item -LiteralPath $Backup -Destination $TargetExe -Force }}
    throw
  }}

  # 6. Restart the new exe and record success.
  Start-Process -FilePath $TargetExe
  Write-Status "updated" "Updated to $Version and restarted."
  Log "Update $Version applied and restarted."

  # 7. Best-effort cleanup of the old exe (may still be memory-mapped; ignore).
  Remove-Item -LiteralPath $Backup -Force -ErrorAction SilentlyContinue
}}
catch {{
  Log ("Update failed: " + $_.Exception.Message)
  Write-Status "apply_failed" $_.Exception.Message
  # Guarantee SOMETHING runnable is started again.
  try {{
    if (Test-Path -LiteralPath $TargetExe) {{ Start-Process -FilePath $TargetExe }}
    elseif (Test-Path -LiteralPath $Backup) {{ Start-Process -FilePath $Backup }}
  }} catch {{ }}
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
