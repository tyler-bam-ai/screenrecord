# Empirical test of the Windows updater swap fix.
# Reproduces the exact failure: a "running" onefile exe holds a write-deny lock
# on ScreenRecorder.exe. Proves the OLD approach (Copy-Item -Force overwrite)
# fails with a sharing violation, while the NEW approach (rename-then-replace)
# succeeds and the target ends up with the NEW contents.
#
# .NET honors FileShare cross-platform, so this is a faithful reproduction of
# the Windows exe-lock behavior even when run under pwsh on macOS/Linux.

$ErrorActionPreference = "Stop"
$fail = 0
function Check($name, $cond) {
  if ($cond) { Write-Host "PASS: $name" }
  else { Write-Host "FAIL: $name"; $script:fail++ }
}

$work = Join-Path ([System.IO.Path]::GetTempPath()) ("swaptest_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $work | Out-Null
$target = Join-Path $work "ScreenRecorder.exe"
$new    = Join-Path $work "ScreenRecorder-1.0.24.exe"
Set-Content -LiteralPath $target -Value "OLD-EXE-v1.0.21" -NoNewline -Encoding ascii
Set-Content -LiteralPath $new    -Value "NEW-EXE-v1.0.24-BIGGER" -NoNewline -Encoding ascii

# Hold the target open with FileShare.Read => other writers are DENIED (the exe lock).
$lock = [System.IO.File]::Open($target, [System.IO.FileMode]::Open,
                               [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)

Write-Host "`n--- OLD approach: Copy-Item -Force over a locked target ---"
$oldFailed = $false
try { Copy-Item -LiteralPath $new -Destination $target -Force }
catch { $oldFailed = $true; Write-Host ("  Copy-Item threw (expected): " + $_.Exception.Message.Split([Environment]::NewLine)[0]) }
Check "OLD overwrite fails while exe is locked (root cause)" $oldFailed
Check "OLD leaves target unchanged (still old contents)" ((Get-Content -Raw -LiteralPath $target) -eq "OLD-EXE-v1.0.21")

Write-Host "`n--- NEW approach: rename-then-replace while target is locked ---"
$backup = "$target.old"
$newOk = $true
try {
  Move-Item -LiteralPath $target -Destination $backup -Force   # rename the locked exe aside
  Copy-Item -LiteralPath $new -Destination $target -Force      # copy new into freed path
} catch { $newOk = $false; Write-Host ("  NEW threw (unexpected): " + $_.Exception.Message) }
Check "NEW swap succeeds while exe is still locked" $newOk
Check "NEW target now has NEW contents" ((Get-Content -Raw -LiteralPath $target) -eq "NEW-EXE-v1.0.24-BIGGER")
Check "NEW backup preserved old exe (restore path exists)" (Test-Path -LiteralPath $backup)

# The still-open lock handle keeps reading the ORIGINAL bytes (now in the backup),
# proving the running process is undisturbed by the swap.
$buf = New-Object byte[] 15
$lock.Position = 0; [void]$lock.Read($buf, 0, 15)
Check "locked handle still reads original bytes (running app undisturbed)" ([System.Text.Encoding]::ASCII.GetString($buf) -eq "OLD-EXE-v1.0.21")
$lock.Close()

Remove-Item -Recurse -Force -Path $work -ErrorAction SilentlyContinue
Write-Host ""
if ($fail -eq 0) { Write-Host "ALL CHECKS PASSED"; exit 0 }
else { Write-Host "$fail CHECK(S) FAILED"; exit 1 }
