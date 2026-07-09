"""Regression tests for the no-console Windows watchdog."""

import unittest
from pathlib import Path

from screenrecord.windows_watchdog import (
    build_registration_script,
    build_vbs_launcher,
    build_watchdog_script,
)


class WindowsWatchdogTests(unittest.TestCase):
    def test_worker_applies_pending_update_and_restarts(self) -> None:
        script = build_watchdog_script(
            Path(r"C:\Users\User\AppData\Local\ScreenRecorder\ScreenRecorder.exe"),
            Path(r"C:\Users\User\AppData\Local\ScreenRecorder\pending_update.json"),
        )
        self.assertIn("pending_update.json", script)
        self.assertIn("Copy-Item", script)
        self.assertIn("Start-Process", script)

    def test_vbs_launcher_is_hidden_and_waits_for_worker(self) -> None:
        script = build_vbs_launcher(Path(r"C:\Program Files\Screen Recorder\watchdog.ps1"))
        self.assertIn('CreateObject("WScript.Shell")', script)
        self.assertIn(", 0, True", script)
        self.assertIn('""C:\\Program Files\\Screen Recorder\\watchdog.ps1""', script)

    def test_scheduled_task_action_is_wscript_not_powershell(self) -> None:
        script = build_registration_script(Path(r"C:\ScreenRecorder\watchdog.vbs"))
        self.assertIn("wscript.exe", script)
        self.assertIn("New-ScheduledTaskAction -Execute $wscript", script)
        self.assertNotIn('New-ScheduledTaskAction -Execute "powershell.exe"', script)


if __name__ == "__main__":
    unittest.main()
