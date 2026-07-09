import unittest
from datetime import datetime, timezone

from tools.event_lookup import _frame_filename, _safe_event, select_event


EVENTS = [
    {
        "seq": 1,
        "ts_utc": "2026-07-09T20:45:46+00:00",
        "event_type": "mouse_click",
        "video_file": "machine_user_2026-07-09_13-45-12.mp4",
        "video_offset_sec": 33.922,
        "details": {"x": 10, "y": 20, "button": "Button.left"},
    },
    {
        "seq": 2,
        "ts_utc": "2026-07-09T20:46:10+00:00",
        "event_type": "key_sequence",
        "video_file": "machine_user_2026-07-09_13-45-12.mp4",
        "video_offset_sec": 58.0,
        "details": {"text": "sensitive phrase", "keys": ["s"], "key_count": 16},
    },
    {
        "seq": 3,
        "ts_utc": "2026-07-09T20:47:00+00:00",
        "event_type": "mouse_click",
        "video_file": "machine_user_2026-07-09_13-45-12.mp4",
        "video_offset_sec": 108.0,
        "details": {"x": 30, "y": 40, "button": "Button.left"},
    },
]


class EventLookupTests(unittest.TestCase):
    def test_selects_latest_matching_event(self):
        self.assertEqual(select_event(EVENTS, event_type="mouse_click")["seq"], 3)

    def test_selects_closest_event_to_timestamp(self):
        at = datetime(2026, 7, 9, 20, 45, 50, tzinfo=timezone.utc)
        self.assertEqual(select_event(EVENTS, event_type="mouse_click", at=at)["seq"], 1)

    def test_can_search_key_text_without_printing_it(self):
        event = select_event(EVENTS, query_text="PHRASE")
        self.assertEqual(event["seq"], 2)
        safe = _safe_event(event)
        self.assertEqual(safe["details"]["text"], "<redacted>")
        self.assertEqual(safe["details"]["keys"], "<redacted>")

    def test_frame_name_contains_machine_sequence_and_timestamp(self):
        name = _frame_filename("ENT D/1360", EVENTS[0])
        self.assertTrue(name.startswith("ENT-D-1360_event-1_"))
        self.assertTrue(name.endswith(".png"))


if __name__ == "__main__":
    unittest.main()
