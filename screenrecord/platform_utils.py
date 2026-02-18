"""Platform-specific utilities for screen recording.

Handles OS detection, FFmpeg command construction (macOS / Windows),
auto-start installation, and FFmpeg availability checks.
"""

import logging
import os
import platform
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OS detection
# ---------------------------------------------------------------------------

MACOS = "Darwin"
WINDOWS = "Windows"


def detect_os() -> str:
    """Return the current operating system identifier."""
    system = platform.system()
    if system not in (MACOS, WINDOWS):
        raise RuntimeError(
            f"Unsupported platform: {system}. Only macOS and Windows are supported."
        )
    logger.debug("Detected platform: %s", system)
    return system


def is_macos() -> bool:
    return platform.system() == MACOS


def is_windows() -> bool:
    return platform.system() == WINDOWS


# ---------------------------------------------------------------------------
# FFmpeg availability
# ---------------------------------------------------------------------------


def check_ffmpeg_installed() -> bool:
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is None:
        logger.warning("FFmpeg not found on PATH.")
        return False
    logger.debug("FFmpeg found at %s", ffmpeg_path)
    return True


def get_ffmpeg_version() -> Optional[str]:
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        first_line = result.stdout.strip().split("\n")[0]
        logger.debug("FFmpeg version: %s", first_line)
        return first_line
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        logger.error("Failed to determine FFmpeg version: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Device enumeration
# ---------------------------------------------------------------------------


def get_recording_devices() -> str:
    if not check_ffmpeg_installed():
        raise RuntimeError("FFmpeg is not installed or not on PATH.")

    current_os = detect_os()

    if current_os == MACOS:
        cmd = ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""]
    else:
        cmd = ["ffmpeg", "-f", "dshow", "-list_devices", "true", "-i", "dummy"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return result.stderr
    except subprocess.SubprocessError as exc:
        raise RuntimeError(f"Failed to enumerate recording devices: {exc}") from exc


# ---------------------------------------------------------------------------
# Screen device auto-detection (macOS)
# ---------------------------------------------------------------------------


def _detect_screen_device_index() -> str:
    """Auto-detect the AVFoundation screen capture device index on macOS.

    Parses ``ffmpeg -list_devices`` output to find the first device whose
    name starts with ``Capture screen``.

    Returns:
        The device index as a string.  Falls back to ``"1"`` if detection fails.
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=10,
        )
        in_video = False
        for line in result.stderr.splitlines():
            if "AVFoundation video devices:" in line:
                in_video = True
                continue
            if "AVFoundation audio devices:" in line:
                break
            if in_video and "Capture screen" in line:
                bracket_start = line.find("[", line.find("]") + 1)
                bracket_end = line.find("]", bracket_start)
                if bracket_start != -1 and bracket_end != -1:
                    idx = line[bracket_start + 1:bracket_end].strip()
                    logger.info("Auto-detected screen capture device index: %s", idx)
                    return idx
    except Exception:
        logger.exception("Failed to auto-detect screen device; falling back to '1'")

    logger.warning("Could not find 'Capture screen' device; defaulting to index '1'")
    return "1"


# ---------------------------------------------------------------------------
# FFmpeg command builder
# ---------------------------------------------------------------------------


def build_ffmpeg_command(
    recording_config: Dict[str, Any],
    computer_name: str,
    employee_name: str,
    output_path: str,
) -> List[str]:
    """Build the FFmpeg command line for recording a single segment.

    Uses ``-t`` to limit the recording duration to one segment. The caller
    is responsible for restarting FFmpeg for the next segment.

    Args:
        recording_config: The ``recording`` section of the application config.
        computer_name: Identifier for this machine.
        employee_name: Identifier for the user.
        output_path: Full path for the output file.

    Returns:
        A list of command-line arguments suitable for :func:`subprocess.Popen`.
    """
    fps: int = recording_config.get("fps", 5)
    crf: int = recording_config.get("crf", 28)
    segment_duration: int = recording_config.get("segment_duration", 3600)
    audio_device: str = recording_config.get("audio_device", "")

    current_os = detect_os()

    if current_os == MACOS:
        cmd = _build_macos_command(
            fps=fps,
            crf=crf,
            segment_duration=segment_duration,
            audio_device=audio_device,
            output_path=output_path,
        )
    else:
        cmd = _build_windows_command(
            fps=fps,
            crf=crf,
            segment_duration=segment_duration,
            audio_device=audio_device,
            output_path=output_path,
        )

    logger.info("Built FFmpeg command: %s", " ".join(cmd))
    return cmd


def _build_macos_command(
    *,
    fps: int,
    crf: int,
    segment_duration: int,
    audio_device: str,
    output_path: str,
) -> List[str]:
    """Construct the FFmpeg invocation for macOS (AVFoundation).

    On macOS 26+ AVFoundation hangs during pixel format negotiation unless
    ``-pixel_format uyvy422`` is set explicitly.  We capture at 30 fps
    (AVFoundation's comfortable native rate) and downsample to the target
    fps with ``-vf fps=N`` before encoding.
    """
    screen_idx = _detect_screen_device_index()

    cmd: List[str] = ["ffmpeg", "-y"]

    # Video input via AVFoundation.
    # Explicit pixel_format prevents a hang on macOS 26+.
    # Capture at 30fps (native-friendly) then downsample via filter.
    cmd += [
        "-f", "avfoundation",
        "-pixel_format", "uyvy422",
        "-framerate", "30",
        "-capture_cursor", "1",
    ]

    if audio_device:
        cmd += ["-i", f"{screen_idx}:{audio_device}"]
    else:
        cmd += ["-i", f"{screen_idx}:none"]

    # Duration limit — one segment per FFmpeg invocation.
    cmd += ["-t", str(segment_duration)]

    # Downsample to target fps before encoding.
    cmd += ["-vf", f"fps={fps}"]

    # Video encoding.
    cmd += [
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
    ]

    # Audio encoding (only when an audio device is specified).
    if audio_device:
        cmd += ["-c:a", "aac", "-b:a", "64k"]

    cmd.append(output_path)
    return cmd


def _build_windows_command(
    *,
    fps: int,
    crf: int,
    segment_duration: int,
    audio_device: str,
    output_path: str,
) -> List[str]:
    """Construct the FFmpeg invocation for Windows (gdigrab + dshow)."""
    cmd: List[str] = ["ffmpeg", "-y"]

    cmd += ["-f", "gdigrab", "-framerate", str(fps), "-i", "desktop"]

    if audio_device:
        cmd += ["-f", "dshow", "-i", f"audio={audio_device}"]

    cmd += ["-t", str(segment_duration)]

    cmd += [
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
    ]

    if audio_device:
        cmd += ["-c:a", "aac", "-b:a", "64k"]

    cmd.append(output_path)
    return cmd


# ---------------------------------------------------------------------------
# Auto-start installation
# ---------------------------------------------------------------------------

_LAUNCHD_LABEL = "com.screenrecord.agent"


def install_autostart(executable_path: Optional[str] = None) -> str:
    if executable_path is None:
        executable_path = f"{sys.executable} -m screenrecord"

    current_os = detect_os()

    if current_os == MACOS:
        return _install_autostart_macos(executable_path)
    else:
        return _install_autostart_windows(executable_path)


def _install_autostart_macos(executable_path: str) -> str:
    launch_agents_dir = Path.home() / "Library" / "LaunchAgents"
    launch_agents_dir.mkdir(parents=True, exist_ok=True)

    plist_path = launch_agents_dir / f"{_LAUNCHD_LABEL}.plist"

    parts = executable_path.split()
    program_arguments = "\n".join(
        f"        <string>{p}</string>" for p in parts
    )

    plist_content = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
          "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>{_LAUNCHD_LABEL}</string>
            <key>ProgramArguments</key>
            <array>
        {program_arguments}
            </array>
            <key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <true/>
            <key>StandardOutPath</key>
            <string>{Path.home() / "Library" / "Logs" / "screenrecord.log"}</string>
            <key>StandardErrorPath</key>
            <string>{Path.home() / "Library" / "Logs" / "screenrecord_error.log"}</string>
        </dict>
        </plist>
    """)

    try:
        plist_path.write_text(plist_content, encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Failed to write launchd plist: {exc}") from exc

    try:
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, timeout=10)
        subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True, check=True, timeout=10)
        logger.info("Launchd agent loaded successfully.")
    except subprocess.SubprocessError as exc:
        logger.warning("Could not load launchd agent automatically: %s.", exc)

    return f"Auto-start installed: {plist_path}"


