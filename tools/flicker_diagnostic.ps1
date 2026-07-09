# ScreenRecorder — Windows screen-flicker diagnostic.
# Run this on the affected machine WHILE the flickering is happening, then send
# the file it writes to the Desktop back to the developer.
#   irm https://github.com/tyler-bam-ai/screenrecord/releases/download/windows-latest/flicker_diagnostic.ps1 | iex
$ErrorActionPreference = "SilentlyContinue"
$out = Join-Path ([Environment]::GetFolderPath("Desktop")) "ScreenRecorder_flicker_diagnostic.txt"
$sr  = Join-Path $env:USERPROFILE ".screenrecord"

function Section($t) { "`n===== $t =====" | Out-File $out -Append -Encoding utf8 }

"ScreenRecorder Flicker Diagnostic   $(Get-Date -Format o)" | Out-File $out -Encoding utf8
"user=$env:USERNAME   host=$env:COMPUTERNAME" | Out-File $out -Append -Encoding utf8

Section "OS"
Get-CimInstance Win32_OperatingSystem |
  Select-Object Caption, Version, BuildNumber, OSArchitecture | Format-List | Out-File $out -Append -Encoding utf8

Section "GPU / display adapters (driver name + version + date matter most)"
Get-CimInstance Win32_VideoController |
  Select-Object Name, DriverVersion, DriverDate, CurrentHorizontalResolution,
    CurrentVerticalResolution, CurrentRefreshRate, VideoModeDescription, AdapterRAM |
  Format-List | Out-File $out -Append -Encoding utf8

Section "Monitors (count + resolution + scaling)"
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Screen]::AllScreens |
  Select-Object DeviceName, Primary, Bounds, BitsPerPixel | Format-List | Out-File $out -Append -Encoding utf8
"DPI awareness / scaling (LogPixels, 96=100%):" | Out-File $out -Append -Encoding utf8
(Get-ItemProperty "HKCU:\Control Panel\Desktop\WindowMetrics" -Name AppliedDPI).AppliedDPI | Out-File $out -Append -Encoding utf8

Section "DWM (Desktop Window Manager) composition"
"Composition enabled: $([bool](Get-Process dwm -ErrorAction SilentlyContinue))" | Out-File $out -Append -Encoding utf8

Section "ScreenRecorder + ffmpeg running processes (CommandLine shows the capture flags)"
Get-CimInstance Win32_Process -Filter "name='ScreenRecorder.exe' or name='ffmpeg.exe'" |
  Select-Object ProcessId, Name, CreationDate, CommandLine | Format-List | Out-File $out -Append -Encoding utf8

Section "Recording config (fps / capture_cursor / segment)"
$cfg = Join-Path $sr "config.yaml"
if (Test-Path $cfg) {
  Get-Content $cfg | Select-String -Pattern "^(recording|input_monitor):|fps|crf|segment_duration|capture_cursor|capture_screenshots" |
    ForEach-Object { $_.Line } | Out-File $out -Append -Encoding utf8
} else { "no config.yaml found at $cfg" | Out-File $out -Append -Encoding utf8 }

Section "Installed exe + version"
$exe = Join-Path $env:LOCALAPPDATA "ScreenRecorder\ScreenRecorder.exe"
if (Test-Path $exe) {
  Get-Item $exe | Select-Object FullName, Length, LastWriteTime | Format-List | Out-File $out -Append -Encoding utf8
} else { "no exe at $exe" | Out-File $out -Append -Encoding utf8 }

Section "Recent app log (last 200 lines)"
$log = Join-Path $sr "screenrecord.log"
if (Test-Path $log) { Get-Content $log -Tail 200 | Out-File $out -Append -Encoding utf8 }
else { "no screenrecord.log found" | Out-File $out -Append -Encoding utf8 }

Section "Recent Windows display/graphics system events (last 24h)"
Get-WinEvent -FilterHashtable @{ LogName='System'; StartTime=(Get-Date).AddDays(-1) } -MaxEvents 4000 2>$null |
  Where-Object { $_.ProviderName -match 'Display|dxgkrnl|nvlddmkm|amdkmdag|igfx|Dwm|TimeoutDetection' } |
  Select-Object TimeCreated, ProviderName, Id, LevelDisplayName -First 40 | Format-Table -AutoSize |
  Out-String -Width 200 | Out-File $out -Append -Encoding utf8

"`nWrote diagnostic to: $out" | Out-File $out -Append -Encoding utf8
Write-Host "Diagnostic written to $out - please send that file back."
