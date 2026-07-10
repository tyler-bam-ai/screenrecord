"""Windows-only smoke test for the ctypes Raw Input receiver."""

import sys
import unittest
from unittest import mock

from screenrecord.input_monitor import InputMonitor
from screenrecord.windows_raw_keyboard import WindowsRawKeyboardListener


@unittest.skipUnless(sys.platform == "win32", "Windows-only Raw Input smoke test")
class WindowsRawKeyboardRuntimeTests(unittest.TestCase):
    def test_listener_registers_hidden_window_and_stops(self) -> None:
        listener = WindowsRawKeyboardListener(lambda _value: None)
        listener.start()
        try:
            self.assertTrue(listener.is_alive())
        finally:
            listener.stop()
        self.assertFalse(listener.is_alive())

    def test_complete_input_monitor_starts_raw_backend(self) -> None:
        class DummyMouseListener:
            def __init__(self, **_kwargs):
                self._alive = False

            def start(self):
                self._alive = True

            def stop(self):
                self._alive = False

            def is_alive(self):
                return self._alive

        monitor = InputMonitor(
            {"input_monitor": {
                "enabled": True,
                "windows_keyboard_backend": "raw_input",
                "capture_screenshots": False,
            }},
            segment_provider=lambda: None,
            output_dir=".",
        )
        with mock.patch("pynput.mouse.Listener", DummyMouseListener):
            monitor.start()
            try:
                health = monitor.keyboard_health()
                self.assertEqual(health["keyboard_backend"], "windows_raw_input")
                self.assertTrue(health["keyboard_listener_alive"])
            finally:
                monitor.stop()


if __name__ == "__main__":
    unittest.main()
