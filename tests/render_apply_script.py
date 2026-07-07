#!/usr/bin/env python3
"""Render the REAL Windows apply script that ReleaseUpdater generates, so the
end-to-end test exercises the shipped code path (not a hand-copy of it).

Usage: python tests/render_apply_script.py <new_exe> <version> <out_ps1>
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from screenrecord.release_updater import ReleaseUpdater, _data_dir  # noqa: E402


def main() -> None:
    new_exe, version, out = sys.argv[1], sys.argv[2], sys.argv[3]
    (_data_dir() / "updates").mkdir(parents=True, exist_ok=True)
    ru = ReleaseUpdater({})
    script = ru._write_apply_script(Path(new_exe), version)
    Path(out).write_text(Path(script).read_text(encoding="utf-8"), encoding="utf-8")
    print(f"rendered real apply script -> {out}")


if __name__ == "__main__":
    main()
