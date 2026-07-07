"""On-machine, user-facing alert when input capture can't run.

macOS forbids MDM from silently granting Input Monitoring / Screen Recording
(Apple policy — only the user can, in System Settings). So when those are
missing we show a native, explanatory dialog with an "Open Settings" button
that deep-links straight to the right pane, and we re-nudge on an interval
until the user grants it.

Windows has no equivalent OS gate, so there the alert only fires if input
capture actually FAILED to start (e.g. security software blocked the global
hook) — never for a permission the user can't do anything about.

No third-party deps: osascript (mac) / ctypes MessageBox (win). Runs in the
user's GUI session (the recorder already must, to capture screen + input).
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

# Deep-links straight to Privacy & Security → Input Monitoring on macOS.
_MAC_SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent")

_STATE_FILE = ".permission_alert_state.json"


def _state_path(output_dir: str) -> Path:
    return Path(output_dir) / _STATE_FILE


def _should_show(output_dir: str, repeat_min: float) -> bool:
    """Throttle: show at most once per repeat_min minutes."""
    p = _state_path(output_dir)
    try:
        last = json.loads(p.read_text()).get("last_shown", 0)
    except Exception:
        last = 0
    return (time.time() - last) >= repeat_min * 60


def _mark_shown(output_dir: str) -> None:
    try:
        _state_path(output_dir).write_text(
            json.dumps({"last_shown": time.time()}))
    except OSError:
        pass


def _clear(output_dir: str) -> None:
    try:
        _state_path(output_dir).unlink()
    except OSError:
        pass


def maybe_alert(config: dict, output_dir: str, logger,
                *, input_started: bool = True) -> str:
    """Check input-capture readiness and show a throttled alert if blocked.
    Returns a short status string for logging. Safe to call every poll tick."""
    im = config.get("input_monitor", {})
    # Default OFF, matching InputMonitor + main.py: no capture, no alert, unless
    # the machine's config explicitly opted in.
    if not im.get("enabled", False) or not im.get("permission_alert", True):
        return "disabled"
    repeat_min = float(im.get("permission_alert_repeat_min", 60))

    if sys.platform == "darwin":
        try:
            from . import macos_permissions
            status = macos_permissions.check_all(input_monitor_enabled=True)
        except Exception:
            return "check-error"
        if status == "ok":
            _clear(output_dir)
            return "ok"
        # Re-fire the native OS prompts too (harmless if already shown).
        try:
            macos_permissions.request_all(logger, input_monitor_enabled=True)
        except Exception:
            pass
        # Our custom dialog is Input-Monitoring specific; only show it when that
        # (or Accessibility) is the gap. Screen Recording has its own native
        # prompt from request_all above.
        if "Input Monitoring" not in status and "Accessibility" not in status:
            return f"native-only ({status})"
        if _should_show(output_dir, repeat_min):
            # daemon thread: the modal blocks until the user clicks, and this is
            # called from the 60s command-poll loop / startup — never block them.
            threading.Thread(target=_show_mac_alert, args=(status, logger),
                             daemon=True).start()
            _mark_shown(output_dir)
            return f"alerted ({status})"
        return f"throttled ({status})"

    if sys.platform.startswith("win"):
        # No OS gate on Windows; only alert if capture actually failed.
        if input_started:
            _clear(output_dir)
            return "ok"
        if _should_show(output_dir, repeat_min):
            threading.Thread(target=_show_windows_alert, args=(logger,),
                             daemon=True).start()
            _mark_shown(output_dir)
            return "alerted (capture-failed)"
        return "throttled (capture-failed)"

    return "ok"


# Ground-truth flag: set the first time ANY keystroke is captured, proving
# Input Monitoring is effective. Stable location (survives log/recordings sweeps).
_IM_CONFIRMED = Path.home() / ".screenrecord" / ".input_monitoring_confirmed"


_IM_PROMPT_STATE = Path.home() / ".screenrecord" / ".im_prompt_state.json"


def _read_im_state() -> dict:
    try:
        return json.loads(_IM_PROMPT_STATE.read_text())
    except Exception:
        return {}


def _write_im_state(d: dict) -> None:
    try:
        _IM_PROMPT_STATE.parent.mkdir(parents=True, exist_ok=True)
        _IM_PROMPT_STATE.write_text(json.dumps(d))
    except OSError:
        pass


def input_monitoring_confirmed() -> bool:
    return _IM_CONFIRMED.exists()


def mark_input_monitoring_confirmed() -> None:
    try:
        _IM_CONFIRMED.parent.mkdir(parents=True, exist_ok=True)
        _IM_CONFIRMED.write_text("1")
    except OSError:
        pass
    # capture succeeded — clear the "problem" state so we never restart again
    try:
        _IM_PROMPT_STATE.unlink()
    except OSError:
        pass


def input_monitoring_restart_due(grace_sec: float = 120) -> bool:
    """Call while Input Monitoring is unconfirmed but the user IS active (mouse
    clicks flowing, zero keystrokes). Returns True exactly once, ~grace_sec
    after the problem was first noticed — long enough for the user to enable it
    in Settings. The caller then exits so launchd (KeepAlive) relaunches the app
    and the fresh process re-evaluates the grant (macOS often needs this)."""
    st = _read_im_state()
    now = time.time()
    if not st.get("problem_since"):
        st["problem_since"] = now
        _write_im_state(st)
        return False
    if now - st["problem_since"] >= grace_sec and not st.get("restart_done"):
        st["restart_done"] = True
        _write_im_state(st)
        return True
    return False


def prompt_input_monitoring_setup(config: dict, logger) -> str:
    """The native Input Monitoring prompt does NOT reliably fire from a
    background LaunchAgent, so proactively open the Input Monitoring settings
    pane with an explanatory dialog. Shown (throttled) until a keystroke is ever
    captured (then mark_input_monitoring_confirmed silences it forever)."""
    im = config.get("input_monitor", {})
    if not im.get("enabled", False) or not im.get("permission_alert", True):
        return "disabled"
    if sys.platform != "darwin" or input_monitoring_confirmed():
        return "confirmed-or-na"
    repeat_min = float(im.get("permission_alert_repeat_min", 60))
    st = _read_im_state()
    if (time.time() - st.get("last", 0)) < repeat_min * 60:
        return "throttled"
    threading.Thread(target=_show_mac_alert,
                     args=("MISSING: Input Monitoring", logger),
                     daemon=True).start()
    st["last"] = time.time()          # merge, don't clobber problem_since
    _write_im_state(st)
    logger.info("Prompted user to enable Input Monitoring (native prompt "
                "unreliable from background agent).")
    return "prompted"


def alert_missing(config: dict, output_dir: str, logger, missing_desc: str) -> str:
    """Show the permission dialog because we have GROUND-TRUTH evidence the
    permission is missing (e.g. keystrokes not being captured despite activity),
    bypassing the unreliable macOS permission API. Throttled + threaded."""
    im = config.get("input_monitor", {})
    if not im.get("permission_alert", True) or sys.platform != "darwin":
        return "disabled"
    repeat_min = float(im.get("permission_alert_repeat_min", 60))
    if not _should_show(output_dir, repeat_min):
        return f"throttled ({missing_desc})"
    threading.Thread(target=_show_mac_alert,
                     args=(f"MISSING: {missing_desc}", logger),
                     daemon=True).start()
    _mark_shown(output_dir)
    return f"alerted ({missing_desc})"


def _show_mac_alert(status: str, logger) -> None:
    missing = status.replace("MISSING: ", "")
    msg = (
        "Screen Recorder needs macOS permission to record keyboard and mouse "
        "activity for workflow analysis.\\n\\n"
        "Your IT team CANNOT enable this remotely — Apple requires you to turn "
        "it on yourself. It takes 10 seconds:\\n\\n"
        "1. Click \\\"Open Settings\\\" below\\n"
        "2. Switch ON \\\"ScreenRecorder\\\" under Input Monitoring "
        "(and Accessibility if shown)\\n\\n"
        "Still needed: " + missing)
    script = (
        'display dialog "' + msg + '" '
        'with title "Action needed: Screen Recorder" '
        'buttons {"Later", "Open Settings"} default button "Open Settings" '
        'with icon caution')
    try:
        out = subprocess.run(["osascript", "-e", script],
                             capture_output=True, text=True, timeout=180)
        if "Open Settings" in (out.stdout or ""):
            subprocess.run(["open", _MAC_SETTINGS_URL], timeout=30)
        logger.info("Showed Input Monitoring permission alert to user.")
    except Exception:
        logger.debug("mac permission alert failed", exc_info=True)


def _show_windows_alert(logger) -> None:
    try:
        import ctypes
        MB_ICONWARNING = 0x30
        ctypes.windll.user32.MessageBoxW(
            0,
            "Screen Recorder could not start keyboard/mouse capture. Security "
            "software may be blocking it. Please contact your IT team so "
            "workflow recording can work correctly.",
            "Action needed: Screen Recorder",
            MB_ICONWARNING)
        logger.info("Showed input-capture-failed alert to user.")
    except Exception:
        logger.debug("windows permission alert failed", exc_info=True)
