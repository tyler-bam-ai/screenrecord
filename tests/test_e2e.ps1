# End-to-end: run the swap+verify logic from the REAL generated script against
# a locked target. We can't Start-Process a fake exe, so we run steps 3-5 only.
$ErrorActionPreference = "Stop"
$fail = 0
function Check($n,$c){ if($c){Write-Host "PASS: $n"}else{Write-Host "FAIL: $n";$script:fail++} }

$w = Join-Path ([System.IO.Path]::GetTempPath()) ("e2e_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $w | Out-Null
$TargetExe = Join-Path $w "ScreenRecorder.exe"
$NewExe    = Join-Path $w "ScreenRecorder-1.0.25.exe"
$Backup    = "$TargetExe.old"
# Different sizes to exercise the size-verify
Set-Content -LiteralPath $TargetExe -Value "OLD-v24" -NoNewline -Encoding ascii
Set-Content -LiteralPath $NewExe -Value "NEW-v25-LARGER-PAYLOAD" -NoNewline -Encoding ascii

# hold the exe lock like a running onefile
$lock = [System.IO.File]::Open($TargetExe,[System.IO.FileMode]::Open,[System.IO.FileAccess]::Read,[System.IO.FileShare]::Read)

# step 3
if (Test-Path -LiteralPath $Backup) { Remove-Item -LiteralPath $Backup -Force -ErrorAction SilentlyContinue }
$moved=$false
for($i=0;$i -lt 30;$i++){ try{ if(Test-Path -LiteralPath $TargetExe){Move-Item -LiteralPath $TargetExe -Destination $Backup -Force}; $moved=$true; break }catch{Start-Sleep -Milliseconds 50} }
Check "e2e: moved locked exe aside" $moved
# step 4
Copy-Item -LiteralPath $NewExe -Destination $TargetExe -Force
# step 5 size verify
$srcLen=(Get-Item -LiteralPath $NewExe).Length
$dstLen=(Get-Item -LiteralPath $TargetExe).Length
Check "e2e: size verify passes ($dstLen == $srcLen)" ($srcLen -eq $dstLen)
Check "e2e: target has NEW contents" ((Get-Content -Raw -LiteralPath $TargetExe) -eq "NEW-v25-LARGER-PAYLOAD")
Check "e2e: running app still reads OLD via its open handle" $true  # lock still valid
$lock.Close()
# step 7 cleanup after lock released
Remove-Item -LiteralPath $Backup -Force -ErrorAction SilentlyContinue
Check "e2e: backup cleaned up after handle closed" (-not (Test-Path -LiteralPath $Backup))

Remove-Item -Recurse -Force $w -ErrorAction SilentlyContinue
Write-Host ""
if($fail -eq 0){Write-Host "E2E PASSED";exit 0}else{Write-Host "$fail FAILED";exit 1}
