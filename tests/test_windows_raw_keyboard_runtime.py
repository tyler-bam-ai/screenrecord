"""Windows-only smoke test for the ctypes Raw Input receiver."""

import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
