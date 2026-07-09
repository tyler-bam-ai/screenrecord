"""Install and repair the per-user Windows watchdog without console flashes.

The scheduled task must not execute ``powershell.exe`` directly.  Even with
``-WindowStyle Hidden``, an interactive scheduled task can briefly create a
visible console window every time it runs.  The task therefore launches the
GUI-subsystem ``wscript.exe`` host, which starts the PowerShell worker with
window style 0 (hidden).

Existing installations self-heal on the first launch of a build containing
this module, so fixing the fleet does not require running the installer again.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from .platform_utils import hidden_subprocess_kwargs

logger = logging.getLogger(__name__)

TASK_NAME = "ScreenRecorderWatchdog"


def _ps_quote(value: str) -> str:
    return str(value).replace("'", "''")


def build_watchdog_script(exe_path: Path, marker_path: Path) -> str:
    """Return the PowerShell worker that relaunches or updates the agent."""
    exe = _ps_quote(str(exe_path))
    marker = _ps_quote(str(marker_path))
    return f"""$ErrorActionPreference = "SilentlyContinue"
$exe = '{exe}'
$marker = '{marker}'
if (Get-Process ScreenRecorder -ErrorAction SilentlyContinue) {{ return }}
if (Test-Path -LiteralPath $marker) {{
  try {{
    $pu = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
    $new = $pu.new_exe
    if ($new -and (Test-Path -LiteralPath $new)) {{
      Copy-Item -LiteralPath $new -Destination $exe -Force
      if ((Get-Item -LiteralPath $exe).Length -eq (Get-Item -LiteralPath $new).Length) {{
        Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue
        Start-Process -FilePath $exe
        return
      }}
    }}
  }} catch {{ }}
  Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue
}}
if (Test-Path -LiteralPath $exe) {{ Start-Process -FilePath $exe }}
"""


def build_vbs_launcher(watchdog_script: Path) -> str:
    """Return a GUI-subsystem launcher that never allocates a console window."""
    path = str(watchdog_script).replace('"', '""')
    return (
        'Option Explicit\n'
        'Dim shell\n'
        'Set shell = CreateObject("WScript.Shell")\n'
        'shell.Run "powershell.exe -NoProfile -NonInteractive '
        '-ExecutionPolicy Bypass -File ""' + path + '""", 0, True\n'
    )


def build_registration_script(vbs_launcher: Path) -> str:
    """Return PowerShell that replaces the task action with hidden WScript."""
    vbs = _ps_quote(str(vbs_launcher))
    return f"""$ErrorActionPreference = "Stop"
$vbs = '{vbs}'
$wscript = Join-Path $env:SystemRoot "System32\\wscript.exe"
$actionArgs = '//B //NoLogo "' + $vbs + '"'
$act = New-ScheduledTaskAction -Execute $wscript -Argument $actionArgs
$trg = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(2)) `
  -RepetitionInterval (New-TimeSpan -Minutes 3) `
  -RepetitionDuration (New-TimeSpan -Days 3650)
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$prn = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "{TASK_NAME}" -Action $act -Trigger $trg `
  -Principal $prn -Force | Out-Null
"""


def _task_is_hidden() -> bool:
    try:
        result = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", TASK_NAME, "/XML"],
            capture_output=True,
            text=True,
            timeout=15,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    xml = (result.stdout or "").lower()
    return (
        result.returncode == 0
        and "wscript.exe" in xml
        and "watchdog.vbs" in xml
    )


def ensure_hidden_watchdog() -> bool:
    """Repair legacy PowerShell task actions in-place for the current user."""
    if os.name != "nt":
        return False

    install_dir = Path(sys.executable).resolve().parent
    exe_path = install_dir / "ScreenRecorder.exe"
    marker_path = install_dir / "pending_update.json"
    ps1_path = install_dir / "watchdog.ps1"
    vbs_path = install_dir / "watchdog.vbs"
    register_path = install_dir / "register_watchdog.ps1"

    try:
        install_dir.mkdir(parents=True, exist_ok=True)
        ps1_path.write_text(
            build_watchdog_script(exe_path, marker_path), encoding="utf-8"
        )
        vbs_path.write_text(build_vbs_launcher(ps1_path), encoding="utf-8")
        if _task_is_hidden():
            logger.info("Windows watchdog already uses the hidden WScript launcher.")
            return True

        register_path.write_text(
            build_registration_script(vbs_path), encoding="utf-8"
        )
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / (
            r"System32\WindowsPowerShell\v1.0\powershell.exe"
        )
        command = str(powershell) if powershell.exists() else "powershell.exe"
        result = subprocess.run(
            [
                command,
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(register_path),
            ],
            capture_output=True,
            text=True,
            timeout=45,
            **hidden_subprocess_kwargs(),
        )
        if result.returncode != 0:
            logger.error(
                "Could not install hidden Windows watchdog (exit=%s): %s",
                result.returncode,
                (result.stderr or result.stdout or "")[-500:],
            )
            return False
        logger.info("Windows watchdog repaired to use hidden WScript launcher.")
        return True
    except Exception:
        logger.exception("Failed to repair Windows watchdog task")
        return False

