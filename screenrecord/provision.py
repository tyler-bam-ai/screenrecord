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
import sys
from pathlib import Path

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
    """If the per-user config is missing, write it from the bundled values.

    No-op when the config already exists or when there are no bundled values
    (e.g. a dev run). Never raises — provisioning failure must not crash the app
    before the normal config error path runs."""
    try:
        dir_ = Path.home() / ".screenrecord"
        cfg = dir_ / "config.yaml"
        if cfg.exists():
            return
        vals = _bundled_provision()
        if not vals or not vals.get("gcreds_b64"):
            return  # nothing baked in; let the normal "config not found" path run

        (dir_ / "recordings").mkdir(parents=True, exist_ok=True)
        (dir_ / "credentials.json").write_bytes(base64.b64decode(vals["gcreds_b64"]))
        (dir_ / "encryption.key").write_bytes(base64.b64decode(vals["enckey_b64"]))

        employee = os.environ.get("USER") or os.environ.get("LOGNAME") or "User"
        try:
            import pwd
            full = pwd.getpwnam(employee).pw_gecos.split(",")[0].strip()
            if full:
                employee = full
        except Exception:
            pass
        computer = socket.gethostname().split(".")[0]

        cfg.write_text(_CONFIG_TEMPLATE.format(
            client=vals.get("client", "Unassigned"), employee=employee,
            computer=computer, dir=str(dir_),
            folder=vals.get("folder", ""), sheet=vals.get("sheet", "")),
            encoding="utf-8")
        try:
            os.chmod(dir_, 0o700)
            os.chmod(dir_ / "credentials.json", 0o400)
            os.chmod(dir_ / "encryption.key", 0o400)
        except OSError:
            pass
        logger.info("Self-provisioned config at %s (employee=%s computer=%s)",
                    cfg, employee, computer)
    except Exception:
        logger.exception("Self-provisioning failed; falling back to normal config load.")
