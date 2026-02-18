#!/usr/bin/env python3
"""
Interactive installer for the Screen Recording Service.
Run this on each employee's computer to set up everything.
"""

import getpass
import os
import platform
import shutil
import socket
import subprocess
import sys
import textwrap

# ANSI color codes
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

CHECK = f"{GREEN}\u2713{RESET}"
CROSS = f"{RED}\u2717{RESET}"
WARN = f"{YELLOW}!{RESET}"

INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))
IS_MACOS = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"


def print_banner():
    banner = f"""
{CYAN}{BOLD}{'=' * 60}
     Screen Recording Service - Installer
{'=' * 60}{RESET}

  Platform:    {platform.system()} {platform.release()}
  Install dir: {INSTALL_DIR}
"""
    print(banner)


def print_step(step_num, title):
    print(f"\n{BOLD}{CYAN}[Step {step_num}]{RESET} {BOLD}{title}{RESET}")
    print("-" * 50)


def check_python():
    """Verify Python 3.8+ is installed."""
    version = sys.version_info
    if version >= (3, 8):
        print(f"  {CHECK} Python {version.major}.{version.minor}.{version.micro} detected")
        return True
    else:
        print(f"  {CROSS} Python 3.8+ is required (found {version.major}.{version.minor}.{version.micro})")
        print(f"  {WARN} Please install Python 3.8 or newer from https://python.org")
        return False


