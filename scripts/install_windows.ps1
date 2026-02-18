# install_windows.ps1
# Windows installer script for the Screen Recording Service.
# Run as: powershell -ExecutionPolicy Bypass -File install_windows.ps1

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectDir = Split-Path -Parent $ScriptDir

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "     Screen Recording Service - Windows Installer" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# --- Check Python ---
Write-Host "[Step 1] Checking Python..." -ForegroundColor Cyan
try {
    $pythonVersion = & python --version 2>&1
    if ($pythonVersion -match "Python (\d+)\.(\d+)") {
        $major = [int]$Matches[1]
        $minor = [int]$Matches[2]
        if ($major -ge 3 -and $minor -ge 8) {
            Write-Host "  OK: $pythonVersion" -ForegroundColor Green
        } else {
            Write-Host "  ERROR: Python 3.8+ is required (found $pythonVersion)" -ForegroundColor Red
            Write-Host "  Download from https://python.org" -ForegroundColor Yellow
            exit 1
        }
    }
} catch {
    Write-Host "  ERROR: Python not found. Install from https://python.org" -ForegroundColor Red
    exit 1
}

# --- Check FFmpeg ---
Write-Host ""
Write-Host "[Step 2] Checking FFmpeg..." -ForegroundColor Cyan
$ffmpegPath = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ($ffmpegPath) {
    $ffmpegVersion = & ffmpeg -version 2>&1 | Select-Object -First 1
    Write-Host "  OK: $ffmpegVersion" -ForegroundColor Green
} else {
    Write-Host "  WARNING: FFmpeg not found." -ForegroundColor Yellow
    Write-Host "  Install with:  winget install ffmpeg" -ForegroundColor Yellow
    Write-Host "  Or download from https://ffmpeg.org/download.html" -ForegroundColor Yellow
}

# --- Install pip requirements ---
Write-Host ""
Write-Host "[Step 3] Installing Python dependencies..." -ForegroundColor Cyan
$requirementsPath = Join-Path $ProjectDir "requirements.txt"
if (Test-Path $requirementsPath) {
    try {
        & python -m pip install -r $requirementsPath
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  OK: Dependencies installed." -ForegroundColor Green
        } else {
            Write-Host "  WARNING: pip install returned exit code $LASTEXITCODE" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "  ERROR: Failed to install dependencies: $_" -ForegroundColor Red
    }
} else {
    Write-Host "  WARNING: requirements.txt not found at $requirementsPath" -ForegroundColor Yellow
}

# --- Create Scheduled Task ---
Write-Host ""
Write-Host "[Step 4] Setting up auto-start scheduled task..." -ForegroundColor Cyan

$taskName = "ScreenRecordService"
$runScript = Join-Path $ProjectDir "run.py"
$configPath = Join-Path $ProjectDir "config.yaml"

# Find pythonw.exe for windowless execution
$pythonExe = (Get-Command python).Source
$pythonDir = Split-Path -Parent $pythonExe
$pythonwExe = Join-Path $pythonDir "pythonw.exe"
if (-not (Test-Path $pythonwExe)) {
    Write-Host "  WARNING: pythonw.exe not found, using python.exe (console window may appear)" -ForegroundColor Yellow
    $pythonwExe = $pythonExe
}

# Remove existing task if present
schtasks /Delete /TN $taskName /F 2>$null | Out-Null

$action = "`"$pythonwExe`" `"$runScript`" --config `"$configPath`""
try {
    schtasks /Create /TN $taskName /TR $action /SC ONLOGON /RL HIGHEST /F
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  OK: Scheduled task '$taskName' created." -ForegroundColor Green
        Write-Host "  The recorder will start automatically when you log in." -ForegroundColor Green
    } else {
        Write-Host "  ERROR: Failed to create scheduled task (exit code $LASTEXITCODE)." -ForegroundColor Red
        Write-Host "  Try running this script as Administrator." -ForegroundColor Yellow
    }
} catch {
    Write-Host "  ERROR: Failed to create scheduled task: $_" -ForegroundColor Red
    Write-Host "  Try running this script as Administrator." -ForegroundColor Yellow
}

# --- Summary ---
Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Installation Complete" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Next steps:" -ForegroundColor White
Write-Host "    1. Run the Python installer for interactive config setup:" -ForegroundColor White
Write-Host "       python `"$ProjectDir\install.py`"" -ForegroundColor Yellow
Write-Host ""
Write-Host "    2. Or start recording manually:" -ForegroundColor White
Write-Host "       python `"$runScript`" --config `"$configPath`"" -ForegroundColor Yellow
Write-Host ""
