"""Entry point for python -m screenrecord."""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="screenrecord",
        description="Screen recording service with Drive upload and AI analysis.",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Install auto-start entry for the current platform and exit.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check FFmpeg installation and validate config, then exit.",
    )
    parser.add_argument(
        "-5",
        dest="five_min",
        action="store_true",
        help="Record and upload in 5-minute segments instead of the default 60 minutes.",
    )
    args = parser.parse_args()

    from .config_manager import load_config, validate_config

    try:
        config = load_config(args.config)
    except Exception as exc:
        print(f"Error loading config: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.install:
        from .platform_utils import install_autostart

        try:
            result = install_autostart()
            print(f"Auto-start installed successfully. {result}")
        except Exception as exc:
            print(f"Failed to install auto-start: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    if args.check:
        from .platform_utils import check_ffmpeg_installed

        ffmpeg_ok = check_ffmpeg_installed()
        if not ffmpeg_ok:
            print("FFmpeg check FAILED.", file=sys.stderr)
            sys.exit(1)
        print("FFmpeg check passed.")

        try:
            validate_config(config)
            print("Config validation passed.")
        except Exception as exc:
            print(f"Config validation FAILED: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    # Apply -5 flag: override segment_duration to 300 seconds (5 minutes)
    if args.five_min:
        config.setdefault("recording", {})["segment_duration"] = 300
        print("Mode: 5-minute segments (recording + upload every 5 min)")
    else:
        duration = config.get("recording", {}).get("segment_duration", 3600)
        print(f"Mode: {duration // 60}-minute segments")

    # Normal operation: start the service
    from .main import ScreenRecordService

    service = ScreenRecordService(config)
    try:
        service.start()
    except KeyboardInterrupt:
        service.stop()
    except Exception as exc:
        print(f"Fatal error: {exc}", file=sys.stderr)
        service.stop()
        sys.exit(1)


if __name__ == "__main__":
    main()
