#!/usr/bin/env bash
# ============================================================================
# Unified release-channel publisher — the ONE tool for canary / promote / rollback.
#
#   canary:    release/publish.sh windows 1.0.26 canary
#   promote:   release/publish.sh windows 1.0.26 stable          # after canary is verified healthy
#   rollback:  release/publish.sh windows 1.0.24 stable --force  # emergency downgrade to a known-good build
#
# It takes an already-built VERSIONED release (win-v<version> / mac-v<version>),
# repoints its update manifest url at the destination channel's release, and
# publishes to that channel tag. Machines follow their channel (stable = the
# whole fleet; canary = a handful of test machines) and update on next check.
#
# Channel -> release tag:  stable -> <platform>-latest,  canary -> <platform>-canary
# (matches what the updaters poll: build_app/update_helper.sh + release_updater.py)
#
# OFFLINE TEST / DRY-RUN: set SR_LOCAL=<dir> containing the <asset> + <manifest>;
# output is written to $SR_LOCAL/out-<tag>/ and nothing touches GitHub.
# ============================================================================
set -euo pipefail
REPO="tyler-bam-ai/screenrecord"
PLATFORM="${1:?platform required: mac or windows (usage: publish.sh PLATFORM VERSION CHANNEL [--force])}"
VERSION="${2:?version required, e.g. 1.0.26}"
CHANNEL="${3:?channel required: canary or stable}"
FORCE="false"; [ "${4:-}" = "--force" ] && FORCE="true"

# EXTRA = additional assets to carry to the channel release (e.g. the Windows
# first-install script, so promoting a new exe also refreshes install_windows.ps1
# and fresh installs never get a frozen installer).
case "$PLATFORM" in
  mac)     MANIFEST="update-mac.json";     ASSET="ScreenRecorder.pkg"; VTAG="mac-v$VERSION"; EXTRA="" ;;
  windows) MANIFEST="update-windows.json"; ASSET="ScreenRecorder.exe"; VTAG="win-v$VERSION"; EXTRA="install_windows.ps1" ;;
  *) echo "platform must be mac|windows" >&2; exit 2 ;;
esac
case "$CHANNEL" in
  canary) DTAG="$PLATFORM-canary" ;;
  stable) DTAG="$PLATFORM-latest" ;;
  *) echo "channel must be canary|stable" >&2; exit 2 ;;
esac

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT

# --- fetch the versioned source assets (from GitHub, or SR_LOCAL for testing) --
if [ -n "${SR_LOCAL:-}" ]; then
  cp "$SR_LOCAL/$MANIFEST" "$work/" 2>/dev/null || { echo "SR_LOCAL missing $MANIFEST" >&2; exit 1; }
  cp "$SR_LOCAL/$ASSET" "$work/" 2>/dev/null || : # asset optional in local test
  [ -n "$EXTRA" ] && cp "$SR_LOCAL/$EXTRA" "$work/" 2>/dev/null || :
else
  DL_EXTRA=""; [ -n "$EXTRA" ] && DL_EXTRA="-p $EXTRA"
  gh release download "$VTAG" -R "$REPO" -p "$MANIFEST" -p "$ASSET" $DL_EXTRA -D "$work" --clobber
fi

# --- rewrite manifest: point url at the DEST channel; set force on rollback ---
python3 - "$work/$MANIFEST" "$DTAG" "$FORCE" "$VERSION" <<'PY'
import json, re, sys
path, dtag, force, version = sys.argv[1:5]
m = json.load(open(path))
m["url"] = re.sub(r"/download/[^/]+/", "/download/%s/" % dtag, m["url"])
if force == "true":
    m["force"] = True
if str(m.get("version")) != str(version):
    print("WARN manifest version %s != requested %s" % (m.get("version"), version), file=sys.stderr)
json.dump(m, open(path, "w"), indent=2); open(path, "a").write("\n")
print("  channel=%s version=%s force=%s\n  url=%s" % (dtag, m.get("version"), m.get("force"), m["url"]))
PY

# --- publish (or write locally in test mode) ---------------------------------
if [ -n "${SR_LOCAL:-}" ]; then
  out="$SR_LOCAL/out-$DTAG"; mkdir -p "$out"; cp "$work/$MANIFEST" "$out/"
  [ -f "$work/$ASSET" ] && cp "$work/$ASSET" "$out/" || true
  [ -n "$EXTRA" ] && [ -f "$work/$EXTRA" ] && cp "$work/$EXTRA" "$out/" || true
  echo "[local] wrote $out/$MANIFEST"; exit 0
fi
gh release view "$DTAG" -R "$REPO" >/dev/null 2>&1 || \
  gh release create "$DTAG" -R "$REPO" --title "$DTAG" --notes "Channel link ($CHANNEL)."
UP_EXTRA=""; [ -n "$EXTRA" ] && [ -f "$work/$EXTRA" ] && UP_EXTRA="$work/$EXTRA"
gh release upload "$DTAG" -R "$REPO" "$work/$ASSET" "$work/$MANIFEST" $UP_EXTRA --clobber
echo "Published $VERSION -> $DTAG (force=$FORCE)."
echo "Machines on '$CHANNEL' update on next check; use the dashboard 'update_now' command to force immediately."
