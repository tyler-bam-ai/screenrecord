# ScreenRecorder — Handoff

Last updated: 2026-07-09. Written for whoever continues this work on another machine.

This is a fleet-deployed employee screen+input monitoring agent (authorized workplace
monitoring for a medical client). Mac + Windows. It records the screen (ffmpeg), captures
keyboard/mouse/scroll (pynput), encrypts each segment, uploads to Google Drive, and reports
status to a Google Sheet dashboard. It self-updates from GitHub Releases.

---

## 1. Access & credentials you need

- **GitHub**: repo `https://github.com/tyler-bam-ai/screenrecord`. `gh` CLI must be authed as
  `tyler-bam-ai`. All release channels live here.
- **Google service account** (reads dashboard Sheet + Drive, and is baked into installers):
  `~/.screenrecord/credentials.json` on the old machine. It's the `medcenter-487623` service
  account. You need this file to run any of the monitoring/verification below.
- **Envelope private key** (decrypts uploaded recordings for verification):
  `~/.screenrecord/keys/screenrecord_envelope_private_key.pem`. Recordings are encrypted with
  the *public* key; only this private key decrypts them. Keep it off the repo.
- **Master dashboard Sheet id**: `1ujcQshvE7Gu_i_42kwgjQCpfmeID_EyZtpMC35g2bFU`
  (tabs: `Machines`, `Commands`, `Recordings`).
- **Mac notarization**: keychain notary profile named `bam-notary` (Apple ID + app-specific
  password already stored). Set `NOTARY_PROFILE=bam-notary` for Mac builds.
- **Mac signing**: "Developer ID Application" + "Developer ID Installer" certs (Team A9LNE3KDJ9)
  must be in the login keychain.
- **Python env for tooling**: `build_app/venv-u2/bin/python3` has the google-api client libs.

---

## 2. Repo layout

`screenrecord/` — the shared Python agent (runs on both OSes):
- `main.py` — service loop; dashboard commands (stop/start/restart/update_now/record_test);
  permission + health logic.
