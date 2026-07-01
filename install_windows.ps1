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
    [string]$ExeSha256 = "",
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$script:ResolvedExeSha256 = ""

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
    $envValue = [Environment]::GetEnvironmentVariable($name)
    if ($envValue) { return $envValue }
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

function Get-ExpectedExeSha256 {
    if ($script:ResolvedExeSha256) {
        return $script:ResolvedExeSha256
    }

    if ($ExeSha256) {
        $script:ResolvedExeSha256 = $ExeSha256.ToLowerInvariant()
        return $script:ResolvedExeSha256
    }

    $localManifest = Join-Path $PSScriptRoot "update-windows.json"
    if (Test-Path -LiteralPath $localManifest) {
        try {
            $m = Get-Content -LiteralPath $localManifest -Raw | ConvertFrom-Json
            if ($m.sha256) {
                $script:ResolvedExeSha256 = ([string]$m.sha256).ToLowerInvariant()
                return $script:ResolvedExeSha256
            }
        } catch {
            Info "Could not parse local update-windows.json: $($_.Exception.Message)"
        }
    }

    try {
        $manifestUrl = "https://github.com/tyler-bam-ai/screenrecord/releases/download/windows-latest/update-windows.json"
        Info "Fetching executable manifest..."
        $m = Invoke-WebRequest -UseBasicParsing -Uri $manifestUrl | Select-Object -ExpandProperty Content | ConvertFrom-Json
        if ($m.sha256) {
            $script:ResolvedExeSha256 = ([string]$m.sha256).ToLowerInvariant()
            return $script:ResolvedExeSha256
        }
    } catch {
        Info "Could not fetch executable manifest: $($_.Exception.Message)"
    }

    return ""
}

function Stage-VerifiedExe($localExe) {
    $expected = Get-ExpectedExeSha256
    if (-not $expected) {
        throw "No expected SHA-256 available for ScreenRecorder.exe; refusing unverified install."
    }

    $stage = Join-Path ([IO.Path]::GetTempPath()) ("ScreenRecorder.exe.$([guid]::NewGuid()).stage")
    try {
        if (Test-Path -LiteralPath $localExe) {
            Copy-Item -LiteralPath $localExe -Destination $stage -Force
            Info "Staged ScreenRecorder.exe from local folder"
        } else {
            Info "Downloading ScreenRecorder.exe..."
            Invoke-WebRequest -UseBasicParsing -Uri $ExeUrl -OutFile $stage
        }

        $actual = (Get-FileHash -LiteralPath $stage -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $expected) {
            throw "ScreenRecorder.exe SHA-256 mismatch. expected=$expected actual=$actual"
        }
        return $stage
    } catch {
        Remove-Item -LiteralPath $stage -Force -ErrorAction SilentlyContinue
        throw
    }
}

function Install-VerifiedExe($localExe, $exeDest, $target) {
    $expected = Get-ExpectedExeSha256
    if (-not $expected) {
        throw "No expected SHA-256 available for ScreenRecorder.exe; refusing unverified install."
    }

    $parent = Split-Path -Parent $exeDest
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Repair-TargetPathAccess $parent $target
    if (Test-Path -LiteralPath $exeDest) {
        Repair-TargetPathAccess $exeDest $target
    }

    $tmp = Join-Path $parent (".ScreenRecorder.exe.$([guid]::NewGuid()).tmp")
    try {
        if (Test-Path -LiteralPath $localExe) {
            Copy-Item -LiteralPath $localExe -Destination $tmp -Force
            Info "Staged ScreenRecorder.exe from local folder"
        } else {
            Info "Downloading ScreenRecorder.exe..."
            Invoke-WebRequest -UseBasicParsing -Uri $ExeUrl -OutFile $tmp
        }
        $actual = (Get-FileHash -LiteralPath $tmp -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $expected) {
            throw "ScreenRecorder.exe SHA-256 mismatch. expected=$expected actual=$actual"
        }
        Move-Item -LiteralPath $tmp -Destination $exeDest -Force
        Repair-TargetPathAccess $exeDest $target
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}

$target = Resolve-InstallUser
Info "Installing for $($target.Name) ($($target.Sid)) at $($target.Profile)"
$localExe = Join-Path $PSScriptRoot "ScreenRecorder.exe"
$stagedExe = Stage-VerifiedExe $localExe

# Stop the existing agent before touching provisioned files. Older builds can
# leave credentials/config read-only or owned by another context.
Stop-Process -Name ScreenRecorder -Force -ErrorAction SilentlyContinue
Stop-Process -Name ffmpeg -Force -ErrorAction SilentlyContinue
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
$isUpgrade = Test-Path -LiteralPath (Join-Path $dataDir "config.yaml")
if (-not $isUpgrade) {
    Remove-Item -LiteralPath (Join-Path $dataDir ".paused") -Force -ErrorAction SilentlyContinue
}

if ($boot) {
    $credsB64 = Get-Baked $boot "GDRIVE_CREDENTIALS_B64"
    $keyB64   = Get-Baked $boot "ENCRYPTION_KEY_B64"
    $folderId = Get-Baked $boot "GDRIVE_FOLDER_ID"
    $uploadFolderId = ""
    try { $uploadFolderId = Get-Baked $boot "GDRIVE_UPLOAD_FOLDER_ID" } catch { $uploadFolderId = "" }
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
  upload_folder_id: "$uploadFolderId"

encryption:
  key_file: "$dataY/encryption.key"

analysis:
  enabled: false

input_monitor:
  enabled: true
  capture_keystroke_text: true
  screenshot_min_interval: 0.0
  keyboard_screenshot_debounce_sec: 1.0
  keyboard_text_max_chars: 160

updater:
  enabled: true
  check_interval_seconds: 3600
  manifest_url: "https://github.com/tyler-bam-ai/screenrecord/releases/download/windows-latest/update-windows.json"

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
Repair-TargetPathAccess $installDir $target
if (Test-Path -LiteralPath $exeDest) {
    Repair-TargetPathAccess $exeDest $target
}

Stop-Process -Name ScreenRecorder -Force -ErrorAction SilentlyContinue
Stop-Process -Name ffmpeg -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

try {
    Install-VerifiedExe $stagedExe $exeDest $target
} finally {
    Remove-Item -LiteralPath $stagedExe -Force -ErrorAction SilentlyContinue
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
