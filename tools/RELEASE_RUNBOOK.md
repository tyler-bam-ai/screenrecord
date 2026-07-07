# Release Runbook — canary-first, with rollback

**Why this exists:** a bad build published straight to the whole fleet caused the
2026-07-02 outage. Updates now go to a **canary** ring (a few test machines)
first, get verified, then are **promoted** to **stable** (everyone). One command
rolls back.

Deployed machines follow a **channel**:
- **stable** → polls `<platform>-latest` (the whole fleet)
- **canary** → polls `<platform>-canary` (a handful of test machines)

The updaters read this: mac `build_app/update_helper.sh`, Windows
`screenrecord/release_updater.py`.

---

## One-time: mark a few machines as canary

Pick 1–3 low-risk machines (ideally BAM-owned, one Mac + one Windows).

- **Mac** (run as admin, or push via MDM):
  ```bash
  echo canary | sudo tee "/Library/Application Support/ScreenRecorder/update_channel"
  ```
- **Windows**: set `updater: { channel: canary }` in that machine's
  `config.yaml` (or push a config with it).

To move a machine back to stable: write `stable` (mac) / remove the channel key
(Windows). Unmarked machines are stable by default.

---

## Normal release flow

### 1. Build the versioned release (source for publishing)

**Windows** — push a tag; CI builds and publishes the **versioned** `win-v<version>`
release only (it no longer auto-hits `windows-latest`):
```bash
git tag win-v1.0.26 && git push origin win-v1.0.26
# wait for the "windows-build" Action to finish -> release win-v1.0.26 exists
```

**Mac** — build + sign + notarize on this Mac (has the Developer ID
`Tyler Young (A9LNE3KDJ9)`), then create the versioned `mac-v<version>` release:
```bash
export NOTARY_PROFILE=<your-notarytool-keychain-profile>
build_app/build_app.sh                                   # build + codesign .app
build_app/build_pkg.sh                                   # -> build_app/dist/ScreenRecorder.pkg (signed+notarized)
build_app/write_update_manifest.sh build_app/dist/ScreenRecorder.pkg
VER=$(python3 -c 'ns={};exec(open("screenrecord/version.py").read(),ns);print(ns["MAC_UPDATE_VERSION"])')
gh release create "mac-v$VER" build_app/dist/ScreenRecorder.pkg build_app/dist/update-mac.json \
  --title "Screen Recorder for macOS v$VER" --notes "Signed + notarized."
```

### 2. Publish to CANARY
```bash
release/publish.sh windows 1.0.26 canary
release/publish.sh mac     1.0.26 canary
```
This repoints the manifest at the `-canary` release and uploads there. Canary
machines update within the hour (or force now via the dashboard **update_now**
command on those machines).

### 3. VERIFY canary (do not skip)
On the Google Sheets dashboard / machine status, confirm the canary machines:
- report the **new version**,
- are **recording** and **uploading** (segments increasing),
- show **permissions ok** (Mac: the Input Monitoring alert worked),
- no crash / update-loop (status not flapping).

Give it a real soak (a few hours to a day).

### 4. PROMOTE to STABLE (the whole fleet)
```bash
release/publish.sh windows 1.0.26 stable
release/publish.sh mac     1.0.26 stable
```
Fleet updates on next check. Watch the dashboard as versions roll over.

---

## ROLLBACK (emergency)

If a stable release misbehaves, republish the last known-good version to stable
with a forced downgrade:
```bash
release/publish.sh windows 1.0.24 stable --force
release/publish.sh mac     1.0.24 stable --force
```
`--force` sets `force:true` in the manifest; the updaters honor force when the
version differs (verified: mac `update_helper.sh` line ~130, Windows
`release_updater.py` `manifest_force and version_differs`), so machines on the
bad build **downgrade** to the good one. Trigger **update_now** to speed it up.

(Requires the `mac-v1.0.24` / `win-v1.0.24` versioned releases to still exist —
they do; versioned releases are never deleted.)

---

## First-time onboarding of a machine

A machine only self-updates once it's running a build that HAS the updater.
Legacy machines (old builds, the Feb-era Texas Sinus Macs with no updater) need
**one** manual install to get onto the channel; after that they self-update.

- **Windows** (elevated PowerShell, or MDM in user context):
  ```powershell
  irm https://raw.githubusercontent.com/tyler-bam-ai/screenrecord/main/install_windows.ps1 | iex
  ```
- **Mac**: push the current signed `.pkg` via MDM (same app-deployment as the
  original install).

---

## Test the publisher offline (no GitHub, no fleet)

```bash
T=$(mktemp -d)
echo '{"platform":"windows","version":"1.0.26","url":".../download/win-v1.0.26/ScreenRecorder.exe","sha256":"x","force":false}' > "$T/update-windows.json"
: > "$T/ScreenRecorder.exe"
SR_LOCAL="$T" release/publish.sh windows 1.0.26 canary   # writes $T/out-windows-canary/ instead of publishing
```

## Safety invariants

- CI never publishes to `*-latest` — only versioned releases. Fleet promotion is
  always a deliberate `release/publish.sh ... stable`.
- Always canary → verify → promote. Never publish straight to stable.
- Versioned releases are immutable rollback targets — don't delete them.
