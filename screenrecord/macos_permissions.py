"""Trigger/report the required macOS privacy permissions at startup.

macOS never lets an app *grant* itself Screen Recording / Accessibility / Input
Monitoring — only the user can, in System Settings. But an app can ask the OS to
show the Screen Recording prompt and, when input capture is enabled, request the
additional event-monitoring permissions instead of failing silently.

Uses ctypes against the system frameworks so there's no extra dependency to
bundle (pyobjc isn't in the frozen build).
"""

import ctypes
import ctypes.util
import logging
import sys

logger = logging.getLogger(__name__)


def request_all(logger_=None, *, input_monitor_enabled: bool = False) -> None:
    """Ask macOS to prompt for required permissions. No-op off macOS.

    Keyboard + mouse capture goes through pynput, whose macOS backend is gated
    on Accessibility (it checks AXIsProcessTrusted and installs a session event
    tap). So input capture needs the *Accessibility* grant — NOT the separate
    "Input Monitoring" pane, where the app never appears. We therefore request
    Accessibility (which both prompts and auto-registers the app in that list)
    and do not pester the user with a second Input Monitoring prompt.
    """
    log = logger_ or logger
    if sys.platform != "darwin":
        return
    _request_screen_recording(log)
    if input_monitor_enabled:
        _request_accessibility(log)


def _load(framework: str):
    path = ("/System/Library/Frameworks/%s.framework/%s" % (framework, framework))
    return ctypes.CDLL(path)


# --------------------------------------------------------------------------
# Status checks (non-prompting) — for reporting to the dashboard
# --------------------------------------------------------------------------

def check_all(*, input_monitor_enabled: bool = False) -> str:
    """Return a permission-status string for the dashboard.

    'ok' when everything needed is granted; otherwise 'MISSING: ...' naming the
    permissions that still need granting. Non-macOS returns 'ok' (Windows has no
    equivalent gating). Uses preflight/non-prompting checks so it never pops a
    dialog. On any check error it assumes granted, to avoid false alarms.
    """
    if sys.platform != "darwin":
        return "ok"
    missing = []
    if not _granted_screen_recording():
        missing.append("Screen Recording")
    if input_monitor_enabled:
        # Input capture (pynput) is gated on Accessibility, not the separate
        # "Input Monitoring" pane — so Accessibility is the only grant to check.
        # (IOHIDCheckAccess is unreliable on the frozen build and isn't the
        # permission we actually use; the authoritative signal is captured
        # keystrokes, tracked by the caller.)
        if not _granted_accessibility():
            missing.append("Accessibility")
    return "ok" if not missing else "MISSING: " + ", ".join(missing)


def _granted_screen_recording() -> bool:
    try:
        cg = _load("CoreGraphics")
        if hasattr(cg, "CGPreflightScreenCaptureAccess"):
            cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
            return bool(cg.CGPreflightScreenCaptureAccess())
    except Exception:
        pass
    return False   # fail CLOSED: unknown = treat as missing so it surfaces


def _granted_accessibility() -> bool:
    try:
        ax = _load("ApplicationServices")
        ax.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(ax.AXIsProcessTrusted())
    except Exception:
        pass
    return False   # fail CLOSED


def _granted_input_monitoring() -> bool:
    # NOTE: IOHIDCheckAccess has proven UNRELIABLE on the frozen build — it has
    # reported Granted while keystrokes were being silently dropped. The
    # authoritative signal is actual keystroke capture (input_monitor
    # capture_counts, checked in main). This stays as a best-effort hint only.
    try:
        iokit = _load("IOKit")
        if hasattr(iokit, "IOHIDCheckAccess"):
            iokit.IOHIDCheckAccess.restype = ctypes.c_uint32
            iokit.IOHIDCheckAccess.argtypes = [ctypes.c_uint32]
            kIOHIDRequestTypeListenEvent = 1
            kIOHIDAccessTypeGranted = 0
            return iokit.IOHIDCheckAccess(kIOHIDRequestTypeListenEvent) == kIOHIDAccessTypeGranted
    except Exception:
        pass
    return False   # fail CLOSED


# Prompt at most ONCE per process. AXIsProcessTrustedWithOptions re-shows the
# prompt on every call while the process is untrusted, and a freshly-granted
# Accessibility permission doesn't take effect until a restart — so without this
# guard the 60s status loop pops the prompt over and over after the user already
# allowed it. A restart yields a fresh process (flags reset) that re-checks
# without prompting if the grant is now effective.
_ax_prompted = False
_screen_prompted = False


def _request_screen_recording(log) -> None:
    global _screen_prompted
    if _screen_prompted:
        return
    try:
        cg = _load("CoreGraphics")
        if hasattr(cg, "CGRequestScreenCaptureAccess"):
            cg.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
            granted = cg.CGRequestScreenCaptureAccess()
            _screen_prompted = True
            log.info("Screen Recording access requested (granted=%s).", bool(granted))
    except Exception:
        log.debug("CGRequestScreenCaptureAccess unavailable", exc_info=True)


def _request_accessibility(log) -> None:
    """AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: true}) shows
    the Accessibility prompt that pynput needs (its event tap is gated here).

    Prompts at most once per process (see _ax_prompted) so an already-granted
    user isn't nagged repeatedly while we wait for the restart that activates it.
    """
    global _ax_prompted
    if _ax_prompted:
        return
    # If Accessibility is already effective in THIS process, don't prompt at all.
    if _granted_accessibility():
        _ax_prompted = True
        return
    try:
        appsvc = _load("ApplicationServices")
        cf = _load("CoreFoundation")

        # Must be the framework's own constant: the dictionary below is created
        # with NULL callbacks (pointer-equality keys), so a lookalike CFString
        # we allocate ourselves would never match AX's lookup and the prompt
        # would silently not show.
        prompt_key = ctypes.c_void_p.in_dll(appsvc, "kAXTrustedCheckOptionPrompt")

        kCFBooleanTrue = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")

        cf.CFDictionaryCreate.restype = ctypes.c_void_p
        cf.CFDictionaryCreate.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_long,
            ctypes.c_void_p, ctypes.c_void_p]
        keys = (ctypes.c_void_p * 1)(prompt_key)
        vals = (ctypes.c_void_p * 1)(kCFBooleanTrue)
        options = cf.CFDictionaryCreate(None, keys, vals, 1, None, None)

        appsvc.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        appsvc.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        trusted = appsvc.AXIsProcessTrustedWithOptions(options)
        _ax_prompted = True
        log.info("Accessibility trust requested (trusted=%s).", bool(trusted))
    except Exception:
        log.debug("AXIsProcessTrustedWithOptions unavailable", exc_info=True)


def _request_input_monitoring(log) -> None:
    """IOHIDRequestAccess(kIOHIDRequestTypeListenEvent) prompts for Input
    Monitoring on macOS 10.15+."""
    try:
        iokit = _load("IOKit")
        if hasattr(iokit, "IOHIDRequestAccess"):
            iokit.IOHIDRequestAccess.restype = ctypes.c_bool
            iokit.IOHIDRequestAccess.argtypes = [ctypes.c_uint32]
            kIOHIDRequestTypeListenEvent = 1
            granted = iokit.IOHIDRequestAccess(kIOHIDRequestTypeListenEvent)
            log.info("Input Monitoring access requested (granted=%s).", bool(granted))
    except Exception:
        log.debug("IOHIDRequestAccess unavailable", exc_info=True)
