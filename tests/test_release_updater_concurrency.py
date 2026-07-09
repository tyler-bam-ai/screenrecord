"""Concurrent periodic/manual update checks must coalesce."""

import threading
import unittest

from screenrecord.release_updater import ReleaseUpdater


class ReleaseUpdaterConcurrencyTests(unittest.TestCase):
    def test_second_check_does_not_enter_download_path(self) -> None:
        updater = ReleaseUpdater.__new__(ReleaseUpdater)
        updater._check_lock = threading.Lock()
        updater._check_and_stage_locked = lambda **kwargs: self.fail(
            "concurrent request entered the download path"
        )

        updater._check_lock.acquire()
        try:
            self.assertFalse(updater.check_and_stage(force=False))
        finally:
            updater._check_lock.release()


if __name__ == "__main__":
    unittest.main()
