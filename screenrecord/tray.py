"""Optional system-tray icon for the background agent.

Gives the agent a small, visible tray presence (a red record dot) so it runs
quietly in the tray instead of a console window, and so the running monitoring
is discoverable rather than hidden. Entirely best-effort: if pystray or its
backend is unavailable, the agent runs fine without it. Never blocks the agent.
"""

import logging
import platform
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_icon = None


def start_tray(
    app_name: str = "Screen Recorder",
    pause_callback: Optional[Callable[[int], bool]] = None,
) -> None:
    """Start the tray icon in a daemon thread. Best-effort, returns nothing."""
    global _icon
    if _icon is not None:
        return
    if platform.system() == "Darwin":
        logger.info("Menu-bar pause menu disabled on macOS LaunchAgent build.")
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

    def _pause_item(minutes: int):
        def _handler(icon, item):
            if pause_callback is None:
                return
            try:
                pause_callback(minutes)
            except Exception:
                logger.exception("Pause-for-%d-minutes tray action failed.", minutes)
        return _handler

    try:
        menu = pystray.Menu(
            pystray.MenuItem("Pause for 5 minutes", _pause_item(5)),
            pystray.MenuItem("Pause for 10 minutes", _pause_item(10)),
            pystray.MenuItem("Pause for 15 minutes", _pause_item(15)),
            pystray.MenuItem("Pause for 30 minutes", _pause_item(30)),
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
