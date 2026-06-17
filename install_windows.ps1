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
    [string]$ExeUrl = "https://github.com/tyler-bam-ai/screenrecord/releases/download/win-v1.0.0/ScreenRecorder.exe",
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

$localExe = Join-Path $PSScriptRoot "ScreenRecorder.exe"
if (Test-Path $localExe) {
    Copy-Item $localExe $exeDest -Force
    Info "Installed ScreenRecorder.exe from local folder"
} else {
    Info "Downloading ScreenRecorder.exe..."
    Invoke-WebRequest -UseBasicParsing -Uri $ExeUrl -OutFile $exeDest
}
Ok "Installed $exeDest"

# --- 4. Auto-start at logon via Scheduled Task -------------------------------
# Use the ScheduledTasks cmdlets (not schtasks.exe): schtasks writes to stderr
# when there is no existing task to delete, which trips $ErrorActionPreference
# = "Stop" and aborts the install. The cmdlets honor -ErrorAction cleanly.
$taskName = "ScreenRecordAgent"
$action   = New-ScheduledTaskAction -Execute $exeDest
$trigger  = New-ScheduledTaskTrigger -AtLogOn
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Force | Out-Null
Ok "Auto-start registered (Scheduled Task '$taskName', runs at logon)"

# --- 5. Start it now ---------------------------------------------------------
Start-Process -FilePath $exeDest
Ok "Screen Recorder is running. It will appear on the dashboard within ~1 minute."
Write-Host ""
Write-Host "To uninstall: schtasks /Delete /TN $taskName /F; Remove-Item -Recurse -Force '$installDir','$dataDir'"
