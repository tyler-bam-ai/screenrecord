"""Optional system-tray icon for the background agent.

Gives the agent a small, visible tray presence (a red record dot) so it runs
quietly in the tray instead of a console window, and so the running monitoring
is discoverable rather than hidden. Entirely best-effort: if pystray or its
backend is unavailable, the agent runs fine without it. Never blocks the agent.
"""

import logging
import threading

logger = logging.getLogger(__name__)

_icon = None


def start_tray(app_name: str = "Screen Recorder") -> None:
    """Start the tray icon in a daemon thread. Best-effort, returns nothing."""
    global _icon
    if _icon is not None:
        return
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception:
        logger.debug("pystray/Pillow unavailable; running without a tray icon.", exc_info=True)
        return

    def _make_image():
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((14, 14, 50, 50), fill=(214, 64, 64, 255))   # red record dot
        return img

    try:
        menu = pystray.Menu(
            pystray.MenuItem(f"{app_name} is running", None, enabled=False),
        )
        _icon = pystray.Icon("screenrecorder", _make_image(), app_name, menu)
    except Exception:
        logger.debug("Failed to construct tray icon.", exc_info=True)
        _icon = None
        return

    def _run():
        try:
            _icon.run()
        except Exception:
            logger.debug("Tray icon loop exited.", exc_info=True)

    threading.Thread(target=_run, name="tray", daemon=True).start()
    logger.info("Tray icon started.")


def stop_tray() -> None:
    global _icon
    if _icon is not None:
        try:
            _icon.stop()
        except Exception:
            pass
        _icon = None