def _install_autostart_windows(executable_path: str) -> str:
    task_name = "ScreenRecordAgent"

    subprocess.run(
        ["schtasks", "/Delete", "/TN", task_name, "/F"],
        capture_output=True, timeout=15,
    )

    try:
        result = subprocess.run(
            [
                "schtasks", "/Create",
                "/TN", task_name,
                "/TR", executable_path,
                "/SC", "ONLOGON",
                "/RL", "HIGHEST",
                "/F",
            ],
            capture_output=True, text=True, check=True, timeout=15,
        )
        logger.info("Scheduled task created: %s", result.stdout.strip())
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Failed to create scheduled task: {exc.stderr}") from exc

    return f"Auto-start installed: scheduled task '{task_name}'"


def uninstall_autostart() -> str:
    current_os = detect_os()

    if current_os == MACOS:
        plist_path = Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"
        if plist_path.exists():
            try:
                subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, timeout=10)
            except subprocess.SubprocessError:
                pass
            plist_path.unlink(missing_ok=True)
            return f"Auto-start removed: {plist_path}"
        return "No auto-start configuration found."
    else:
        task_name = "ScreenRecordAgent"
        try:
            subprocess.run(
                ["schtasks", "/Delete", "/TN", task_name, "/F"],
                capture_output=True, check=True, text=True, timeout=15,
            )
            return f"Auto-start removed: scheduled task '{task_name}'"
        except subprocess.CalledProcessError:
            return "No auto-start configuration found."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sanitize_filename(name: str) -> str:
    """Replace characters that are unsafe in filenames with underscores."""
    unsafe = set('<>:"/\\|?* ')
    return "".join(c if c not in unsafe else "_" for c in name).strip("_")
