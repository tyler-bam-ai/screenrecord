"""Diagnostic bundle collection and upload.

The managed macOS install needs to be debuggable without asking IT to run a
manual Terminal command. This module builds a small redacted zip of local logs
and runtime context, then uploads it to ``_diagnostics`` in the configured Drive
root folder.
"""

import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import traceback
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]
DIAGNOSTICS_FOLDER_NAME = "_diagnostics"
MAX_LOG_BYTES = 256 * 1024
STARTUP_MIN_INTERVAL_SECONDS = 24 * 60 * 60
BLOCKED_MIN_INTERVAL_SECONDS = 30 * 60


def upload_diagnostics_bundle(
    config: Optional[Dict[str, Any]],
    reason: str,
    *,
    min_interval_seconds: Optional[int] = None,
    error: Optional[Any] = None,
) -> Optional[str]:
    """Upload a redacted diagnostics zip to Google Drive.

    Returns the uploaded Drive file ID, or ``None`` when skipped/failed. This is
    deliberately best-effort and must never stop the recorder from starting.
    """
    zip_path: Optional[Path] = None
    try:
        data_dir = Path.home() / ".screenrecord"
        data_dir.mkdir(parents=True, exist_ok=True)
        config = config or _best_effort_config(data_dir)
        marker = data_dir / "last_diagnostics_upload.json"
        reason = _safe_token(reason or "unknown")
        if min_interval_seconds is None:
            min_interval_seconds = (
                STARTUP_MIN_INTERVAL_SECONDS
                if reason == "startup"
                else BLOCKED_MIN_INTERVAL_SECONDS
            )

        if _recent_attempt(marker, reason, min_interval_seconds):
            logger.info("Diagnostics upload skipped for '%s' (recent attempt).", reason)
            return None

        _write_marker(marker, {"reason": reason, "attempted_at": _now_iso()})
        zip_path = _build_bundle(config, reason, data_dir, error=error)
        file_id = _upload_zip(config, zip_path)

        _write_marker(
            marker,
            {
                "reason": reason,
                "attempted_at": _now_iso(),
                "uploaded_at": _now_iso(),
                "file_id": file_id,
                "local_path": str(zip_path),
            },
        )
        logger.info("Diagnostics uploaded for '%s' -> %s", reason, file_id)
        return file_id
    except Exception as exc:
        logger.exception("Diagnostics upload failed for '%s'.", reason)
        try:
            marker = Path.home() / ".screenrecord" / "last_diagnostics_upload.json"
            local_paths = (
                _expose_local_copy(zip_path)
                if zip_path
                else _write_emergency_failure_note(reason, exc)
            )
            _write_marker(
                marker,
                {
                    "reason": _safe_token(reason or "unknown"),
                    "attempted_at": _now_iso(),
                    "failed_at": _now_iso(),
                    "error": str(exc)[:500],
                    "local_path": str(zip_path) if zip_path else "",
                    "easy_copy_paths": local_paths,
                },
            )
        except Exception:
            pass
        return None


def record_early_failure(reason: str, error: Optional[Any] = None) -> Optional[str]:
    """Capture/upload diagnostics before the service object exists.

    This is used for config-load failures, app-entry crashes, and other cases
    that happen before ``ScreenRecordService`` can call its normal diagnostic
    hook. It never rate-limits: early failures are rare and each one matters.
    """
    data_dir = Path.home() / ".screenrecord"
    config = _best_effort_config(data_dir)
    return upload_diagnostics_bundle(
        config,
        f"early-{reason}",
        min_interval_seconds=0,
        error=error,
    )


