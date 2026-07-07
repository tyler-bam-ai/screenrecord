# REAL end-to-end Windows self-update test — runs on an actual Windows machine
# (GitHub windows-latest runner). Proves the fix against real Windows exe-locking.
#
#   1. Build two REAL console exes with the in-box C# compiler:
#        v1 prints "1.0.21" then sleeps (stays running -> Windows LOCKS its file);
#        v2 prints "1.0.25" and exits.
#   2. Install v1 as ScreenRecorder.exe and launch it (file now OS-locked).
#   3. Show the OLD overwrite approach FAILS with a real sharing violation.
#   4. Run the REAL generated apply script (rendered from ReleaseUpdater) -> it
#      stops v1, renames it aside, copies v2 in, restarts.
#   5. Assert ScreenRecorder.exe now reports "1.0.25" and status == "updated".
# Exits non-zero on any failed assertion (fails the CI job).

param([string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path)

$ErrorActionPreference = "Stop"
$fail = 0
function Check($name, $cond) {
  if ($cond) { Write-Host "PASS: $name" } else { Write-Host "FAIL: $name"; $script:fail++ }
}

$tmp = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }
$work = Join-Path $tmp ("e2e_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $work | Out-Null

function Build-Exe($outPath, $version, [bool]$sleep) {
  $sleepLine = if ($sleep) { "System.Threading.Thread.Sleep(600000);" } else { "" }
  $src = "public class P { public static void Main() { System.Console.WriteLine(""$version""); $sleepLine } }"
  Add-Type -TypeDefinition $src -OutputAssembly $outPath -OutputType ConsoleApplication
}

Write-Host "== building real console exes =="
$v1 = Join-Path $work "app_v1.exe"
$v2 = Join-Path $work "ScreenRecorder-1.0.25.exe"
Build-Exe $v1 "1.0.21" $true
Build-Exe $v2 "1.0.25" $false

# --- install v1 as ScreenRecorder.exe and launch it (locks the file) ---------
$target = Join-Path $work "ScreenRecorder.exe"
Copy-Item -LiteralPath $v1 -Destination $target -Force
$beforeOut = Join-Path $work "before.txt"
$proc = Start-Process -FilePath $target -PassThru -RedirectStandardOutput $beforeOut -WindowStyle Hidden
Start-Sleep -Seconds 2
$before = (Get-Content -Raw -LiteralPath $beforeOut).Trim()
Write-Host "installed & running version: $before (pid $($proc.Id))"
Check "machine starts on OLD version 1.0.21" ($before -eq "1.0.21")
Check "running exe is OS-locked" (-not ($proc.HasExited))

# --- OLD approach: overwrite a running/locked exe fails (the production bug) --
$oldFailed = $false
try { Copy-Item -LiteralPath $v2 -Destination $target -Force }
catch { $oldFailed = $true; Write-Host ("  overwrite threw (expected): " + $_.Exception.Message.Split([Environment]::NewLine)[0]) }
Check "OLD overwrite fails on real Windows lock (root cause reproduced)" $oldFailed

# --- render the REAL apply script from the shipped ReleaseUpdater code --------
$apply = Join-Path $work "apply.ps1"
$log   = Join-Path $work "windows_updater.log"
$status= Join-Path (Split-Path -Parent $log) "updater_status.json"
python (Join-Path $RepoRoot "tests/render_apply_script.py") $v2 "1.0.25" $apply
Check "real apply script rendered" (Test-Path -LiteralPath $apply)

# --- run the REAL apply script: it swaps the locked exe and restarts ---------
Write-Host "== running real generated apply script =="
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $apply -TargetPid $proc.Id -TargetExe $target -LogPath $log
Start-Sleep -Seconds 3

# --- verify the swap + version flip on real Windows --------------------------
$after = (& $target).Trim()   # v2 prints and exits immediately
Write-Host "version after update: $after"
Check "ScreenRecorder.exe now reports NEW version 1.0.25" ($after -eq "1.0.25")
if (Test-Path -LiteralPath $status) {
  $st = (Get-Content -Raw -LiteralPath $status | ConvertFrom-Json)
  Write-Host "updater_status: $($st.status) / $($st.version)"
  Check "updater_status.json status == updated" ($st.status -eq "updated")
} else { Check "updater_status.json written" $false }
Write-Host "== helper log =="; if (Test-Path $log) { Get-Content $log }

Get-Process -Name ScreenRecorder -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
Write-Host ""
if ($fail -eq 0) { Write-Host "ALL CHECKS PASSED (real Windows)"; exit 0 }
else { Write-Host "$fail CHECK(S) FAILED"; exit 1 }
