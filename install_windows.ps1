# ============================================================================
# Screen Recorder - Windows installer
#
# Installs the ScreenRecorder.exe background agent for the current user:
#   - provisions %USERPROFILE%\.screenrecord (config, credentials, encryption key)
#   - registers a Scheduled Task that starts the agent at every logon
#   - starts it immediately
#
# Deployment values (Drive credentials, encryption key, sheet id, client name)
# are read from bootstrap.sh in the repo, so there is a single source of truth
# shared with the macOS installer.
#
# Usage (run in PowerShell, no admin required):
#   ScreenRecorder.exe and this script should sit in the same folder.
#   powershell -ExecutionPolicy Bypass -File install_windows.ps1
# ============================================================================
param(
    [string]$ExeUrl = "https://github.com/tyler-bam-ai/screenrecord/releases/download/win-v1.0.3/ScreenRecorder.exe",
    [string]$BootstrapUrl = "https://raw.githubusercontent.com/tyler-bam-ai/screenrecord/main/bootstrap.sh"
)
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Info($m) { Write-Host "[*] $m" }
function Ok($m)   { Write-Host "[+] $m" -ForegroundColor Green }

# --- 1. Read baked deployment values from bootstrap.sh -----------------------
Info "Fetching deployment configuration..."
$boot = (Invoke-WebRequest -UseBasicParsing -Uri $BootstrapUrl).Content

function Get-Baked($name) {
    if ($boot -match "(?m)^$name=`"([^`"]*)`"") { return $Matches[1] }
    throw "Could not find $name in bootstrap.sh"
}
$credsB64  = Get-Baked "GDRIVE_CREDENTIALS_B64"
$keyB64    = Get-Baked "ENCRYPTION_KEY_B64"
$folderId  = Get-Baked "GDRIVE_FOLDER_ID"
$sheetId   = Get-Baked "GSHEET_ID"
$client    = Get-Baked "CLIENT_NAME"

# --- 2. Provision the data directory -----------------------------------------
$dataDir = Join-Path $env:USERPROFILE ".screenrecord"
$recDir  = Join-Path $dataDir "recordings"
New-Item -ItemType Directory -Force -Path $recDir | Out-Null

[IO.File]::WriteAllBytes((Join-Path $dataDir "credentials.json"), [Convert]::FromBase64String($credsB64))
[IO.File]::WriteAllBytes((Join-Path $dataDir "encryption.key"),  [Convert]::FromBase64String($keyB64))

$employee = $env:USERNAME
$computer = $env:COMPUTERNAME
# YAML wants forward slashes for paths.
$dataY = $dataDir -replace '\\','/'
$config = @"
client_name: "$client"
employee_name: "$employee"
computer_name: "$computer"

recording:
  fps: 5
  crf: 28
  segment_duration: 3600
  output_dir: "$dataY/recordings"
  audio_device: ""

google_drive:
  credentials_file: "$dataY/credentials.json"
  root_folder_id: "$folderId"

encryption:
  key_file: "$dataY/encryption.key"

analysis:
  enabled: false

google_sheets:
  sheet_id: "$sheetId"

rag:
  enabled: false
"@
Set-Content -Path (Join-Path $dataDir "config.yaml") -Value $config -Encoding UTF8
Ok "Provisioned $dataDir ($employee / $computer / $client)"

# --- 3. Install the executable -----------------------------------------------
$installDir = Join-Path $env:LOCALAPPDATA "ScreenRecorder"
New-Item -ItemType Directory -Force -Path $installDir | Out-Null
$exeDest = Join-Path $installDir "ScreenRecorder.exe"

# Stop any running instance first, or the exe file is locked and the copy below
# fails (this is an upgrade/reinstall, not just a fresh install).
Stop-Process -Name ScreenRecorder -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

$localExe = Join-Path $PSScriptRoot "ScreenRecorder.exe"
if (Test-Path $localExe) {
    Copy-Item $localExe $exeDest -Force
    Info "Installed ScreenRecorder.exe from local folder"
} else {
    Info "Downloading ScreenRecorder.exe..."
    Invoke-WebRequest -UseBasicParsing -Uri $ExeUrl -OutFile $exeDest
}
Ok "Installed $exeDest"

# --- 4. Auto-start at logon (per-user Run key, no admin needed) --------------
# A Scheduled Task via Register-ScheduledTask needs elevation (Access denied for
# a standard user), and schtasks.exe writes to stderr when deleting a missing
# task (trips ErrorActionPreference=Stop). The per-user Run key under HKCU is
# always writable by the current user and starts the agent at every logon.
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
Set-ItemProperty -Path $runKey -Name "ScreenRecordAgent" -Value "`"$exeDest`""
Ok "Auto-start registered (per-user logon Run entry)"

# --- 5. Start it now ---------------------------------------------------------
Start-Process -FilePath $exeDest
Ok "Screen Recorder is running. It will appear on the dashboard within ~1 minute."
Write-Host ""
Write-Host "To uninstall: Remove-ItemProperty '$runKey' ScreenRecordAgent; Stop-Process -Name ScreenRecorder -Force; Remove-Item -Recurse -Force '$installDir','$dataDir'"