def _build_bundle(
    config: Dict[str, Any],
    reason: str,
    data_dir: Path,
    *,
    error: Optional[Any] = None,
) -> Path:
    computer = _safe_token(str(config.get("computer_name") or socket.gethostname()))
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bundle_dir = data_dir / "diagnostics"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_old_bundles(bundle_dir)
    zip_path = bundle_dir / f"diagnostic_{computer}_{timestamp}_{reason}.zip"

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        _write_json(zf, "summary.json", _summary(config, reason))
        _write_text(zf, "config_redacted.yaml", _redacted_config_text(config))
        if error is not None:
            _write_text(zf, "error.txt", _error_text(error))
        _write_text(zf, "environment.txt", _environment_snapshot())
        _write_text(zf, "launchd.txt", _launchd_snapshot())
        _write_text(zf, "app_bundle.txt", _app_bundle_snapshot())
        _write_text(zf, "system_recent.txt", _system_log_snapshot())

        candidates = [
            (data_dir / "screenrecord.log", "screenrecord.log"),
            (data_dir / "early_startup.log", "early_startup.log"),
            (data_dir / "provision.log", "provision.log"),
            (data_dir / "install_diagnostic.txt", "install_diagnostic.txt"),
            (data_dir / "audit.log", "audit.log"),
            (Path("/tmp/ai.bam.screenrecord.stdout.log"), "launchd_stdout.log"),
            (Path("/tmp/ai.bam.screenrecord.stderr.log"), "launchd_stderr.log"),
            (Path("/var/tmp/ai.bam.screenrecord.postinstall.log"), "postinstall.log"),
        ]
        for path, arcname in candidates:
            _add_tail_if_exists(zf, path, arcname)

    return zip_path


def _best_effort_config(data_dir: Path) -> Dict[str, Any]:
    config_path = data_dir / "config.yaml"
    try:
        import yaml

        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception as exc:
        return {
            "computer_name": _detect_computer_name(),
            "employee_name": os.environ.get("USER") or os.environ.get("LOGNAME") or "",
            "client_name": "Unassigned",
            "diagnostic_note": f"Could not load config.yaml: {exc}",
            "google_drive": {
                "credentials_file": str(data_dir / "credentials.json"),
                "root_folder_id": "",
            },
            "google_sheets": {},
            "recording": {"output_dir": str(data_dir / "recordings")},
        }
    return {
        "computer_name": _detect_computer_name(),
        "employee_name": os.environ.get("USER") or os.environ.get("LOGNAME") or "",
        "client_name": "Unassigned",
        "google_drive": {
            "credentials_file": str(data_dir / "credentials.json"),
            "root_folder_id": "",
        },
        "google_sheets": {},
        "recording": {"output_dir": str(data_dir / "recordings")},
    }


def _expose_local_copy(zip_path: Optional[Path]) -> list:
    if zip_path is None or not zip_path.exists():
        return []
    copied = []
    target_dirs = [Path("/Users/Shared"), Path.home() / "Desktop", Path.home() / "Downloads"]
    for target_dir in target_dirs:
        if not target_dir.is_dir():
            continue
        target = target_dir / f"ScreenRecorder_{zip_path.name}"
        try:
            shutil.copy2(zip_path, target)
            try:
                target.chmod(0o644)
            except OSError:
                pass
            copied.append(str(target))
        except OSError:
            pass
    return copied


def _write_emergency_failure_note(reason: str, error: Any) -> list:
    text = (
        "ScreenRecorder could not build a diagnostics zip.\n\n"
        f"reason={reason}\n"
        f"created_at={_now_iso()}\n\n"
        + _error_text(error)
    )
    copied = []
    for target_dir in (Path("/Users/Shared"), Path.home() / "Desktop", Path.home() / "Downloads"):
        if not target_dir.is_dir():
            continue
        target = target_dir / f"ScreenRecorder_diagnostic_failure_{_safe_token(reason)}.txt"
        try:
            target.write_text(text, encoding="utf-8")
            target.chmod(0o644)
            copied.append(str(target))
        except OSError:
            pass
    return copied