- `recorder.py` — ffmpeg capture + hourly segment rotation.
- `input_monitor.py` — keyboard/mouse/**scroll** capture. Events → `<segment>.events.jsonl`,
  zipped+encrypted to `<segment>.events.zip.enc`, uploaded with the video. event_type values:
  `mouse_click`, `mouse_scroll`, `key_sequence`. **Mouse *moves* are not captured.**
- `platform_utils.py` — per-OS ffmpeg command. **Windows uses `gdigrab -draw_mouse 1` — the
  suspected flicker source (see §7).**
- `release_updater.py` — **the Windows self-updater** (download→verify→apply-swap PowerShell).
  This is what the branch is all about.
- `macos_permissions.py`, `permission_alert.py` — Mac Accessibility/Screen-Recording prompts.
- `uploader.py`, `heartbeat.py`, `sheets_backend.py`, `encryption.py`, `diagnostics.py`,
  `provision.py`, `config_manager.py`, `version.py`.

Build/deploy:
- Mac: `build_app/build_app.sh` (build+sign+notarize .app), `build_app/build_pkg.sh` (.pkg),
  `build_app/postinstall.template`, `build_app/launch_wrapper.sh`, plists.
- Windows: `install_windows.ps1` (installer + watchdog), built by CI
  `.github/workflows/windows-build.yml` on a `win-v*` tag push.
- Promote a versioned build to a channel: `release/publish.sh <platform> <version> stable`.
- Diagnostic hosted for the client: `tools/flicker_diagnostic.ps1`.

Branch: **`fix/windows-updater-swap`** — NOT merged to `main`. All the recent work is here.

---

## 3. Deployment model (how machines get code)

Machines poll a **rolling channel** manifest on GitHub Releases and self-update:
- Mac fleet → tag `mac-latest` (`update-mac.json` + `ScreenRecorder.pkg`).
- Windows fleet → tag `windows-latest` (`update-windows.json` + `ScreenRecorder.exe`).
- Versioned builds live at `mac-v*` / `win-v*` tags. `release/publish.sh` copies a versioned
  build's assets onto the channel tag (`--clobber`) and rewrites the manifest `url` to the
  channel.
- **GitHub CDN caches the channel manifest** — after `--clobber`, machines can keep seeing the
  OLD manifest for minutes (cache-bust with `?cb=` only affects your own fetch, not theirs).
  This bit us repeatedly; expect propagation lag.
- The updater refuses downgrades (guard in `release_updater.py` / `update_helper.sh`).

---

## 4. CURRENT DEPLOYED STATE (read this before touching anything)

| Thing | Value |
|---|---|
| `mac-latest` channel | **1.4.31.41** — current, good |
| `windows-latest` channel | **1.0.24** — **deliberately rolled back** (incident mitigation, see §6) |
| repo `MAC_VERSION` | 1.4.31 build 41 (matches mac-latest) |
| repo `WINDOWS_VERSION` | **1.0.36** — AHEAD of the channel on purpose |
| latest built Windows tags | `win-v1.0.36` exists (has the hardened updater + watchdog) |

**The repo is intentionally ahead of the Windows channel right now.** `windows-latest` is pinned
back to a fake 1.0.24 manifest so the clinic's old machines see "up to date" and stop looping
(see §6). Do not re-promote a newer Windows build to `windows-latest` until the flicker + a fully
validated updater are ready, or you will restart the pop-up incident.

---

## 5. Mac — DONE ✅

Shipped and self-updating. All confirmed from decrypted Drive data:
- Original bug fixed: the launch wrapper broke TCC attribution so **no permission prompts
  appeared**. Fix: `launch_wrapper.sh` now `exec`s the app (see `memory/mac-tcc-launch-wrapper.md`).
- Correct permission model: keyboard/mouse capture needs **Accessibility**, NOT "Input
  Monitoring" (pynput checks `AXIsProcessTrusted`). One clean prompt. See
  `memory/mac-input-monitoring-prompt.md`.
- Confirmed end-to-end: screen video + keystrokes (with text) + clicks + scroll, encrypted,
  uploaded, decrypted. Remote update proven (1.4.28→…→1.4.31). 60-min segments.
- Fleet snapshot at handoff: 7 Macs live on 1.4.31, most healthy. **Two need attention:**
  `Billing-S3` = user hasn't approved the Screen Recording prompt; `NursesStioniMac` = recording
  but uploads failing 14h+ (network/Drive, investigate). A few machines (`williams-iPhone` etc.,
  odd hostnames) are on OLDER builds not yet on 1.4.31.
- Also shipped in these builds (from a code review): disk-full recovery, empty-config-section
  crash guard, diagnostics secret-scrubbing, downgrade guard, full-removal `uninstall.sh`.

**Mac needs no further action** beyond chasing the two problem machines above.

---

## 6. Windows — IN PROGRESS + a live incident

### What's fixed and validated
- **Scroll capture** (was missing on the fleet's old build) — added and confirmed from decrypted
  data on a real machine (`TYLERYOUNG4035`): video + keys + clicks + **scroll** all upload.
- **Updater DETECTION** — the old fleet build (1.0.24) has a stuck updater that never re-checks.
  The new build detects updates correctly.
- **Updater APPLY hardening** (commit `e2ee4c8` + `6150356`): bounded download (45s/read, 300s
  overall) so a stall can't hang the agent; a **watchdog scheduled task** (`ScreenRecorderWatchdog`,
  installed by `install_windows.ps1`) that relaunches the agent within ~3 min if it stops (Windows
  has no launchd KeepAlive); an `updating.lock` so the watchdog doesn't fight a legitimate swap.
  1.0.35→1.0.36 self-update was validated end-to-end on `TYLERYOUNG4035`.

### THE INCIDENT (2026-07-09, active at handoff)
Client emailed: cursor **flickering still happening**, AND **a window keeps popping up mid-work
and disappearing**, disrupting the team.
- **Root cause of the pop-up = my testing.** Bumping `windows-latest` through 1.0.31→1.0.36 made
  every clinic machine (still on old **1.0.24** with the *broken* updater) see "newer version
  available," launch the update-helper PowerShell window (the pop-up), fail the swap, and retry
  every check interval — a visible window flash on a loop.
- **The flickering is the SEPARATE original issue** (see §7). Not caused or fixed by any of this.

### Mitigation I applied remotely (Windows only — Mac untouched)
1. **Rolled `windows-latest` manifest back to version `1.0.24`** (fake manifest, sha of zeros —
   never fetched because machines see "up to date"). This stops the update-loop pop-ups fleet-wide
   once CDN propagates. THIS IS WHY THE CHANNEL IS "BEHIND" THE REPO.
2. **Queued `stop` (pause) commands** in the Sheet `Commands` tab for all Windows machines
   (`ENT-1271, ENT-1345, ENT-1450, ENT-1507, ENT-D-1360, TYLERYOUNG4035`). Pausing stops ffmpeg →
   stops the flicker temporarily. Reversible with a `start` command.

### Uninstall line (definitive per-machine removal; client/IT runs in PowerShell as the user)
```powershell
schtasks /Delete /TN ScreenRecorderWatchdog /F 2>$null; Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name ScreenRecordAgent -ErrorAction SilentlyContinue; Get-CimInstance Win32_Process -Filter "name='powershell.exe'" | Where-Object { $_.CommandLine -match 'apply_screenrecorder_update' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Get-Process ScreenRecorder,ffmpeg -ErrorAction SilentlyContinue | Stop-Process -Force; Remove-Item -Recurse -Force "$env:LOCALAPPDATA\ScreenRecorder","$env:USERPROFILE\.screenrecord" -ErrorAction SilentlyContinue; "ScreenRecorder fully removed."
```

### Working Windows install line (file-based; `irm | iex` is BROKEN for this script's param block)
```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force; $p="$env:TEMP\sr_install.ps1"; iwr "<installer_url>" -OutFile $p -UseBasicParsing; & $p
```
Add `$env:SR_SEGMENT_SECONDS=300;` before it for a 5-minute test build. For a *pinned* version
whose exe differs from the channel, pass `& $p -ExeUrl "<versioned_exe_url>"`.

---

## 7. Windows flickering — STILL OPEN, highest priority

- Client reports cursor/screen flicker on the clinic Windows machines. They are on **1.0.24**,
  which we never changed — so nothing we shipped caused or fixed it.
- Leading hypothesis: `gdigrab -draw_mouse 1` (in `platform_utils.py` `build_ffmpeg_command`
  Windows path). gdigrab cursor drawing is a known flicker source on some GPU/driver combos.
- **We do NOT yet have the diagnostic data.** `tools/flicker_diagnostic.ps1` is hosted at
  `.../releases/download/windows-latest/flicker_diagnostic.ps1` (RE-UPLOAD it after any channel
  change). Client runs it while flickering; it writes `ScreenRecorder_flicker_diagnostic.txt` to
  the Desktop (GPU/driver, refresh rate, live ffmpeg cmd, logs). Get that file.
- Likely fix: switch Windows capture to GPU-based `ddagrab` (Desktop Duplication), or drop
  `-draw_mouse`. Do NOT ship it fleet-wide until tested on a real machine — capture-method changes
  are exactly what you can't validate blind.

---

## 8. NEXT STEPS (in order)

1. **Confirm the incident is quiet.** Verify each clinic machine's `updater_status` settled and
   recording paused (see §9). If pop-ups persist on any machine, have IT run the uninstall line.
   Decide with the client: fully uninstall the Windows agents for now, or leave them paused.
2. **Get the flicker diagnostic** from a clinic machine and fix the capture method (§7). Build,
   and test on ONE real Windows machine before any rollout.
3. **When shipping Windows again:** the whole fleet is on the broken 1.0.24 updater and CANNOT
   self-update — so getting the fixed build out requires **one manual/RMM push** of
   `install_windows.ps1` per machine (that build has the working updater + watchdog; after it,
   they self-update). Only THEN re-promote to `windows-latest`. Sequence carefully to avoid a
   repeat of §6 (don't leave a newer manifest visible to machines that can't apply it).
4. **Re-validate the hardened updater** once more after the flicker fix (1.0.36 already passed on
   TYLERYOUNG4035, but re-confirm on a clinic-like machine).
5. **Merge `fix/windows-updater-swap` to `main`** once Windows is stable.
6. Mac loose ends: chase `Billing-S3` (approve prompt) and `NursesStioniMac` (upload failure);
   confirm the older-build Macs move to 1.4.31.

---

## 9. How to monitor / verify (run from the old machine's creds)

Use `build_app/venv-u2/bin/python3` with the service-account creds. Core patterns used all session:
- **Dashboard state**: read Sheet `Machines!A:I` (cols: computer_name, employee_name, client_name,
  status, last_heartbeat, segments_uploaded, uptime_hours, installed_at, permissions).
- **Live per-machine detail**: Drive file `heartbeat_<computer>.json` (has `app_version`,
  `recorder_active`, `segment_duration_seconds`, `updater_status`, `current_segment_age_seconds`).
- **Remote command**: append a row to Sheet `Commands!A:E` =
  `[iso_utc_timestamp, computer_name, "stop"|"start"|"restart"|"update_now", "pending", ""]`.
  The agent polls every ~60s. (`stop`=pause, `start`/`restart`=resume/re-exec.)
- **Verify actual capture**: list Drive for `<computer>_*.mp4.enc` (video) and
  `<computer>_*.events.zip.enc` (input log), download, decrypt with
  `screenrecord.encryption.FileEncryptor.load_private_key(<priv_key>).decrypt_file(...)`, then
  ffprobe the mp4 and read the `.events.jsonl` inside the zip (count `mouse_click`/`mouse_scroll`/
  `key_sequence`). This is how "is it really capturing scroll" was proven.
- Ad-hoc monitor scripts were written to the session scratchpad (ephemeral, not in the repo) —
  rebuild from the patterns above.

---

## 10. Known bugs still open (from the code review, not yet fixed)

Lower priority than the above, but logged:
- **Windows self-update trusts an unsigned manifest's SHA only** — no Authenticode check on the
  downloaded exe. RCE risk if the manifest/release is tampered. The Windows exe isn't Authenticode-
  signed at all, so fixing this needs a code-signing cert first.
- Diagnostics zip written world-readable to `/Users/Shared` + Desktop and left on upload failure.
- Credential/key files briefly world-readable during provisioning before chmod.
- Rotated audit logs not permission-hardened.
- Uploader retries non-retryable 4xx and can create duplicate Drive files on post-commit errors.
- Sheets row-index cache can clobber a neighboring machine's row if a row is deleted within the
  10-min TTL.
- `record_test` dashboard command spins a 2nd recorder that contends with the main one on macOS
  (produces no clip) — don't rely on it on Mac.

See the memory files under
`~/.claude/projects/-Users-tyleryoung-Code-screenrecord/memory/` for the Mac permission saga
details (`mac-tcc-launch-wrapper.md`, `mac-input-monitoring-prompt.md`) and deployment notes.