def check_ffmpeg():
    """Check if FFmpeg is installed."""
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        try:
            result = subprocess.run(
                ["ffmpeg", "-version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            first_line = result.stdout.strip().split("\n")[0] if result.stdout else "unknown version"
            print(f"  {CHECK} FFmpeg found: {ffmpeg_path}")
            print(f"      {first_line}")
            return True
        except (subprocess.SubprocessError, OSError):
            pass

    print(f"  {CROSS} FFmpeg not found")
    if IS_MACOS:
        print(f"  {WARN} Install with Homebrew:")
        print(f"      {BOLD}brew install ffmpeg{RESET}")
    elif IS_WINDOWS:
        print(f"  {WARN} Install FFmpeg using one of:")
        print(f"      {BOLD}winget install ffmpeg{RESET}")
        print(f"      or download from https://ffmpeg.org/download.html")
    else:
        print(f"  {WARN} Install FFmpeg via your package manager:")
        print(f"      {BOLD}sudo apt install ffmpeg{RESET}  (Debian/Ubuntu)")
        print(f"      {BOLD}sudo dnf install ffmpeg{RESET}  (Fedora)")
    return False


def check_audio_setup():
    """Platform-specific audio setup guidance."""
    if IS_MACOS:
        print(f"  Checking for BlackHole audio loopback device...")
        try:
            result = subprocess.run(
                ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                capture_output=True,
                text=True,
                timeout=10,
            )
            output = result.stdout + result.stderr
            if "BlackHole" in output:
                print(f"  {CHECK} BlackHole audio device detected")
                return True
            else:
                print(f"  {WARN} BlackHole not detected in audio devices")
                print()
                print(f"  BlackHole is required for capturing system audio on macOS.")
                print(f"  Install it with Homebrew:")
                print(f"      {BOLD}brew install blackhole-2ch{RESET}")
                print()
                print(f"  After installing, set up a Multi-Output Device in Audio MIDI Setup:")
                print(f"    1. Open /Applications/Utilities/Audio MIDI Setup.app")
                print(f"    2. Click '+' at the bottom left and select 'Create Multi-Output Device'")
                print(f"    3. Check both your speakers/headphones and BlackHole 2ch")
                print(f"    4. Right-click the Multi-Output Device and select 'Use This Device For Sound Output'")
                print()
                print(f"  {YELLOW}You can continue the install without it, but audio capture won't work.{RESET}")
                return False
        except (subprocess.SubprocessError, OSError):
            print(f"  {WARN} Could not check audio devices (FFmpeg not available)")
            return False

    elif IS_WINDOWS:
        print(f"  {WARN} Windows audio capture uses Stereo Mix or WASAPI loopback.")
        print()
        print(f"  To enable Stereo Mix:")
        print(f"    1. Right-click the speaker icon in the taskbar")
        print(f"    2. Select 'Sounds' -> 'Recording' tab")
        print(f"    3. Right-click in the empty area and check 'Show Disabled Devices'")
        print(f"    4. If 'Stereo Mix' appears, right-click it and select 'Enable'")
        print()
        print(f"  If Stereo Mix is not available, the recorder will attempt WASAPI loopback.")
        print(f"  {CHECK} Windows audio guidance provided")
        return True

    else:
        print(f"  {WARN} Linux audio capture uses PulseAudio/PipeWire.")
        print(f"  Ensure PulseAudio or PipeWire is running for audio capture.")
        return True


def install_dependencies():
    """Run pip install -r requirements.txt."""
    requirements_path = os.path.join(INSTALL_DIR, "requirements.txt")
    if not os.path.exists(requirements_path):
        print(f"  {CROSS} requirements.txt not found at {requirements_path}")
        return False

    print(f"  Installing Python dependencies...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", requirements_path],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0:
            print(f"  {CHECK} Dependencies installed successfully")
            return True
        else:
            print(f"  {CROSS} pip install failed:")
            for line in result.stderr.strip().split("\n")[-5:]:
                print(f"      {line}")
            return False
    except subprocess.TimeoutExpired:
        print(f"  {CROSS} pip install timed out (5 minute limit)")
        return False
    except (subprocess.SubprocessError, OSError) as e:
        print(f"  {CROSS} Failed to run pip: {e}")
        return False


def create_config():
    """Interactive config creation."""
    config_path = os.path.join(INSTALL_DIR, "config.yaml")

    if os.path.exists(config_path):
        print(f"  {WARN} config.yaml already exists at {config_path}")
        overwrite = input(f"  Overwrite? (y/N): ").strip().lower()
        if overwrite != "y":
            print(f"  {CHECK} Keeping existing config.yaml")
            return True

    print(f"  Please provide the following information:\n")

    # Employee name
    default_user = getpass.getuser()
    employee_name = input(f"  Employee name [{default_user}]: ").strip()
    if not employee_name:
        employee_name = default_user

    # Computer name (auto-detected)
    computer_name = socket.gethostname()
    print(f"  Computer name (auto-detected): {computer_name}")

    # Google Drive credentials
    print()
    creds_path = input(f"  Path to Google Drive credentials JSON file: ").strip()
    if creds_path:
        creds_path = os.path.expanduser(creds_path)
        if not os.path.exists(creds_path):
            print(f"  {WARN} File not found: {creds_path}")
            print(f"      The file will need to exist before running the recorder.")

    # Google Drive folder ID
    folder_id = input(f"  Google Drive root folder ID: ").strip()

    # Video analysis
    print()
    enable_analysis = input(f"  Enable video analysis? (y/N): ").strip().lower() == "y"

    gemini_api_key = ""
    xai_api_key = ""
    openrouter_api_key = ""

    if enable_analysis:
        gemini_api_key = input(f"  Gemini API key (optional, press Enter to skip): ").strip()
        xai_api_key = input(f"  xAI API key (optional, press Enter to skip): ").strip()
        openrouter_api_key = input(f"  OpenRouter API key (optional, press Enter to skip): ").strip()

    # Build config YAML
    config_lines = [
        f"# Screen Recording Service Configuration",
        f"# Generated by installer on {platform.node()}",
        f"",
        f"employee_name: \"{employee_name}\"",
        f"computer_name: \"{computer_name}\"",
        f"",
        f"# Recording settings",
        f"recording:",
        f"  segment_duration: 300  # seconds per video segment",
        f"  resolution: \"1920x1080\"",
        f"  fps: 15",
        f"  video_codec: \"libx264\"",
        f"  audio_enabled: true",
        f"",
        f"# Google Drive upload settings",
        f"google_drive:",
        f"  credentials_file: \"{creds_path}\"",
        f"  root_folder_id: \"{folder_id}\"",
        f"  upload_chunk_size: 10485760  # 10 MB",
        f"",
        f"# Video analysis settings",
        f"analysis:",
        f"  enabled: {str(enable_analysis).lower()}",
    ]

    if enable_analysis:
        if gemini_api_key:
            config_lines.append(f"  gemini_api_key: \"{gemini_api_key}\"")
        if xai_api_key:
            config_lines.append(f"  xai_api_key: \"{xai_api_key}\"")
        if openrouter_api_key:
            config_lines.append(f"  openrouter_api_key: \"{openrouter_api_key}\"")

    config_lines.extend([
        f"",
        f"# Local storage",
        f"storage:",
        f"  local_temp_dir: \"{os.path.join(INSTALL_DIR, 'recordings')}\"",
        f"  keep_local_days: 1",
        f"",
        f"# Logging",
        f"logging:",
        f"  level: \"INFO\"",
        f"  file: \"{os.path.join(INSTALL_DIR, 'screenrecord.log')}\"",
    ])

    config_content = "\n".join(config_lines) + "\n"

    try:
        with open(config_path, "w") as f:
            f.write(config_content)
        print(f"\n  {CHECK} Config written to {config_path}")
        return True
    except OSError as e:
        print(f"\n  {CROSS} Failed to write config: {e}")
        return False


def setup_autostart():
    """Set up auto-start on login."""
    run_script = os.path.join(INSTALL_DIR, "run.py")
    config_path = os.path.join(INSTALL_DIR, "config.yaml")

    if not os.path.exists(run_script):
        print(f"  {WARN} run.py not found at {run_script}")
        print(f"      Auto-start will be configured but may not work until run.py exists.")

    if IS_MACOS:
        return _setup_macos_launchd(run_script, config_path)
    elif IS_WINDOWS:
        return _setup_windows_task(run_script, config_path)
    else:
        print(f"  {WARN} Auto-start setup is not implemented for {platform.system()}.")
        print(f"      You can add a crontab entry or systemd user service manually.")
        return False


def _setup_macos_launchd(run_script, config_path):
    """Create and load a macOS launchd plist."""
    plist_dir = os.path.expanduser("~/Library/LaunchAgents")
    plist_path = os.path.join(plist_dir, "com.screenrecord.agent.plist")

    # Find python3 path
    python_path = shutil.which("python3") or sys.executable

    plist_content = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
          "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>com.screenrecord.agent</string>

            <key>ProgramArguments</key>
            <array>
                <string>{python_path}</string>
                <string>{run_script}</string>
                <string>--config</string>
                <string>{config_path}</string>
            </array>

            <key>RunAtLoad</key>
            <true/>

            <key>KeepAlive</key>
            <true/>

            <key>WorkingDirectory</key>
            <string>{INSTALL_DIR}</string>

            <key>StandardOutPath</key>
            <string>/tmp/screenrecord.stdout.log</string>

            <key>StandardErrorPath</key>
            <string>/tmp/screenrecord.stderr.log</string>
        </dict>
        </plist>
    """)

    try:
        os.makedirs(plist_dir, exist_ok=True)

        # Unload existing plist if present
        if os.path.exists(plist_path):
            subprocess.run(
                ["launchctl", "unload", plist_path],
                capture_output=True,
                timeout=10,
            )

        with open(plist_path, "w") as f:
            f.write(plist_content)
        print(f"  {CHECK} Plist written to {plist_path}")

        # Load the plist
        result = subprocess.run(
            ["launchctl", "load", plist_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            print(f"  {CHECK} LaunchAgent loaded successfully")
        else:
            err = result.stderr.strip()
            if err:
                print(f"  {WARN} launchctl load returned: {err}")
            print(f"  {WARN} The agent will start automatically on next login.")

        return True

    except OSError as e:
        print(f"  {CROSS} Failed to create plist: {e}")
        return False
    except subprocess.SubprocessError as e:
        print(f"  {WARN} Plist written but launchctl command failed: {e}")
        print(f"      You can load it manually with: launchctl load {plist_path}")
        return True


def _setup_windows_task(run_script, config_path):
    """Create a Windows scheduled task for auto-start."""
    # Find pythonw.exe (windowless Python)
    python_dir = os.path.dirname(sys.executable)
    pythonw = os.path.join(python_dir, "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable  # Fall back to python.exe
        print(f"  {WARN} pythonw.exe not found, using python.exe (a console window may appear)")

    task_name = "ScreenRecordService"
    action = f'"{pythonw}" "{run_script}" --config "{config_path}"'

    # Delete existing task if present (ignore errors)
    subprocess.run(
        ["schtasks", "/Delete", "/TN", task_name, "/F"],
        capture_output=True,
        timeout=10,
    )

    try:
        result = subprocess.run(
            [
                "schtasks", "/Create",
                "/TN", task_name,
                "/TR", action,
                "/SC", "ONLOGON",
                "/RL", "HIGHEST",
                "/F",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            print(f"  {CHECK} Scheduled task '{task_name}' created successfully")
            print(f"      The recorder will start automatically when you log in.")
            return True
        else:
            print(f"  {CROSS} Failed to create scheduled task:")
            print(f"      {result.stderr.strip()}")
            print(f"  {WARN} You may need to run this installer as Administrator.")
            return False
    except (subprocess.SubprocessError, OSError) as e:
        print(f"  {CROSS} Failed to run schtasks: {e}")
        return False


def main():
    print_banner()

    results = {}

    # Step 1: Check Python
    print_step(1, "Checking Python version")
    results["python"] = check_python()
    if not results["python"]:
        print(f"\n{RED}{BOLD}Cannot continue without Python 3.8+. Exiting.{RESET}")
        sys.exit(1)

    # Step 2: Check FFmpeg
    print_step(2, "Checking FFmpeg")
    results["ffmpeg"] = check_ffmpeg()

    # Step 3: Check audio setup
    print_step(3, "Checking audio setup")
    results["audio"] = check_audio_setup()

    # Step 4: Install dependencies
    print_step(4, "Installing Python dependencies")
    results["dependencies"] = install_dependencies()

    # Step 5: Create config
    print_step(5, "Creating configuration")
    results["config"] = create_config()

    # Step 6: Setup auto-start
    print_step(6, "Setting up auto-start")
    results["autostart"] = setup_autostart()

    # Summary
    print(f"\n{BOLD}{CYAN}{'=' * 60}")
    print(f"  Installation Summary")
    print(f"{'=' * 60}{RESET}\n")

    status_labels = {
        "python": "Python 3.8+",
        "ffmpeg": "FFmpeg",
        "audio": "Audio setup",
        "dependencies": "Python dependencies",
        "config": "Configuration file",
        "autostart": "Auto-start on login",
    }

    all_ok = True
    for key, label in status_labels.items():
        ok = results.get(key, False)
        icon = CHECK if ok else CROSS
        print(f"  {icon} {label}")
        if not ok:
            all_ok = False

    print()

    if all_ok:
        print(f"  {GREEN}{BOLD}Setup complete! The screen recorder will start automatically on next login.{RESET}")
    else:
        print(f"  {YELLOW}{BOLD}Setup finished with warnings. Please address the issues above.{RESET}")
        print(f"  {YELLOW}The recorder may not work correctly until all checks pass.{RESET}")

    # Offer to start now
    print()
    start_now = input(f"  Would you like to start recording now? (y/N): ").strip().lower()
    if start_now == "y":
        run_script = os.path.join(INSTALL_DIR, "run.py")
        config_path = os.path.join(INSTALL_DIR, "config.yaml")
        if os.path.exists(run_script):
            print(f"\n  Starting screen recorder...")
            try:
                subprocess.Popen(
                    [sys.executable, run_script, "--config", config_path],
                    cwd=INSTALL_DIR,
                )
                print(f"  {CHECK} Recorder started in background.")
            except (subprocess.SubprocessError, OSError) as e:
                print(f"  {CROSS} Failed to start recorder: {e}")
        else:
            print(f"  {CROSS} run.py not found at {run_script}")
    else:
        print(f"\n  You can start manually with:")
        print(f"      {BOLD}python3 {os.path.join(INSTALL_DIR, 'run.py')} --config {os.path.join(INSTALL_DIR, 'config.yaml')}{RESET}")

    print()


if __name__ == "__main__":
    main()
