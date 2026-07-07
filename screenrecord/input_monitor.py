"""User-input ("DOM") capture: mouse clicks and keystrokes tied back to the
video segment they happened in.

Cross-platform (macOS + Windows) via ``pynput`` for global input events. Optional
screenshots are supported, but disabled by default because the video is the
source of visual truth and screenshot capture can be expensive on workstations.

For every event we record, into files named after the current video segment:
  * a JSONL line in ``<segment_stem>.events.jsonl`` with:
      - absolute UTC timestamp
      - the video filename and the offset (seconds) into that video
      - event type + details (button/coords for clicks, key text for keys)
      - an optional screenshot filename, normally empty

The main service uploads these alongside the encrypted video segment, so a
reviewer can jump straight to the moment in the video. PHI masking happens in a
later pass (Vertex/Gemini under the BAA); raw capture is encrypted at rest.

All optional dependencies are imported lazily; if any are missing the monitor
disables itself and logs a warning rather than crashing the recorder.
"""

import json
import logging
import os
import queue
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
    """Captures input events tied to the video, with optional screenshots."""

    def __init__(
        self,
        config: dict,
        segment_provider: SegmentProvider,
        output_dir: str,
    ) -> None:
        im = config.get("input_monitor", {})
        # Default ON: event sidecars are the analysis pipeline's ground truth.
        # Safe if the OS permission is missing — start() degrades to no events
        # (the caller wraps it), it never blocks recording. A machine that must
        # not capture input sets input_monitor.enabled: false explicitly.
        # Default OFF: capture must be enabled EXPLICITLY in config (an app
        # update alone must never start keystroke/mouse capture on a machine
        # whose config never opted in — a consent/privacy invariant). New
        # installs set input_monitor.enabled: true via install.py/provision.py.
        self._enabled: bool = self._config_bool(im.get("enabled"), False)
        self._capture_keystroke_text: bool = self._config_bool(
            im.get("capture_keystroke_text"), True
        )
        self._capture_screenshots: bool = self._config_bool(
            im.get("capture_screenshots"), False
        )
        # Minimum seconds between screenshots. Events are still logged when a
        # burst is throttled; only the expensive full-screen image is skipped.
        self._min_interval: float = max(
            0.0, float(im.get("screenshot_min_interval", 0.35))
        )
        # Keyboard screenshots are debounced so normal typing produces one
        # after-typing screenshot instead of one image per letter.
        self._keyboard_debounce: float = max(
            0.2, float(im.get("keyboard_screenshot_debounce_sec", 1.0))
        )
        self._keyboard_text_max_chars: int = max(
            0, int(im.get("keyboard_text_max_chars", 160))
        )
        self._click_screenshot_delay: float = max(
            0.0, float(im.get("click_screenshot_delay_sec", 0.15))
        )
        # Scroll events fire rapidly (trackpad/wheel); coalesce a burst into one
        # "mouse_scroll" event with summed dx/dy, like keyboard sequences.
        self._scroll_debounce: float = max(
            0.1, float(im.get("scroll_debounce_sec", 0.5))
        )
        self._screenshot_format = str(im.get("screenshot_format", "jpg")).lower()
        if self._screenshot_format == "jpeg":
            self._screenshot_format = "jpg"
        if self._screenshot_format not in ("jpg", "png"):
            self._screenshot_format = "jpg"
        self._jpeg_quality: int = min(
            95, max(40, int(im.get("screenshot_jpeg_quality", 60)))
        )
        self._screenshot_max_width: int = max(
            0, int(im.get("screenshot_max_width", 1600))
        )
        self._screenshot_queue_max: int = max(
            1, int(im.get("screenshot_queue_max", 2))
        )

        self._segment_provider = segment_provider
        self._events_dir = Path(output_dir)

        self._mouse_listener = None
        self._keyboard_listener = None
        self._lock = threading.Lock()
        self._seq = 0
        # capture counters — ground truth for whether each OS permission is
        # actually effective (mouse => Accessibility, keys => Input Monitoring).
        # The macOS permission API can falsely report "granted" on frozen
        # builds; actual event flow cannot.
        self._n_clicks = 0
        self._n_keys = 0
        self._last_shot = -self._min_interval
        self._running = False
        self._key_timer: Optional[threading.Timer] = None
        self._pending_keys: List[str] = []
        self._pending_key_started_at: Optional[float] = None
        self._pending_key_last_at: Optional[float] = None
        self._pending_key_segment: Optional[Tuple[str, float]] = None
        self._scroll_timer: Optional[threading.Timer] = None
        self._pending_scroll: Optional[dict] = None
        self._shot_timers: List[threading.Timer] = []
        self._shot_queue: "queue.Queue[dict]" = queue.Queue(
            maxsize=self._screenshot_queue_max
        )
        self._shot_worker: Optional[threading.Thread] = None
        self._shot_stop = threading.Event()

    def capture_counts(self) -> Tuple[int, int]:
        """(#clicks, #key_sequences) captured so far. Ground truth for whether
        Input Monitoring is EFFECTIVE: many clicks with zero keys means the
        keyboard tap is being silently dropped (Input Monitoring not granted),
        regardless of what the macOS permission API claims."""
        return self._n_clicks, self._n_keys

    def restart(self) -> None:
        """Recreate the OS event taps to pick up a permission (especially Input
        Monitoring) granted AFTER the listener first started. Cheap; preserves
        the capture counters."""
        try:
            self.stop()
        except Exception:
            logger.debug("input restart: stop failed", exc_info=True)
        try:
            self.start()
        except Exception:
            logger.debug("input restart: start failed", exc_info=True)

    # ------------------------------------------------------------------
    def start(self) -> None:
        if not self._enabled:
            logger.info("Input monitor disabled in config.")
            return
        try:
            from pynput import keyboard, mouse  # noqa: F401
        except Exception as exc:
            logger.warning(
                "Input monitor unavailable (missing pynput): %s. "
                "Continuing without input capture.", exc,
            )
            self._enabled = False
            return
        if self._capture_screenshots:
            try:
                import mss  # noqa: F401
                from PIL import Image, ImageDraw  # noqa: F401
            except Exception as exc:
                logger.warning(
                    "Input screenshots unavailable (missing mss/Pillow): %s. "
                    "Continuing with event logging only.", exc,
                )
                self._capture_screenshots = False

        from pynput import keyboard, mouse

        self._events_dir.mkdir(parents=True, exist_ok=True)
        self._running = True
        if self._capture_screenshots:
            self._shot_stop.clear()
            self._shot_worker = threading.Thread(
                target=self._screenshot_worker,
                name="input-screenshot-worker",
                daemon=True,
            )
            self._shot_worker.start()
        self._mouse_listener = mouse.Listener(
            on_click=self._on_click, on_scroll=self._on_scroll)
        self._keyboard_listener = keyboard.Listener(on_press=self._on_press)
        self._mouse_listener.start()
        self._keyboard_listener.start()
        logger.info(
            "Input monitor started (keystroke_text=%s, screenshots=%s, min_interval=%.2fs).",
            self._capture_keystroke_text, self._capture_screenshots, self._min_interval,
        )

    def stop(self) -> None:
        self._flush_keyboard_sequence(reason="stop")
        self._flush_scroll(reason="stop")   # flush before _running goes false
        self._running = False
        for lst in (self._mouse_listener, self._keyboard_listener):
            try:
                if lst is not None:
                    lst.stop()
            except Exception:
                pass
        self._cancel_key_timer()
        self._stop_screenshot_worker()
        logger.info("Input monitor stopped.")

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    def _on_click(self, x, y, button, pressed) -> None:
        if not pressed:
            return  # record button-down only
        self._n_clicks += 1
        # flush any in-progress typing/scrolling so event order is preserved
        self._flush_keyboard_sequence(reason="before_click")
        self._flush_scroll(reason="before_click")
        self._record(
            event_type="mouse_click",
            details={"x": int(x), "y": int(y), "button": str(button)},
            cursor=(int(x), int(y)),
            emphasize=True,
            force_screenshot=True,
            screenshot_delay=self._click_screenshot_delay,
        )

    def _on_scroll(self, x, y, dx, dy) -> None:
        """Coalesce a scroll burst; a debounce timer flushes one event with the
        summed delta (dy<0 = scrolled down). Scrolling through long forms/lists
        is a core workflow action the 1fps video otherwise misses."""
        if not self._running:
            return
        seg = None
        try:
            seg = self._segment_provider()
        except Exception:
            pass
        if not seg:
            return
        now = time.monotonic()
        with self._lock:
            if self._pending_scroll is None:
                self._pending_scroll = {
                    "dx": 0, "dy": 0, "ticks": 0,
                    "started_at": now, "segment": seg,
                }
            ps = self._pending_scroll
            ps["dx"] += int(dx)
            ps["dy"] += int(dy)
            ps["ticks"] += 1
            ps["x"], ps["y"] = int(x), int(y)
            ps["last_at"] = now
            if self._scroll_timer is not None:
                self._scroll_timer.cancel()
            self._scroll_timer = threading.Timer(
                self._scroll_debounce, self._flush_scroll,
                kwargs={"reason": "debounce"},
            )
            self._scroll_timer.daemon = True
            self._scroll_timer.start()

    def _flush_scroll(self, reason: str = "debounce") -> None:
        with self._lock:
            ps = self._pending_scroll
            self._pending_scroll = None
            if self._scroll_timer is not None:
                self._scroll_timer.cancel()
                self._scroll_timer = None
        if not ps:
            return
        self._record(
            event_type="mouse_scroll",
            details={
                "x": ps.get("x"), "y": ps.get("y"),
                "dx": ps["dx"], "dy": ps["dy"], "ticks": ps["ticks"],
                "duration_sec": round(
                    max(0.0, ps.get("last_at", ps["started_at"])
                        - ps["started_at"]), 3),
                "flush_reason": reason,
            },
            cursor=(ps.get("x"), ps.get("y")) if ps.get("x") is not None else None,
            emphasize=False,
            allow_screenshot=False,   # never screenshot a scroll burst, and
                                      # don't consume the click screenshot budget
            segment=ps["segment"],
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
        self._n_keys += 1  # ground truth that Input Monitoring is effective

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
        screenshot_delay: float = 0.0,
        allow_screenshot: bool = True,
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
            take_shot = (
                allow_screenshot
                and self._capture_screenshots
                and (now - self._last_shot) >= self._min_interval
            )
            self._seq += 1
            seq = self._seq
            if take_shot:
                self._last_shot = now

        stem = Path(seg_name).stem  # video file stem
        shot_name = ""
        if take_shot:
            shot_name = self._shot_name(seq, event_type, offset)

        record = {
            "ts_utc": ts_utc,
            "video_file": seg_name,
            "video_offset_sec": round(offset, 3),
            "event_type": event_type,
            "details": details,
            "screenshot": shot_name,
            "seq": seq,
        }

        if take_shot:
            self._enqueue_screenshot(
                stem=stem,
                shot_name=shot_name,
                record=record,
                seq=seq,
                event_type=event_type,
                offset=offset,
                ts_utc=ts_utc,
                cursor=cursor,
                emphasize=emphasize,
                delay=screenshot_delay,
            )
            return

        if force_screenshot and not self._capture_screenshots:
            record["screenshot_skipped"] = "disabled"
        elif force_screenshot and self._min_interval > 0:
            record["screenshot_skipped"] = "rate_limited"
        self._write_record(stem, record)

    def _enqueue_screenshot(
        self,
        *,
        stem: str,
        shot_name: str,
        record: dict,
        seq: int,
        event_type: str,
        offset: float,
        ts_utc: str,
        cursor,
        emphasize: bool,
        delay: float,
    ) -> None:
        task = {
            "due_at": time.monotonic() + max(0.0, delay),
            "stem": stem,
            "shot_name": shot_name,
            "record": record,
            "seq": seq,
            "event_type": event_type,
            "offset": offset,
            "ts_utc": ts_utc,
            "cursor": cursor,
            "emphasize": emphasize,
        }
        try:
            self._shot_queue.put_nowait(task)
        except queue.Full:
            record["screenshot"] = ""
            record["screenshot_skipped"] = "queue_full"
            self._write_record(stem, record)

    def _screenshot_worker(self) -> None:
        while not self._shot_stop.is_set() or not self._shot_queue.empty():
            try:
                task = self._shot_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                due_at = float(task.get("due_at", 0.0))
                while not self._shot_stop.is_set():
                    remaining = due_at - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(remaining, 0.1))
                record = task["record"]
                actual = self._capture_screenshot(
                    stem=task["stem"],
                    shot_name=task["shot_name"],
                    seq=task["seq"],
                    event_type=task["event_type"],
                    offset=task["offset"],
                    ts_utc=task["ts_utc"],
                    cursor=task["cursor"],
                    emphasize=task["emphasize"],
                )
                if not actual:
                    record["screenshot"] = ""
                self._write_record(task["stem"], record)
            finally:
                self._shot_queue.task_done()

    def _write_record(self, stem: str, record: dict) -> None:
        try:
            events_file = self._events_dir / f"{stem}.events.jsonl"
            with open(events_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError:
            logger.debug("Could not write input event for %s", stem)

    def _capture_screenshot(
        self,
        *,
        stem: str,
        shot_name: str,
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

            scale = 1.0
            if self._screenshot_max_width and img.width > self._screenshot_max_width:
                scale = self._screenshot_max_width / float(img.width)
                new_size = (self._screenshot_max_width, max(1, int(img.height * scale)))
                resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
                img = img.resize(new_size, resample)

            draw = ImageDraw.Draw(img, "RGBA")
            if cursor is not None:
                cx = int((cursor[0] - mon["left"]) * scale)
                cy = int((cursor[1] - mon["top"]) * scale)
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
            out_path = shot_dir / shot_name
            if self._screenshot_format == "png":
                img.save(out_path, "PNG")
            else:
                img.save(
                    out_path,
                    "JPEG",
                    quality=self._jpeg_quality,
                    optimize=False,
                    progressive=False,
                )
            return shot_name
        except Exception:
            logger.debug("Screenshot capture failed", exc_info=True)
            return ""

    def _stop_screenshot_worker(self) -> None:
        self._shot_stop.set()
        deadline = time.monotonic() + 3.0
        while self._shot_worker is not None and self._shot_worker.is_alive():
            if time.monotonic() >= deadline:
                break
            self._shot_worker.join(timeout=0.25)
        self._shot_worker = None

    def _shot_name(self, seq: int, event_type: str, offset: float) -> str:
        return (
            f"{seq:06d}_{self._safe_token(event_type)}_"
            f"{self._format_offset(offset).replace(':', '-')}.{self._screenshot_format}"
        )

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

    @staticmethod
    def _config_bool(value, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "y", "on"}:
                return True
            if lowered in {"0", "false", "no", "n", "off"}:
                return False
        return default
