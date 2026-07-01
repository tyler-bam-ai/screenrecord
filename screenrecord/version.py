"""Version constants for packaged ScreenRecorder builds."""

MAC_VERSION = "1.4.17"
MAC_BUILD = "22"
WINDOWS_VERSION = "1.0.20"
MAC_UPDATE_VERSION = f"{MAC_VERSION}.{MAC_BUILD}"


def current_platform_version() -> str:
    """Return the packaged version for the current platform."""
    import sys

    if sys.platform == "darwin":
        return MAC_VERSION
    if sys.platform == "win32":
        return WINDOWS_VERSION
    return MAC_VERSION
