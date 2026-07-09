"""Input events must stay inside the video segment they reference."""

import unittest

from screenrecord.input_monitor import InputMonitor


class InputSegmentBoundaryTests(unittest.TestCase):
    def _monitor(self, segment):
        monitor = InputMonitor(
            {"input_monitor": {"enabled": False}},
            segment_provider=lambda: segment,
            output_dir=".",
        )
        monitor._running = True
        return monitor

    def test_record_uses_actual_debounced_event_time_for_offset(self) -> None:
        monitor = self._monitor(("segment.mp4", 100.0))
        records = []
        monitor._write_record = lambda stem, record: records.append((stem, record))

        monitor._record(
            event_type="key_sequence",
            details={"text": "test"},
            cursor=None,
            emphasize=False,
            segment=("segment.mp4", 100.0),
            event_monotonic=399.5,
        )

        self.assertEqual(records[0][0], "segment")
        self.assertEqual(records[0][1]["video_offset_sec"], 299.5)

    def test_keyboard_flush_passes_last_key_time(self) -> None:
        monitor = self._monitor(("new.mp4", 400.0))
        monitor._pending_keys = ["a", "b"]
        monitor._pending_key_started_at = 398.0
        monitor._pending_key_last_at = 399.25
        monitor._pending_key_segment = ("old.mp4", 100.0)
        calls = []
        monitor._record = lambda **kwargs: calls.append(kwargs)

        monitor._flush_keyboard_sequence(reason="segment_changed")

        self.assertEqual(calls[0]["segment"][0], "old.mp4")
        self.assertEqual(calls[0]["event_monotonic"], 399.25)

    def test_scroll_flush_passes_last_wheel_time(self) -> None:
        monitor = self._monitor(("new.mp4", 400.0))
        monitor._pending_scroll = {
            "dx": 0,
            "dy": -3,
            "ticks": 3,
            "started_at": 398.0,
            "last_at": 399.5,
            "segment": ("old.mp4", 100.0),
            "x": 20,
            "y": 30,
        }
        calls = []
        monitor._record = lambda **kwargs: calls.append(kwargs)

        monitor._flush_scroll(reason="segment_changed")

        self.assertEqual(calls[0]["segment"][0], "old.mp4")
        self.assertEqual(calls[0]["event_monotonic"], 399.5)


if __name__ == "__main__":
    unittest.main()
