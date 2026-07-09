"""Tests for deterministic video/event pairing metadata."""

import unittest

from screenrecord.main import _build_segment_logbook


class SegmentLogbookTests(unittest.TestCase):
    def test_logbook_pairs_every_event_to_one_segment(self) -> None:
        stem = "ENT-1271_User_2026-07-09_12-00-00"
        events = [
            {
                "seq": 1,
                "ts_utc": "2026-07-09T19:00:02+00:00",
                "video_file": stem + ".mp4",
                "video_offset_sec": 2.25,
                "event_type": "mouse_click",
                "details": {"x": 100, "y": 200, "button": "Button.left"},
            },
            {
                "seq": 2,
                "ts_utc": "2026-07-09T19:00:05+00:00",
                "video_file": stem + ".mp4",
                "video_offset_sec": 5.5,
                "event_type": "mouse_scroll",
                "details": {"dy": -3},
            },
            {
                "seq": 3,
                "ts_utc": "2026-07-09T19:00:08+00:00",
                "video_file": stem + ".mp4",
                "video_offset_sec": 8.75,
                "event_type": "key_sequence",
                "details": {"text": "test"},
            },
        ]

        logbook = _build_segment_logbook(stem, events)

        self.assertEqual(logbook["pairing"]["join_key"], stem)
        self.assertEqual(logbook["pairing"]["encrypted_video_file"], stem + ".mp4.enc")
        self.assertEqual(logbook["pairing"]["events_bundle"], stem + ".events.zip.enc")
        self.assertEqual(logbook["event_count"], 3)
        self.assertEqual(
            logbook["event_counts"],
            {"mouse_click": 1, "mouse_scroll": 1, "key_sequence": 1},
        )
        self.assertEqual(logbook["timeline"]["first_event_offset_sec"], 2.25)
        self.assertEqual(logbook["timeline"]["last_event_offset_sec"], 8.75)
        self.assertEqual(logbook["events"], events)


if __name__ == "__main__":
    unittest.main()
