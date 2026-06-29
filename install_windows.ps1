# ============================================================================
# Screen Recorder - Windows installer
#
# Installs the ScreenRecorder.exe background agent for one Windows user:
#   - provisions <user profile>\.screenrecord when bootstrap.sh is reachable
#   - installs the exe to <user profile>\AppData\Local\ScreenRecorder
#   - registers that user's Run key for logon auto-start
#   - starts immediately only when running in that same user's context
#
# MDM note:
#   If this script runs as NT AUTHORITY\SYSTEM, it resolves the logged-on user
#   and writes that user's profile + HKU Run key instead of SYSTEM's profile.
#   If no user is logged in, pass -TargetUser "DOMAIN\User" or run in user
#   context. The latest exe also contains baked provisioning values and can
#   self-repair config on first user launch.
# ============================================================================
param(
    [string]$ExeUrl = "https://github.com/tyler-bam-ai/screenrecord/releases/download/windows-latest/ScreenRecorder.exe",
    [string]$BootstrapUrl = "https://raw.githubusercontent.com/tyler-bam-ai/screenrecord/main/bootstrap.sh",
    [string]$TargetUser = "",
    [string]$BootstrapFile = "",
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Info($m) { Write-Host "[*] $m" }
function Ok($m)   { Write-Host "[+] $m" -ForegroundColor Green }

function Test-RunningAsSystem {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return $identity.User.Value -eq "S-1-5-18"
}

function Resolve-InstallUser {
    $name = $TargetUser
    if (-not $name) {
        if (Test-RunningAsSystem) {
            $cs = Get-CimInstance Win32_ComputerSystem
            $name = $cs.UserName
            if (-not $name) {
                throw "No logged-on target user detected. Run the script in user context or pass -TargetUser 'DOMAIN\User'."
            }
        } else {
            $name = "$env:USERDOMAIN\$env:USERNAME"
        }
    }

    $account = New-Object System.Security.Principal.NTAccount($name)
    $sid = $account.Translate([System.Security.Principal.SecurityIdentifier]).Value
    $profileKey = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$sid"
    $profile = (Get-ItemProperty -Path $profileKey -ErrorAction Stop).ProfileImagePath
    $profile = [Environment]::ExpandEnvironmentVariables($profile)
    if (-not (Test-Path $profile)) {
        throw "Resolved profile path does not exist for ${name}: $profile"
    }

    [pscustomobject]@{
        Name = $name
        Sam = (($name -split "\\")[-1])
        Sid = $sid
        Profile = $profile
    }
}

function Get-Baked($boot, $name) {
    if ($boot -match "(?m)^$name=`"([^`"]*)`"") { return $Matches[1] }
    throw "Could not find $name in bootstrap.sh"
}

function Invoke-BestEffort($label, [scriptblock]$action) {
    try {
        & $action
    } catch {
        Info "$label failed: $($_.Exception.Message)"
    }
}

function Repair-TargetPathAccess($path, $target) {
    if (-not (Test-Path -LiteralPath $path)) {
        return
    }

    $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    if (-not $item) {
        return
    }

    Invoke-BestEffort "Clearing file attributes on $path" {
        & attrib.exe -R -S -H $path /S /D 2>$null | Out-Null
    }

    if ($item.PSIsContainer) {
        Invoke-BestEffort "Taking ownership of $path" {
            & takeown.exe /F $path /R /D Y 2>$null | Out-Null
        }
    } else {
        Invoke-BestEffort "Taking ownership of $path" {
            & takeown.exe /F $path 2>$null | Out-Null
        }
    }

    $rights = if ($item.PSIsContainer) { "(OI)(CI)F" } else { "F" }
    $grants = @(
        "*$($target.Sid):$rights",
        "*S-1-5-18:$rights",
        "*S-1-5-32-544:$rights"
    )

    Invoke-BestEffort "Repairing ACL on $path" {
        $args = @($path, "/inheritance:e", "/grant:r") + $grants
        if ($item.PSIsContainer) {
            $args += @("/T", "/C")
        }
        & icacls.exe @args 2>$null | Out-Null
    }
}

function Write-ManagedBytes($path, [byte[]]$bytes, $target) {
    $parent = Split-Path -Parent $path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Repair-TargetPathAccess $parent $target

    if (Test-Path -LiteralPath $path) {
        Repair-TargetPathAccess $path $target
        Remove-Item -LiteralPath $path -Force -ErrorAction Stop
    }

    $tmp = Join-Path $parent (".$([IO.Path]::GetFileName($path)).$([guid]::NewGuid()).tmp")
    try {
        [IO.File]::WriteAllBytes($tmp, $bytes)
        Move-Item -LiteralPath $tmp -Destination $path -Force
        Repair-TargetPathAccess $path $target
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}

function Write-ManagedText($path, [string]$text, $target) {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    Write-ManagedBytes $path ($utf8.GetBytes($text)) $target
}

function Ensure-UserHiveLoaded($target) {
    $sidPath = "Registry::HKEY_USERS\$($target.Sid)"
    if (Test-Path $sidPath) {
        return $false
    }

    $ntUser = Join-Path $target.Profile "NTUSER.DAT"
    if (-not (Test-Path $ntUser)) {
        throw "Cannot load user hive; missing $ntUser"
    }
    Info "Loading registry hive for $($target.Name)"
    & reg.exe load "HKU\$($target.Sid)" "$ntUser" | Out-Null
    return $true
}

function Unload-UserHiveIfNeeded($target, [bool]$loadedByUs) {
    if ($loadedByUs) {
        [gc]::Collect()
        Start-Sleep -Milliseconds 250
        & reg.exe unload "HKU\$($target.Sid)" | Out-Null
    }
}

$target = Resolve-InstallUser
Info "Installing for $($target.Name) ($($target.Sid)) at $($target.Profile)"

# Stop the existing agent before touching provisioned files. Older builds can
# leave credentials/config read-only or owned by another context.
Stop-Process -Name ScreenRecorder -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

# --- 1. Fetch deployment values if possible ----------------------------------
$boot = $null
if ($BootstrapFile) {
    Info "Reading deployment configuration from $BootstrapFile..."
    $boot = Get-Content -LiteralPath $BootstrapFile -Raw
} else {
    try {
        Info "Fetching deployment configuration..."
        $boot = (Invoke-WebRequest -UseBasicParsing -Uri $BootstrapUrl).Content
    } catch {
        Info "Could not fetch bootstrap.sh; latest exe will self-provision from baked values on first launch."
    }
}

# --- 2. Provision the target user's data directory ---------------------------
$dataDir = Join-Path $target.Profile ".screenrecord"
$recDir = Join-Path $dataDir "recordings"
New-Item -ItemType Directory -Force -Path $recDir | Out-Null
Repair-TargetPathAccess $dataDir $target

if ($boot) {
    $credsB64 = Get-Baked $boot "GDRIVE_CREDENTIALS_B64"
    $keyB64   = Get-Baked $boot "ENCRYPTION_KEY_B64"
    $folderId = Get-Baked $boot "GDRIVE_FOLDER_ID"
    $sheetId  = Get-Baked $boot "GSHEET_ID"
    $client   = Get-Baked $boot "CLIENT_NAME"

    Write-ManagedBytes (Join-Path $dataDir "credentials.json") ([Convert]::FromBase64String($credsB64)) $target
    Write-ManagedBytes (Join-Path $dataDir "encryption.key")  ([Convert]::FromBase64String($keyB64)) $target

    $employee = $target.Sam
    $computer = $env:COMPUTERNAME
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

input_monitor:
  enabled: false
  capture_keystroke_text: true
  screenshot_min_interval: 0.0

google_sheets:
  sheet_id: "$sheetId"

rag:
  enabled: false
"@
    Write-ManagedText (Join-Path $dataDir "config.yaml") $config $target
    Ok "Provisioned $dataDir ($employee / $computer / $client)"
} else {
    Ok "Created $dataDir; config will be written by the exe self-provisioner."
}

# --- 3. Install the executable ------------------------------------------------
$installDir = Join-Path (Join-Path $target.Profile "AppData\Local") "ScreenRecorder"
New-Item -ItemType Directory -Force -Path $installDir | Out-Null
$exeDest = Join-Path $installDir "ScreenRecorder.exe"

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

# --- 4. Auto-start for the target user ---------------------------------------
$hiveLoadedByUs = $false
try {
    $hiveLoadedByUs = Ensure-UserHiveLoaded $target
    $runKey = "Registry::HKEY_USERS\$($target.Sid)\Software\Microsoft\Windows\CurrentVersion\Run"
    New-Item -Path $runKey -Force | Out-Null
    Set-ItemProperty -Path $runKey -Name "ScreenRecordAgent" -Value "`"$exeDest`""
    Ok "Auto-start registered for $($target.Name)"
} finally {
    Unload-UserHiveIfNeeded $target $hiveLoadedByUs
}

# --- 5. Start now if safe -----------------------------------------------------
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if ($NoStart) {
    Ok "Installed for $($target.Name). Start skipped by -NoStart."
} elseif (-not (Test-RunningAsSystem) -and $currentSid -eq $target.Sid) {
    Start-Process -FilePath $exeDest
    Ok "Screen Recorder is running. It will appear on the dashboard within ~1 minute."
} else {
    Ok "Installed for $($target.Name). It will start at that user's next logon."
}

Write-Host ""
Write-Host "Static exe: $ExeUrl"
Write-Host "Uninstall for this user: remove HKU\\$($target.Sid)\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\ScreenRecordAgent, stop ScreenRecorder, then delete '$installDir' and '$dataDir'."
