#!/bin/bash
# Writes the macOS update manifest for the final ScreenRecorder.pkg bytes.
set -euo pipefail
cd "$(dirname "$0")"

PKG="${1:-dist/ScreenRecorder.pkg}"
[ -f "$PKG" ] || { echo "Package not found: $PKG" >&2; exit 1; }

MAC_VERSION=$(python3 - <<'PY'
from pathlib import Path
ns = {}
exec(Path("../screenrecord/version.py").read_text(), ns)
print(ns["MAC_UPDATE_VERSION"])
PY
)
PKG_SHA=$(shasum -a 256 "$PKG" | awk '{print $1}')
TEAM_ID=$(pkgutil --check-signature "$PKG" 2>/dev/null | sed -n 's/.*(\([A-Z0-9]\{10\}\)).*/\1/p' | head -1)

python3 - "$MAC_VERSION" "$PKG_SHA" "$TEAM_ID" <<'PY'
import json
import sys

version, sha, team = sys.argv[1:4]
payload = {
    "platform": "mac",
    "version": version,
    "url": "https://github.com/tyler-bam-ai/screenrecord/releases/download/mac-latest/ScreenRecorder.pkg",
    "sha256": sha,
    "team_id": team,
    "force": False,
}
with open("dist/update-mac.json", "w", encoding="utf-8") as fh:
    json.dump(payload, fh, indent=2)
    fh.write("\n")
PY

echo "Manifest: $(pwd)/dist/update-mac.json"
