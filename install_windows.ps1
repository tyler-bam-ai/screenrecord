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
    [string]$ExeSha256 = "__SCREENRECORDER_EXE_SHA256__",
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$script:ResolvedExeSha256 = ""
$script:InstallPhase = "start"
$script:InstallTarget = $null
$script:StagedExeSha256 = ""
$script:StoppedExistingProcesses = $false
$script:TranscriptPath = Join-Path (Join-Path $env:ProgramData "ScreenRecorder") "install.log"

try {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $script:TranscriptPath) | Out-Null
    Start-Transcript -Path $script:TranscriptPath -Append | Out-Null
} catch {
    Write-Host "[*] Could not start installer transcript: $($_.Exception.Message)"
}

function Info($m) { Write-Host "[*] $m" }
function Ok($m)   { Write-Host "[+] $m" -ForegroundColor Green }

function Write-InstallDiagnostic($kind, $errorRecord) {
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("kind=$kind")
    $lines.Add("timestamp=$((Get-Date).ToUniversalTime().ToString('o'))")
    $lines.Add("phase=$script:InstallPhase")
    $lines.Add("identity=$([Security.Principal.WindowsIdentity]::GetCurrent().Name)")
    $lines.Add("computer=$env:COMPUTERNAME")
    $lines.Add("transcript=$script:TranscriptPath")
    $lines.Add("expected_exe_sha256=$script:ResolvedExeSha256")
    $lines.Add("staged_exe_sha256=$script:StagedExeSha256")
    $lines.Add("stopped_existing_processes=$script:StoppedExistingProcesses")
    if ($script:InstallTarget) {
        $lines.Add("target_name=$($script:InstallTarget.Name)")
        $lines.Add("target_sid=$($script:InstallTarget.Sid)")
        $lines.Add("target_profile=$($script:InstallTarget.Profile)")
    }
    if ($errorRecord) {
        $lines.Add("error=$($errorRecord.Exception.Message)")
        $lines.Add("position=$($errorRecord.InvocationInfo.PositionMessage)")
        $lines.Add("category=$($errorRecord.CategoryInfo)")
        $lines.Add("fully_qualified_error_id=$($errorRecord.FullyQualifiedErrorId)")
    }

    $text = ($lines -join [Environment]::NewLine) + [Environment]::NewLine
    $paths = @()
    $paths += Join-Path (Join-Path $env:ProgramData "ScreenRecorder") "install_diagnostic.txt"
    if ($env:PUBLIC) {
        $paths += Join-Path (Join-Path $env:PUBLIC "Documents") "ScreenRecorder_install_diagnostic.txt"
    }
    if ($script:InstallTarget -and $script:InstallTarget.Profile) {
        $paths += Join-Path (Join-Path $script:InstallTarget.Profile ".screenrecord") "install_diagnostic.txt"
    }

    foreach ($path in $paths) {
        try {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $path) | Out-Null
            Add-Content -LiteralPath $path -Value $text -Encoding UTF8
        } catch {
            Write-Host "[*] Could not write install diagnostic to ${path}: $($_.Exception.Message)"
        }
    }
}

trap {
    Write-InstallDiagnostic "failure" $_
    try { Stop-Transcript | Out-Null } catch {}
    exit 1
}

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

