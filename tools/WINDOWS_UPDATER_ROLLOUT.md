# Windows updater fix — rollout & beta-gate

Branch `fix/windows-updater-swap` (commit f0d62d8) fixes the hourly self-update
loop (rename-then-replace exe swap + hardening). Verified locally with pwsh; the
one thing a Mac can't prove is the Windows-native restart, which the beta VM does.

## The one unavoidable manual install (and why it's the LAST one)

The broken applier is baked into the **1.0.21** exe already running on the
clinics. Code already deployed can only be replaced via the updater — which is
the thing that's broken. So the jump **1.0.21 → 1.0.25 must be a manual install**
on each Windows machine (a fresh install bypasses the broken in-place swap).

The whole point of the fix + beta gate is to prove that **1.0.25 → every future
version self-updates cleanly**, so this is the last manual install anyone ever
does.

## Beta gate (must pass before any clinic machine)

Beta box: `VM-WNC-LAB1` or `TYLERYOUNG4035` (both in-fleet Windows).

1. Merge the branch; push tag `win-v1.0.25` → `windows-build.yml` builds and
   publishes 1.0.25 to the `windows-latest` release.
2. **Manually install 1.0.25** on the beta box. Confirm empirically:
   - records **video** (fresh `.mp4.enc` in Drive),
   - records **keystrokes + mouse clicks** (fresh `.events.zip.enc` whose
     decrypted `events.jsonl` has non-zero `mouse_click` / `key_sequence`),
   - `updater_status.json` shows `local_version: 1.0.25`, status `up_to_date`
     (no re-staging loop).
3. **The decisive test — prove self-update works from 1.0.25:** publish a
   trivial `win-v1.0.26`. On the beta box (running 1.0.25, i.e. the FIXED
   applier), confirm within ~1h:
   - `windows_updater.log` shows the helper ran and `status: updated`,
   - `local_version` → `1.0.26`,
   - recording continues (no gap, no loop),
   - the machine is NOT still on 1.0.25.
   This is the empirical proof the loop is gone for good.

Only after step 3 passes do clinic machines get touched.

## Clinic rollout (after beta passes)

- **ENT (Wisconsin):** first fix the firewall (allow `github.com` +
  `*.githubusercontent.com` — their SSL-handshake timeout blocks downloads),
  then one manual install of 1.0.25 per machine. After that they self-update.
- **KEN-ORBETA (S&SS Windows):** one manual install of 1.0.25.
- Frame to IT: "one final install — we've verified on our test machine that it
  self-updates cleanly from here on, so this is the last time."

## What was fixed (for the record)

Root cause: onefile exe holds a file lock → `Copy-Item -Force` overwrite fails →
helper relaunches stale exe → hourly loop. Adversarial review additionally
caught and fixed: manifest `force` re-loop, pre-release version-string skew,
restore-skips-partial brick risk, `$`-in-path expansion, stale-`.old` collision,
and a mutually-exclusive process flag. All re-verified with pwsh.