def _cleanup_old_bundles(bundle_dir: Path, keep: int = 10) -> None:
    try:
        bundles = sorted(
            bundle_dir.glob("diagnostic_*.zip"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in bundles[keep:]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _summary(config: Dict[str, Any], reason: str) -> Dict[str, Any]:
    return {
        "reason": reason,
        "created_at": _now_iso(),
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "frozen": bool(getattr(sys, "frozen", False)),
        "cwd": os.getcwd(),
        "home": str(Path.home()),
        "user": os.environ.get("USER") or os.environ.get("LOGNAME") or "",
        "employee_name": config.get("employee_name", ""),
        "computer_name": config.get("computer_name", ""),
        "client_name": config.get("client_name", ""),
        "hostname": socket.gethostname(),
        "machine": platform.machine(),
        "macos": platform.mac_ver()[0] if sys.platform == "darwin" else "",
        "segment_duration": config.get("recording", {}).get("segment_duration", ""),
        "screenrecord_disable_updater": os.environ.get("SCREENRECORD_DISABLE_UPDATER", ""),
        "xpc_service_name": os.environ.get("XPC_SERVICE_NAME", ""),
        "diagnostic_note": config.get("diagnostic_note", ""),
    }


def _redacted_config_text(config: Dict[str, Any]) -> str:
    try:
        import yaml

        redacted = json.loads(json.dumps(config))
        if "google_drive" in redacted:
            redacted["google_drive"]["credentials_file"] = "<redacted path>"
            if redacted["google_drive"].get("root_folder_id"):
                redacted["google_drive"]["root_folder_id"] = "<set>"
        if "google_sheets" in redacted and redacted["google_sheets"].get("sheet_id"):
            redacted["google_sheets"]["sheet_id"] = "<set>"
        if "encryption" in redacted:
            redacted["encryption"]["key_file"] = "<redacted path>"
        return yaml.safe_dump(redacted, sort_keys=False, allow_unicode=True)
    except Exception:
        return "<could not render redacted config>\n"


def _environment_snapshot() -> str:
    lines = [
        f"created_at={_now_iso()}",
        f"platform={platform.platform()}",
        f"python={sys.version}",
        f"executable={sys.executable}",
        f"cwd={os.getcwd()}",
        f"home={Path.home()}",
        f"user={os.environ.get('USER', '')}",
        f"logname={os.environ.get('LOGNAME', '')}",
        f"xpc_service_name={os.environ.get('XPC_SERVICE_NAME', '')}",
        f"path={os.environ.get('PATH', '')}",
    ]
    for cmd in (
        ["sw_vers"],
        ["uname", "-a"],
        ["id"],
        ["scutil", "--get", "ComputerName"],
        ["df", "-h", str(Path.home())],
        ["ps", "axww", "-o", "pid,ppid,user,stat,command"],
        ["pkgutil", "--pkg-info", "ai.bam.screenrecord.pkg"],
    ):
        lines.append("")
        lines.append("$ " + " ".join(cmd))
        lines.append(_run(cmd))
    return "\n".join(lines) + "\n"


def _launchd_snapshot() -> str:
    if sys.platform != "darwin":
        return "launchd snapshot unavailable off macOS\n"
    label = os.environ.get("XPC_SERVICE_NAME") or "ai.bam.screenrecord"
    if "/" in label:
        label = "ai.bam.screenrecord"
    uid = str(os.getuid())
    return _run(["launchctl", "print", f"gui/{uid}/{label}"], timeout=5)


def _app_bundle_snapshot() -> str:
    app = Path("/Applications/ScreenRecorder.app")
    executable = app / "Contents" / "MacOS" / "ScreenRecorder"
    lines = [f"created_at={_now_iso()}", f"app_exists={app.exists()}"]
    for cmd in (
        ["ls", "-la", "/Applications/ScreenRecorder.app"],
        ["plutil", "-p", "/Applications/ScreenRecorder.app/Contents/Info.plist"],
        ["codesign", "-dv", "--verbose=4", "/Applications/ScreenRecorder.app"],
        ["spctl", "-a", "-vv", "/Applications/ScreenRecorder.app"],
        ["file", str(executable)],
    ):
        lines.append("")
        lines.append("$ " + " ".join(cmd))
        lines.append(_run(cmd, timeout=8))
    return "\n".join(lines) + "\n"


def _system_log_snapshot() -> str:
    if sys.platform != "darwin":
        return "system log snapshot unavailable off macOS\n"
    predicate = (
        'process == "ScreenRecorder" OR process == "ffmpeg" OR '
        'eventMessage CONTAINS[c] "ai.bam.screenrecord" OR '
        'eventMessage CONTAINS[c] "ScreenRecorder"'
    )
    return _run(
        ["log", "show", "--last", "30m", "--style", "syslog", "--predicate", predicate],
        timeout=12,
    )


def _upload_zip(config: Dict[str, Any], zip_path: Path) -> str:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    drive_cfg = config.get("google_drive") or {}
    credentials_file = drive_cfg.get("credentials_file") or ""
    root_folder_id = drive_cfg.get("root_folder_id") or ""
    if not credentials_file or not root_folder_id:
        raise RuntimeError("Diagnostics upload unavailable: missing Drive credentials/root folder.")
    if not Path(credentials_file).is_file():
        raise RuntimeError(f"Diagnostics upload unavailable: credentials file missing at {credentials_file}.")
    creds = Credentials.from_service_account_file(
        credentials_file, scopes=SCOPES
    )
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    diagnostics_id = _find_or_create_folder(service, DIAGNOSTICS_FOLDER_NAME, root_folder_id)
    client = _safe_drive_name(str(config.get("client_name") or "Unassigned"))
    computer = _safe_drive_name(str(config.get("computer_name") or "Unknown"))
    client_id = _find_or_create_folder(service, client, diagnostics_id)
    computer_id = _find_or_create_folder(service, computer, client_id)

    metadata = {"name": zip_path.name, "parents": [computer_id]}
    media = MediaFileUpload(
        str(zip_path),
        mimetype="application/zip",
        resumable=True,
        chunksize=2 * 1024 * 1024,
    )
    request = service.files().create(
        body=metadata,
        media_body=media,
        fields="id",
        supportsAllDrives=True,
    )
    response = None
    while response is None:
        _, response = request.next_chunk()
    file_id = response.get("id")
    if not file_id:
        raise RuntimeError("Diagnostics upload did not return a file ID.")
    return file_id


def _find_or_create_folder(service: Any, name: str, parent_id: str) -> str:
    safe_name = _drive_query_literal(name)
    safe_parent = _drive_query_literal(parent_id)
    query = (
        f"name={safe_name} and {safe_parent} in parents "
        "and mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    try:
        results = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                fields="files(id, name)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = results.get("files", [])
        if files:
            return files[0]["id"]
    except Exception:
        logger.exception("Error searching for diagnostics folder '%s'.", name)
        raise

    folder = (
        service.files()
        .create(
            body={
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            },
            fields="id",
            supportsAllDrives=True,
        )
        .execute()
    )
    return folder["id"]


def _add_tail_if_exists(zf: zipfile.ZipFile, path: Path, arcname: str) -> None:
    try:
        if not path.exists() or not path.is_file():
            return
        with path.open("rb") as fh:
            try:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - MAX_LOG_BYTES))
            except OSError:
                pass
            data = fh.read(MAX_LOG_BYTES)
        zf.writestr(arcname, data)
    except Exception as exc:
        zf.writestr(f"{arcname}.error.txt", f"Could not read {path}: {exc}\n")


def _write_json(zf: zipfile.ZipFile, arcname: str, data: Dict[str, Any]) -> None:
    zf.writestr(arcname, json.dumps(data, indent=2, sort_keys=True) + "\n")


def _write_text(zf: zipfile.ZipFile, arcname: str, text: str) -> None:
    zf.writestr(arcname, text)


def _run(cmd: list, timeout: int = 3) -> str:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return output[-20000:] if output else f"(exit {result.returncode}, no output)"
    except Exception as exc:
        return f"ERROR: {exc}"


def _error_text(error: Any) -> str:
    if isinstance(error, BaseException):
        return "".join(traceback.format_exception(type(error), error, error.__traceback__))
    return str(error)


def _detect_computer_name() -> str:
    if sys.platform == "darwin":
        for key in ("LocalHostName", "ComputerName"):
            value = _run(["scutil", "--get", key], timeout=2).strip()
            if value and not value.startswith("ERROR:") and not value.startswith("(exit"):
                return value
    return socket.gethostname().split(".")[0]


def _recent_attempt(marker: Path, reason: str, min_interval_seconds: int) -> bool:
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        if data.get("reason") != reason:
            return False
        attempted = data.get("attempted_at") or data.get("uploaded_at")
        if not attempted:
            return False
        ts = datetime.fromisoformat(str(attempted).replace("Z", "+00:00"))
        return time.time() - ts.timestamp() < min_interval_seconds
    except Exception:
        return False


def _write_marker(marker: Path, data: Dict[str, Any]) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_token(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return value.strip("-")[:80] or "unknown"


def _safe_drive_name(value: str) -> str:
    return re.sub(r"[\r\n/]+", "-", value.strip())[:120] or "Unknown"


def _drive_query_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