function Normalize-Sha256($value) {
    $sha = ([string]$value).Trim()
    if ($sha -match '^[a-fA-F0-9]{64}$') {
        return $sha.ToLowerInvariant()
    }
    return ""
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

    $embedded = Normalize-Sha256 $ExeSha256
    if ($embedded) {
        $script:ResolvedExeSha256 = $embedded
        return $script:ResolvedExeSha256
    }

    $localManifest = Join-Path $PSScriptRoot "update-windows.json"
    if (Test-Path -LiteralPath $localManifest) {
        try {
            $m = Get-Content -LiteralPath $localManifest -Raw | ConvertFrom-Json
            $sha = Normalize-Sha256 $m.sha256
            if ($sha) {
                $script:ResolvedExeSha256 = $sha
                return $script:ResolvedExeSha256
            }
        } catch {
            Info "Could not parse local update-windows.json: $($_.Exception.Message)"
        }
    }

    try {
        $manifestUrl = "https://github.com/tyler-bam-ai/screenrecord/releases/download/windows-latest/update-windows.json"
        Info "Fetching executable manifest..."
        $content = [string](Invoke-WebRequest -UseBasicParsing -Uri $manifestUrl).Content
        $m = $content.TrimStart([char]0xfeff) | ConvertFrom-Json
        $sha = Normalize-Sha256 $m.sha256
        if ($sha) {
            $script:ResolvedExeSha256 = $sha
            return $script:ResolvedExeSha256
        }
    } catch {
        Info "Could not fetch executable manifest: $($_.Exception.Message)"
    }

    try {
        $releaseApi = "https://api.github.com/repos/tyler-bam-ai/screenrecord/releases/tags/windows-latest"
        Info "Fetching GitHub release asset digest..."
        $release = ([string](Invoke-WebRequest -UseBasicParsing -Uri $releaseApi).Content).TrimStart([char]0xfeff) | ConvertFrom-Json
        $asset = $release.assets | Where-Object { $_.name -eq "ScreenRecorder.exe" } | Select-Object -First 1
        if ($asset -and $asset.digest -match '^sha256:(.+)$') {
            $sha = Normalize-Sha256 $Matches[1]
            if ($sha) {
                $script:ResolvedExeSha256 = $sha
                return $script:ResolvedExeSha256
            }
        }
    } catch {
        Info "Could not fetch GitHub release asset digest: $($_.Exception.Message)"
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
        $script:StagedExeSha256 = $actual
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
        $script:StagedExeSha256 = $actual
        if ($actual -ne $expected) {
            throw "ScreenRecorder.exe SHA-256 mismatch. expected=$expected actual=$actual"
        }
        Move-Item -LiteralPath $tmp -Destination $exeDest -Force
        Repair-TargetPathAccess $exeDest $target
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}

$script:InstallPhase = "resolve_target"
$target = Resolve-InstallUser
$script:InstallTarget = $target
Info "Installing for $($target.Name) ($($target.Sid)) at $($target.Profile)"
$localExe = Join-Path $PSScriptRoot "ScreenRecorder.exe"
$script:InstallPhase = "stage_verified_exe"
$stagedExe = Stage-VerifiedExe $localExe

# --- 1. Fetch and validate deployment values if possible ----------------------
$script:InstallPhase = "fetch_validate_bootstrap"
$boot = $null
$provision = $null
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

if ($boot) {
    $credsB64 = Get-Baked $boot "GDRIVE_CREDENTIALS_B64"
    $keyB64   = Get-Baked $boot "ENCRYPTION_KEY_B64"
    $folderId = Get-Baked $boot "GDRIVE_FOLDER_ID"
    $uploadFolderId = ""
    try { $uploadFolderId = Get-Baked $boot "GDRIVE_UPLOAD_FOLDER_ID" } catch { $uploadFolderId = "" }
    $heartbeatFolderId = ""
    try { $heartbeatFolderId = Get-Baked $boot "GDRIVE_HEARTBEAT_FOLDER_ID" } catch { $heartbeatFolderId = "" }
    $diagnosticsFolderId = ""
    try { $diagnosticsFolderId = Get-Baked $boot "GDRIVE_DIAGNOSTICS_FOLDER_ID" } catch { $diagnosticsFolderId = "" }
    $sheetId  = Get-Baked $boot "GSHEET_ID"
    $client   = Get-Baked $boot "CLIENT_NAME"

    $provision = [pscustomobject]@{
        CredentialsBytes = [Convert]::FromBase64String($credsB64)
        KeyBytes = [Convert]::FromBase64String($keyB64)
        FolderId = $folderId
        UploadFolderId = $uploadFolderId
        HeartbeatFolderId = $heartbeatFolderId
        DiagnosticsFolderId = $diagnosticsFolderId
        SheetId = $sheetId
        Client = $client
    }
}

# Stop the existing agent only after the new exe and provisioning inputs have
# been validated. Older builds can leave config read-only or owned by another
# context, so the repair/write path still runs after shutdown.
$script:InstallPhase = "stop_existing_processes"
Stop-Process -Name ScreenRecorder -Force -ErrorAction SilentlyContinue
Stop-Process -Name ffmpeg -Force -ErrorAction SilentlyContinue
$script:StoppedExistingProcesses = $true
Start-Sleep -Seconds 1

# --- 2. Provision the target user's data directory ---------------------------
$script:InstallPhase = "provision_target_profile"
$dataDir = Join-Path $target.Profile ".screenrecord"
$recDir = Join-Path $dataDir "recordings"
New-Item -ItemType Directory -Force -Path $recDir | Out-Null
Repair-TargetPathAccess $dataDir $target
$isUpgrade = Test-Path -LiteralPath (Join-Path $dataDir "config.yaml")
if (-not $isUpgrade) {
    Remove-Item -LiteralPath (Join-Path $dataDir ".paused") -Force -ErrorAction SilentlyContinue
}

if ($provision) {
    Write-ManagedBytes (Join-Path $dataDir "credentials.json") $provision.CredentialsBytes $target
    Write-ManagedBytes (Join-Path $dataDir "encryption.key")  $provision.KeyBytes $target

    $employee = $target.Sam
    $computer = $env:COMPUTERNAME
    $dataY = $dataDir -replace '\\','/'
    $config = @"
client_name: "$($provision.Client)"
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
  root_folder_id: "$($provision.FolderId)"
  upload_folder_id: "$($provision.UploadFolderId)"
  heartbeat_folder_id: "$($provision.HeartbeatFolderId)"
  diagnostics_folder_id: "$($provision.DiagnosticsFolderId)"
  allow_public_links: false

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
  sheet_id: "$($provision.SheetId)"
  make_public: false

rag:
  enabled: false
"@
    Write-ManagedText (Join-Path $dataDir "config.yaml") $config $target
    Ok "Provisioned $dataDir ($employee / $computer / $($provision.Client))"
} else {
    Ok "Created $dataDir; config will be written by the exe self-provisioner."
}

# --- 3. Install the executable ------------------------------------------------
$script:InstallPhase = "install_verified_exe"
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
$script:InstallPhase = "register_autostart"
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
$script:InstallPhase = "start_or_defer"
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
$script:InstallPhase = "complete"
Write-InstallDiagnostic "success" $null
try { Stop-Transcript | Out-Null } catch {}
