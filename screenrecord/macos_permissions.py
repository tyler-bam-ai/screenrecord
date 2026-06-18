"""Trigger the standard macOS permission prompts at startup.

macOS never lets an app *grant* itself Screen Recording / Accessibility / Input
Monitoring — only the user can, in System Settings. But an app can ask the OS to
show its own prompt (the "... would like to ... Open System Settings" dialog)
instead of failing silently. That's the best non-MDM UX available, so we fire
all three requests on launch. Each call is best-effort and never fatal.

Uses ctypes against the system frameworks so there's no extra dependency to
bundle (pyobjc isn't in the frozen build).
"""

import ctypes
import ctypes.util
import logging
import sys

logger = logging.getLogger(__name__)


def request_all(logger_=None) -> None:
    """Ask macOS to prompt for Screen Recording, Accessibility, and Input
    Monitoring. No-op off macOS. Safe to call every launch."""
    log = logger_ or logger
    if sys.platform != "darwin":
        return
    _request_screen_recording(log)
    _request_accessibility(log)
    _request_input_monitoring(log)


def _load(framework: str):
    path = ("/System/Library/Frameworks/%s.framework/%s" % (framework, framework))
    return ctypes.CDLL(path)


# --------------------------------------------------------------------------
# Status checks (non-prompting) — for reporting to the dashboard
# --------------------------------------------------------------------------

def check_all() -> str:
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
    if not _granted_accessibility():
        missing.append("Accessibility (DOM)")
    return "ok" if not missing else "MISSING: " + ", ".join(missing)


def _granted_screen_recording() -> bool:
    try:
        cg = _load("CoreGraphics")
        if hasattr(cg, "CGPreflightScreenCaptureAccess"):
            cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
            return bool(cg.CGPreflightScreenCaptureAccess())
    except Exception:
        pass
    return True


def _granted_accessibility() -> bool:
    try:
        ax = _load("ApplicationServices")
        ax.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(ax.AXIsProcessTrusted())
    except Exception:
        pass
    return True


def _request_screen_recording(log) -> None:
    try:
        cg = _load("CoreGraphics")
        if hasattr(cg, "CGRequestScreenCaptureAccess"):
            cg.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
            granted = cg.CGRequestScreenCaptureAccess()
            log.info("Screen Recording access requested (granted=%s).", bool(granted))
    except Exception:
        log.debug("CGRequestScreenCaptureAccess unavailable", exc_info=True)


def _request_accessibility(log) -> None:
    """AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: true}) shows
    the Accessibility prompt that pynput needs (its event tap is gated here)."""
    try:
        appsvc = _load("ApplicationServices")
        cf = _load("CoreFoundation")

        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        kCFStringEncodingUTF8 = 0x08000100
        prompt_key = cf.CFStringCreateWithCString(
            None, b"AXTrustedCheckOptionPrompt", kCFStringEncodingUTF8)

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
