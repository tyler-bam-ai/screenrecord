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
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple

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
        # Keyboard screenshots are debounced so normal typing produces one
        # after-typing screenshot instead of one image per letter.
        self._keyboard_debounce: float = max(
            0.2, float(im.get("keyboard_screenshot_debounce_sec", 1.0))
        )
        self._keyboard_text_max_chars: int = max(
            0, int(im.get("keyboard_text_max_chars", 160))
        )

        self._segment_provider = segment_provider
        self._events_dir = Path(output_dir)

        self._mouse_listener = None
        self._keyboard_listener = None
        self._lock = threading.Lock()
        self._seq = 0
        self._last_shot = 0.0
        self._running = False
        self._key_timer: Optional[threading.Timer] = None
        self._pending_keys: List[str] = []
        self._pending_key_started_at: Optional[float] = None
        self._pending_key_last_at: Optional[float] = None
        self._pending_key_segment: Optional[Tuple[str, float]] = None

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
        self._flush_keyboard_sequence(reason="stop")
        self._running = False
        for lst in (self._mouse_listener, self._keyboard_listener):
            try:
                if lst is not None:
                    lst.stop()
            except Exception:
                pass
        self._cancel_key_timer()
        logger.info("Input monitor stopped.")

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    def _on_click(self, x, y, button, pressed) -> None:
        if not pressed:
            return  # record button-down only
        self._flush_keyboard_sequence(reason="before_click")
        self._record(
            event_type="mouse_click",
            details={"x": int(x), "y": int(y), "button": str(button)},
            cursor=(int(x), int(y)),
            emphasize=True,
            force_screenshot=True,
        )

    def _on_press(self, key) -> None:
        key_value = ""
        if self._capture_keystroke_text:
            try:
                key_value = key.char if hasattr(key, "char") and key.char else str(key)
            except Exception:
                key_value = str(key)
        else:
            # Record that a key was pressed, but not which one (HIPAA-safer).
            key_value = "<redacted>"
        self._queue_keyboard_event(key_value)

    # ------------------------------------------------------------------
    def _queue_keyboard_event(self, key_value: str) -> None:
        """Collect adjacent key presses into a single screenshot event."""
        if not self._running:
            return
        now = time.monotonic()
        seg = None
        try:
            seg = self._segment_provider()
        except Exception:
            pass
        if not seg:
            return
        with self._lock:
            if not self._pending_keys:
                self._pending_key_started_at = now
                self._pending_key_segment = seg
            self._pending_keys.append(key_value)
            self._pending_key_last_at = now
            self._schedule_key_flush_locked()

    def _schedule_key_flush_locked(self) -> None:
        if self._key_timer is not None:
            self._key_timer.cancel()
        self._key_timer = threading.Timer(
            self._keyboard_debounce,
            self._flush_keyboard_sequence,
            kwargs={"reason": "debounce"},
        )
        self._key_timer.daemon = True
        self._key_timer.start()

    def _cancel_key_timer(self) -> None:
        with self._lock:
            if self._key_timer is not None:
                self._key_timer.cancel()
                self._key_timer = None

    def _flush_keyboard_sequence(self, reason: str = "debounce") -> None:
        with self._lock:
            keys = list(self._pending_keys)
            started_at = self._pending_key_started_at
            last_at = self._pending_key_last_at
            segment = self._pending_key_segment
            self._pending_keys = []
            self._pending_key_started_at = None
            self._pending_key_last_at = None
            self._pending_key_segment = None
            if self._key_timer is not None:
                self._key_timer.cancel()
                self._key_timer = None
        if not keys:
            return

        text = "".join(k for k in keys if len(k) == 1)
        if self._keyboard_text_max_chars and len(text) > self._keyboard_text_max_chars:
            text = text[: self._keyboard_text_max_chars] + "..."
        details = {
            "key_count": len(keys),
            "duration_sec": round(max(0.0, (last_at or 0.0) - (started_at or 0.0)), 3),
            "flush_reason": reason,
        }
        if self._capture_keystroke_text:
            details["text"] = text
            details["keys"] = keys[:50]
            if len(keys) > 50:
                details["keys_truncated"] = len(keys) - 50
        else:
            details["text"] = "<redacted>"

        self._record(
            event_type="key_sequence",
            details=details,
            cursor=None,
            emphasize=False,
            force_screenshot=True,
            segment=segment,
        )

    def _record(
        self,
        event_type: str,
        details: dict,
        cursor,
        emphasize: bool,
        *,
        force_screenshot: bool = False,
        segment: Optional[Tuple[str, float]] = None,
    ) -> None:
        if not self._running:
            return
        seg = segment
        try:
            if seg is None:
                seg = self._segment_provider()
        except Exception:
            pass
        if not seg:
            return  # not recording a segment right now; nothing to tie to
        seg_name, started_at = seg
        offset = max(0.0, time.monotonic() - started_at)
        ts_utc = datetime.now(timezone.utc).isoformat()

        with self._lock:
            now = time.monotonic()
            take_shot = force_screenshot or (now - self._last_shot) >= self._min_interval
            self._seq += 1
            seq = self._seq
            if take_shot:
                self._last_shot = now

        stem = Path(seg_name).stem  # video file stem
        shot_name = ""
        if take_shot:
            shot_name = self._capture_screenshot(
                stem=stem,
                seq=seq,
                event_type=event_type,
                offset=offset,
                ts_utc=ts_utc,
                cursor=cursor,
                emphasize=emphasize,
            )

        record = {
            "ts_utc": ts_utc,
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

    def _capture_screenshot(
        self,
        *,
        stem: str,
        seq: int,
        event_type: str,
        offset: float,
        ts_utc: str,
        cursor,
        emphasize: bool,
    ) -> str:
        try:
            import mss
            from PIL import Image, ImageDraw
        except Exception:
            return ""
        try:
            with mss.mss() as sct:
                mon = sct.monitors[0]  # full virtual desktop across monitors
                raw = sct.grab(mon)
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            draw = ImageDraw.Draw(img, "RGBA")
            if cursor is not None:
                cx = cursor[0] - mon["left"]
                cy = cursor[1] - mon["top"]
                r = 26 if emphasize else 16
                # translucent fill on a click; ring + crosshair always
                if emphasize:
                    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 0, 0, 70))
                draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 0, 0, 255), width=3)
                draw.line([cx - r - 8, cy, cx + r + 8, cy], fill=(255, 0, 0, 255), width=2)
                draw.line([cx, cy - r - 8, cx, cy + r + 8], fill=(255, 0, 0, 255), width=2)
            label = [
                f"video: {stem}.mp4",
                f"offset: {self._format_offset(offset)} | event: {seq:06d} {event_type}",
                f"captured: {ts_utc}",
            ]
            self._draw_label(draw, label)
            shot_dir = self._events_dir / f"{stem}.events"
            shot_dir.mkdir(parents=True, exist_ok=True)
            shot_name = (
                f"{seq:06d}_{self._safe_token(event_type)}_"
                f"{self._format_offset(offset).replace(':', '-')}.png"
            )
            img.save(shot_dir / shot_name, "PNG")
            return shot_name
        except Exception:
            logger.debug("Screenshot capture failed", exc_info=True)
            return ""

    @staticmethod
    def _format_offset(seconds: float) -> str:
        total_millis = max(0, int(round(seconds * 1000)))
        total, millis = divmod(total_millis, 1000)
        hours, rem = divmod(total, 3600)
        minutes, secs = divmod(rem, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"

    @staticmethod
    def _safe_token(value: str) -> str:
        token = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
        return token.strip("-") or "event"

    @staticmethod
    def _draw_label(draw, lines: List[str]) -> None:
        padding = 8
        line_height = 15
        width = max(420, min(1400, max(len(line) for line in lines) * 7 + padding * 2))
        height = line_height * len(lines) + padding * 2
        draw.rectangle([0, 0, width, height], fill=(0, 0, 0, 175))
        y = padding
        for line in lines:
            draw.text((padding, y), line, fill=(255, 255, 255, 255))
            y += line_height
