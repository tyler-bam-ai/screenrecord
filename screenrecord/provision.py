"""First-run self-provisioning.

The pkg's postinstall writes the per-user config at install time, but that only
works if a user is logged in *and* the console-user detection succeeds — which
it doesn't in some MDM/root contexts. So the agent also provisions itself: on
startup, if ``~/.screenrecord/config.yaml`` is missing, it writes the config,
credentials, and key from values baked into the app bundle. Because the agent
runs *as the user* at login, this always has the right home dir and never
depends on install-time state. Idempotent and safe.
"""

import base64
import json
import logging
import os
import socket
import subprocess
import sys
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_CONFIG_TEMPLATE = """client_name: "{client}"
employee_name: "{employee}"
computer_name: "{computer}"

recording:
  fps: 5
  crf: 28
  segment_duration: 3600
  output_dir: "{dir}/recordings"
  audio_device: ""

google_drive:
  credentials_file: "{dir}/credentials.json"
  root_folder_id: "{folder}"

encryption:
  key_file: "{dir}/encryption.key"

analysis:
  enabled: false

google_sheets:
  sheet_id: "{sheet}"

rag:
  enabled: false
"""


def _diag(message: str) -> None:
    """Best-effort early provisioning log, before app logging is configured."""
    try:
        dir_ = Path.home() / ".screenrecord"
        dir_.mkdir(parents=True, exist_ok=True)
        with open(dir_ / "provision.log", "a", encoding="utf-8") as fh:
            fh.write(message.rstrip() + "\n")
    except Exception:
        pass


def _mac_scutil_value(key: str) -> str:
    if sys.platform != "darwin":
        return ""
    try:
        result = subprocess.run(
            ["scutil", "--get", key],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _detected_employee() -> str:
    employee = os.environ.get("USER") or os.environ.get("LOGNAME") or "User"
    try:
        import pwd
        full = pwd.getpwnam(employee).pw_gecos.split(",")[0].strip()
        if full:
            employee = full
    except Exception:
        pass
    return employee


def _detected_computer() -> str:
    return (
        _mac_scutil_value("LocalHostName")
        or _mac_scutil_value("ComputerName")
        or socket.gethostname().split(".")[0]
    )


def _valid_key_file(path: Path) -> bool:
    try:
        return len(base64.b64decode(path.read_bytes().strip(), validate=True)) == 32
    except Exception:
        return False


def _load_existing_config(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_baked_files(dir_: Path, vals: dict) -> None:
    """Refresh credentials/key from the signed bundle.

    Old installer attempts can leave partial or raw-key files behind. The baked
    bundle values are the source of truth for managed installs, so refreshing
    them on launch is safer than trusting whatever state a previous failed
    install left in the user's home directory.
    """
    credentials = dir_ / "credentials.json"
    key = dir_ / "encryption.key"
    for p in (credentials, key):
        if p.exists():
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
    credentials.write_bytes(base64.b64decode(vals["gcreds_b64"]))
    if not key.exists() or not _valid_key_file(key):
        key.write_bytes(base64.b64decode(vals["enckey_b64"]))
    try:
        os.chmod(credentials, 0o400)
        os.chmod(key, 0o400)
    except OSError:
        pass


def _normalise_config(existing: dict, dir_: Path, vals: dict) -> dict:
    employee = existing.get("employee_name") or _detected_employee()
    if str(employee).strip().lower() in ("", "unknown", "user"):
        employee = _detected_employee()

    computer = existing.get("computer_name") or _detected_computer()
    if str(computer).strip().lower() in ("", "unknown"):
        computer = _detected_computer()

    rec = existing.get("recording") if isinstance(existing.get("recording"), dict) else {}
    return {
        "client_name": existing.get("client_name") or vals.get("client", "Unassigned"),
        "employee_name": employee,
        "computer_name": computer,
        "recording": {
            "fps": rec.get("fps", 5),
            "crf": rec.get("crf", 28),
            "segment_duration": rec.get("segment_duration", 3600),
            "output_dir": str(dir_ / "recordings"),
            "audio_device": rec.get("audio_device", "") or "",
        },
        "google_drive": {
            "credentials_file": str(dir_ / "credentials.json"),
            "root_folder_id": vals.get("folder", ""),
        },
        "encryption": {
            "key_file": str(dir_ / "encryption.key"),
        },
        "analysis": {
            "enabled": False,
        },
        "input_monitor": {
            "enabled": False,
            "capture_keystroke_text": True,
            "screenshot_min_interval": 0.0,
        },
        "google_sheets": {
            "sheet_id": vals.get("sheet", ""),
        },
        "rag": {
            "enabled": False,
        },
    }


def _bundled_provision() -> dict:
    """Load the baked provisioning values from the app bundle, or {} if absent.

    Looks next to the frozen executable / in the PyInstaller temp dir for
    ``_provision.json`` (built from bootstrap.sh values at package time)."""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "_provision.json")
    candidates.append(Path(__file__).resolve().parent / "_provision.json")
    exe_dir = Path(sys.executable).resolve().parent
    candidates += [exe_dir / "_provision.json", exe_dir.parent / "Resources" / "_provision.json"]
    for p in candidates:
        try:
            if p.is_file():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            logger.debug("Could not read provision file %s", p, exc_info=True)
    return {}


def ensure_config() -> None:
    """Ensure the per-user config matches the bundled managed-deploy values.

    Earlier MDM attempts may have created no config, a partial config, a config
    pointing at the wrong Sheet, or an invalid raw encryption key. Repair those
    states in-place so a package upgrade can recover a managed Mac without
    asking IT to collect logs first.
    """
    try:
        dir_ = Path.home() / ".screenrecord"
        cfg = dir_ / "config.yaml"
        vals = _bundled_provision()
        if not vals or not vals.get("gcreds_b64"):
            return  # nothing baked in; let the normal "config not found" path run

        (dir_ / "recordings").mkdir(parents=True, exist_ok=True)
        _write_baked_files(dir_, vals)

        existing = _load_existing_config(cfg) if cfg.exists() else {}
        normalised = _normalise_config(existing, dir_, vals)

        if existing != normalised:
            cfg.write_text(
                yaml.safe_dump(normalised, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
            _diag(
                "Provisioned/repaired config "
                f"(employee={normalised.get('employee_name')} "
                f"computer={normalised.get('computer_name')})"
            )

        try:
            os.chmod(dir_, 0o700)
            os.chmod(dir_ / "credentials.json", 0o400)
            os.chmod(dir_ / "encryption.key", 0o400)
        except OSError:
            pass
        logger.info("Provisioning verified at %s", cfg)
    except Exception:
        _diag("Self-provisioning failed; falling back to normal config load.")
        logger.exception("Self-provisioning failed; falling back to normal config load.")
