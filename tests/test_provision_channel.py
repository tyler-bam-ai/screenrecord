"""Canary enrollment must survive managed config normalization."""

import tempfile
import unittest
from pathlib import Path

from screenrecord.provision import _normalise_config


class ProvisionChannelTests(unittest.TestCase):
    def test_preserves_canary_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _normalise_config(
                {"updater": {"channel": "canary"}},
                Path(tmp),
                {"folder": "root", "sheet": "sheet"},
            )
        self.assertEqual(config["updater"]["channel"], "canary")

    def test_rejects_unknown_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _normalise_config(
                {"updater": {"channel": "production-ish"}},
                Path(tmp),
                {"folder": "root", "sheet": "sheet"},
            )
        self.assertEqual(config["updater"]["channel"], "stable")


if __name__ == "__main__":
    unittest.main()
