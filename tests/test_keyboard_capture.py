"""Regression coverage for exact text and Windows Raw Input key handling."""

import unittest

from screenrecord.input_monitor import InputMonitor
from screenrecord.windows_raw_keyboard import (
    RI_KEY_E0,
    VK_CONTROL,
    VK_LCONTROL,
    VK_RCONTROL,
    VK_RSHIFT,
    VK_SHIFT,
    normalize_raw_vkey,
)


class KeyboardTextTests(unittest.TestCase):
    def test_retains_windows_backend_configuration_for_startup(self) -> None:
        monitor = InputMonitor(
            {"input_monitor": {
                "enabled": True,
                "windows_keyboard_backend": "raw_input",
            }},
            segment_provider=lambda: None,
            output_dir=".",
        )
        self.assertEqual(monitor._windows_keyboard_backend, "raw_input")

    def test_reconstructs_exact_phrase_with_spaces(self) -> None:
        keys = list("hello") + ["Key.space"] + list("world")
        self.assertEqual(InputMonitor._reconstruct_text(keys), "hello world")

    def test_applies_backspace_and_preserves_enter_and_tab(self) -> None:
        keys = list("tesx") + ["Key.backspace", "t", "Key.enter", "Key.tab", "2"]
        self.assertEqual(InputMonitor._reconstruct_text(keys), "test\n\t2")

    def test_filters_modifiers_and_control_characters(self) -> None:
        keys = ["Key.ctrl_l", "\x16", "Key.shift", "A", "Key.alt_l"]
        self.assertEqual(InputMonitor._reconstruct_text(keys), "A")


class RawVirtualKeyTests(unittest.TestCase):
    def test_resolves_right_shift_from_scan_code(self) -> None:
        self.assertEqual(normalize_raw_vkey(VK_SHIFT, 0x36, 0), VK_RSHIFT)

    def test_resolves_control_side_from_extended_flag(self) -> None:
        self.assertEqual(normalize_raw_vkey(VK_CONTROL, 0x1D, 0), VK_LCONTROL)
        self.assertEqual(
            normalize_raw_vkey(VK_CONTROL, 0x1D, RI_KEY_E0), VK_RCONTROL
        )


if __name__ == "__main__":
    unittest.main()
