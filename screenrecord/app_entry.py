"""Entry point for the bundled (.app) build.

Used only when frozen by PyInstaller. It makes the bundled ffmpeg discoverable
on PATH (the recorder invokes ``ffmpeg`` by name), moves into the writable data
dir (launchd runs us with CWD=/), disables the git auto-updater, and defaults
the config to ``~/.screenrecord/config.yaml``.
"""

import os
import sys
import traceback
from pathlib import Path
from typing import Any


def _ensure_stdio() -> None:
    """Windowed PyInstaller apps on Windows can have missing stdio streams."""
    if os.name != "nt":
        return
    for name, mode in (("stdin", "r"), ("stdout", "a"), ("stderr", "a")):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, mode, encoding="utf-8"))
            except Exception:
                pass


def _setup_bundle_env() -> None:
    if not getattr(sys, "frozen", False):
        return
    _ensure_stdio()
    # Under launchd the working directory is "/" (read-only). Move into the
    # writable data dir so the service's relative paths (screenrecord.log,
    # audit.log, consent_records.json) resolve there, matching the proven
    # non-bundled deployment. Without this the agent crashes on first log write.
    data_dir = Path.home() / ".screenrecord"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(data_dir)
    except OSError:
        pass

    # The bundled ffmpeg may land in Resources, Frameworks, MacOS, or _MEIPASS
    # depending on PyInstaller version. Find it and put its dir FIRST on PATH so
    # the recorder's `ffmpeg` lookups resolve to ours, never a stray system one.
    exe_dir = Path(sys.executable).resolve().parent      # Contents/MacOS (mac) or dir of .exe (win)
    contents = exe_dir.parent                            # Contents (mac)
    # "ffmpeg" on macOS/Linux, "ffmpeg.exe" on Windows.
    ffmpeg_names = ("ffmpeg.exe", "ffmpeg") if os.name == "nt" else ("ffmpeg", "ffmpeg.exe")
    for base in (Path(getattr(sys, "_MEIPASS", exe_dir)), exe_dir,
                 contents / "Resources", contents / "Frameworks"):
        found = next((base / n for n in ffmpeg_names if (base / n).exists()), None)
        if found is not None:
            os.environ["PATH"] = str(base) + os.pathsep + os.environ.get("PATH", "")
            os.environ.setdefault("SCREENRECORD_FFMPEG", str(found))
            break

    # Disable the legacy source-tree git updater in frozen builds. Packaged
    # release updates are handled separately: a root helper on macOS and a
    # per-user EXE swapper on Windows.
    os.environ["SCREENRECORD_DISABLE_UPDATER"] = "1"


def _write_early_log(message: str) -> None:
    try:
        data_dir = Path.home() / ".screenrecord"
        data_dir.mkdir(parents=True, exist_ok=True)
        with (data_dir / "early_startup.log").open("a", encoding="utf-8") as fh:
            fh.write(message.rstrip() + "\n")
    except Exception:
        pass


def _record_early_failure(reason: str, error: Any) -> None:
    _write_early_log(f"{reason}: {error!r}")
    try:
        from screenrecord.diagnostics import record_early_failure

        record_early_failure(reason, error)
    except Exception as diag_exc:
        _write_early_log(f"diagnostic capture failed: {diag_exc!r}")
        _write_emergency_note(reason, error, diag_exc)


def _write_emergency_note(reason: str, error: Any, diag_error: Any = None) -> None:
    text = [
        "ScreenRecorder emergency startup diagnostic",
        f"reason={reason}",
        f"python={sys.version}",
        f"executable={sys.executable}",
        f"cwd={os.getcwd()}",
        f"home={Path.home()}",
        "",
        "### startup error",
        _format_error(error),
    ]
    if diag_error is not None:
        text.extend(["", "### diagnostic capture error", _format_error(diag_error)])
    body = "\n".join(text) + "\n"

    targets = [Path.home() / "Desktop", Path.home() / "Downloads"]
    if os.name == "nt":
        public = os.environ.get("PUBLIC")
        if public:
            targets.insert(0, Path(public) / "Documents")
    else:
        targets.insert(0, Path("/Users/Shared"))
    for directory in targets:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / "ScreenRecorder_emergency_startup_diagnostic.txt"
            target.write_text(body, encoding="utf-8")
            target.chmod(0o644)
        except Exception:
            pass


def _format_error(error: Any) -> str:
    if isinstance(error, BaseException):
        return "".join(traceback.format_exception(type(error), error, error.__traceback__))
    return repr(error)


def _run() -> None:
    _setup_bundle_env()
    # Self-provision the per-user config if the installer didn't (MDM installs
    # where the postinstall's console-user detection failed). Runs as the user
    # at login, so it always has the right home dir. No-op if config exists.
    from screenrecord import provision
    provision.ensure_config()
    if "--config" not in sys.argv:
        sys.argv += ["--config", str(Path.home() / ".screenrecord" / "config.yaml")]
    from screenrecord.__main__ import main as real_main
    real_main()


def main() -> None:
    try:
        _run()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if code:
            _record_early_failure("system_exit", exc)
        raise
    except BaseException as exc:
        _record_early_failure("app_entry_exception", exc)
        raise


if __name__ == "__main__":
    main()
