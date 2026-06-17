"""User-input ("DOM") capture: mouse clicks and keystrokes, each paired with an
annotated screenshot and tied back to the video segment it happened in.

Cross-platform (macOS + Windows) via ``pynput`` for global input events,
``mss`` for screenshots, and ``Pillow`` for annotating the cursor/click marker.

For every event we record, into files named after the current video segment:
  * a JSONL line in ``<segment_stem>.events.jsonl`` with:
      - absolute UTC timestamp
      - the video filename and the offset (seconds) into that video
      - event type + details (button/coords for clicks, key text for keys)
      - the screenshot filename
  * a PNG in ``<segment_stem>.events/`` showing the screen with a marker on the
    cursor (and a stronger marker on a click).

The main service uploads these alongside the encrypted video segment, so a
reviewer can jump straight to the moment in the video. PHI masking happens in a
later pass (Vertex/Gemini under the BAA); raw capture is encrypted at rest.

All optional dependencies are imported lazily; if any are missing the monitor
disables itself and logs a warning rather than crashing the recorder.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

# Provider returns (segment_filename, started_at_monotonic) or None when idle.
SegmentProvider = Callable[[], Optional[Tuple[str, float]]]


class InputMonitor:
    """Captures input events + annotated screenshots tied to the video."""

    def __init__(
        self,
        config: dict,
        segment_provider: SegmentProvider,
        output_dir: str,
    ) -> None:
        im = config.get("input_monitor", {})
        self._enabled: bool = im.get("enabled", False)
        self._capture_keystroke_text: bool = im.get("capture_keystroke_text", True)
        # Minimum seconds between screenshots (0 = one per event, full fidelity).
        self._min_interval: float = float(im.get("screenshot_min_interval", 0.0))

        self._segment_provider = segment_provider
        self._events_dir = Path(output_dir)

        self._mouse_listener = None
        self._keyboard_listener = None
        self._sct = None              # mss instance (per-thread)
        self._lock = threading.Lock()
        self._seq = 0
        self._last_shot = 0.0
        self._running = False

    # ------------------------------------------------------------------
    def start(self) -> None:
        if not self._enabled:
            logger.info("Input monitor disabled in config.")
            return
        try:
            from pynput import keyboard, mouse  # noqa: F401
            import mss  # noqa: F401
            from PIL import Image, ImageDraw  # noqa: F401
        except Exception as exc:
            logger.warning(
                "Input monitor unavailable (missing pynput/mss/Pillow): %s. "
                "Continuing without input capture.", exc,
            )
            self._enabled = False
            return

        from pynput import keyboard, mouse

        self._events_dir.mkdir(parents=True, exist_ok=True)
        self._running = True
        self._mouse_listener = mouse.Listener(on_click=self._on_click)
        self._keyboard_listener = keyboard.Listener(on_press=self._on_press)
        self._mouse_listener.start()
        self._keyboard_listener.start()
        logger.info(
            "Input monitor started (keystroke_text=%s, min_interval=%.2fs).",
            self._capture_keystroke_text, self._min_interval,
        )

    def stop(self) -> None:
        self._running = False
        for lst in (self._mouse_listener, self._keyboard_listener):
            try:
                if lst is not None:
                    lst.stop()
            except Exception:
                pass
        logger.info("Input monitor stopped.")

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    def _on_click(self, x, y, button, pressed) -> None:
        if not pressed:
            return  # record button-down only
        self._record(
            event_type="mouse_click",
            details={"x": int(x), "y": int(y), "button": str(button)},
            cursor=(int(x), int(y)),
            emphasize=True,
        )

    def _on_press(self, key) -> None:
        details = {}
        if self._capture_keystroke_text:
            try:
                details["key"] = key.char if hasattr(key, "char") and key.char else str(key)
            except Exception:
                details["key"] = str(key)
        else:
            # Record that a key was pressed, but not which one (HIPAA-safer).
            details["key"] = "<redacted>"
        self._record(event_type="key_press", details=details, cursor=None, emphasize=False)

    # ------------------------------------------------------------------
    def _record(self, event_type: str, details: dict, cursor, emphasize: bool) -> None:
        if not self._running:
            return
        seg = None
        try:
            seg = self._segment_provider()
        except Exception:
            pass
        if not seg:
            return  # not recording a segment right now; nothing to tie to
        seg_name, started_at = seg
        offset = max(0.0, time.monotonic() - started_at)

        with self._lock:
            now = time.monotonic()
            take_shot = (now - self._last_shot) >= self._min_interval
            self._seq += 1
            seq = self._seq
            if take_shot:
                self._last_shot = now

        stem = Path(seg_name).stem  # video file stem
        shot_name = ""
        if take_shot:
            shot_name = self._capture_screenshot(stem, seq, cursor, emphasize)

        record = {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "video_file": seg_name,
            "video_offset_sec": round(offset, 3),
            "event_type": event_type,
            "details": details,
            "screenshot": shot_name,
            "seq": seq,
        }
        try:
            events_file = self._events_dir / f"{stem}.events.jsonl"
            with open(events_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError:
            logger.debug("Could not write input event for %s", seg_name)

    def _capture_screenshot(self, stem: str, seq: int, cursor, emphasize: bool) -> str:
        try:
            import mss
            from PIL import Image, ImageDraw
        except Exception:
            return ""
        try:
            if self._sct is None:
                self._sct = mss.mss()
            mon = self._sct.monitors[1]  # primary monitor
            raw = self._sct.grab(mon)
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            if cursor is not None:
                draw = ImageDraw.Draw(img, "RGBA")
                cx = cursor[0] - mon["left"]
                cy = cursor[1] - mon["top"]
                r = 26 if emphasize else 16
                # translucent fill on a click; ring + crosshair always
                if emphasize:
                    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 0, 0, 70))
                draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 0, 0, 255), width=3)
                draw.line([cx - r - 8, cy, cx + r + 8, cy], fill=(255, 0, 0, 255), width=2)
                draw.line([cx, cy - r - 8, cx, cy + r + 8], fill=(255, 0, 0, 255), width=2)
            shot_dir = self._events_dir / f"{stem}.events"
            shot_dir.mkdir(parents=True, exist_ok=True)
            shot_name = f"{seq:06d}.png"
            img.save(shot_dir / shot_name, "PNG")
            return shot_name
        except Exception:
            logger.debug("Screenshot capture failed", exc_info=True)
            return ""
