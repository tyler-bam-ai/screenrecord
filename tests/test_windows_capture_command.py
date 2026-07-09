"""Regression tests for the Windows FFmpeg capture command."""

import unittest

from screenrecord.platform_utils import _build_windows_command


class WindowsCaptureCommandTests(unittest.TestCase):
    def test_gdigrab_never_paints_the_real_cursor(self) -> None:
        """The gdigrab cursor painter causes user-visible flicker/focus loss."""
        command = _build_windows_command(
            fps=5,
            crf=28,
            segment_duration=3600,
            audio_device="",
            capture_cursor=True,
            output_path="segment.mp4",
        )

        draw_mouse_index = command.index("-draw_mouse")
        self.assertEqual(command[draw_mouse_index + 1], "0")
        self.assertIn("desktop", command)

    def test_audio_capture_is_preserved(self) -> None:
        command = _build_windows_command(
            fps=5,
            crf=28,
            segment_duration=300,
            audio_device="Microphone (USB)",
            capture_cursor=True,
            output_path="segment.mp4",
        )

        self.assertIn("dshow", command)
        self.assertIn("audio=Microphone (USB)", command)
        self.assertEqual(command[-1], "segment.mp4")


if __name__ == "__main__":
    unittest.main()
