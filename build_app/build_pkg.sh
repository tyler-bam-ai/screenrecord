#!/bin/bash
# Builds ScreenRecorder.pkg — a one-step installer for MDM deployment:
#   /Applications/ScreenRecorder.app  (signed + notarized)
#   /Library/LaunchAgents/ai.bam.screenrecord.plist
#   /Library/LaunchDaemons/ai.bam.screenrecord.updater.plist
#   postinstall provisions ~/.screenrecord (config + creds + public key) and starts it.
# The pkg MUST be signed with a Developer ID Installer cert — MDM
# (InstallEnterpriseApplication) rejects unsigned packages (they hang at
# "waiting to install"). Notarize the signed pkg separately after this.
set -euo pipefail
cd "$(dirname "$0")"

BOOT=../bootstrap.sh
APP=dist/ScreenRecorder.app
[ -d "$APP" ] || { echo "Build the app first: ./build_app.sh"; exit 1; }
MAC_VERSION=$(python3 - <<'PY'
from pathlib import Path
ns = {}
exec(Path("../screenrecord/version.py").read_text(), ns)
print(ns["MAC_UPDATE_VERSION"])
PY
)

# Pull the baked deployment values from bootstrap.sh (single source of truth).
val() {
    eval "ENV_VAL=\${$1:-}"
    if [ -n "${ENV_VAL:-}" ]; then
        printf '%s' "$ENV_VAL"
    else
        (grep -m1 "^$1=" "$BOOT" || true) | cut -d'"' -f2
    fi
}
GCREDS=$(val GDRIVE_CREDENTIALS_B64)
ENCKEY=$(val ENCRYPTION_KEY_B64)
ENCPUB=$(val ENCRYPTION_PUBLIC_KEY_B64)
FOLDER=$(val GDRIVE_FOLDER_ID)
UPLOAD_FOLDER=$(val GDRIVE_UPLOAD_FOLDER_ID)
HEARTBEAT_FOLDER=$(val GDRIVE_HEARTBEAT_FOLDER_ID)
DIAGNOSTICS_FOLDER=$(val GDRIVE_DIAGNOSTICS_FOLDER_ID)
SHEET=$(val GSHEET_ID)
CLIENT=$(val CLIENT_NAME)
[ -n "$GCREDS" ] && [ -n "$SHEET" ] || { echo "Failed to read baked values from bootstrap.sh"; exit 1; }

# Assemble the payload.
rm -rf pkgroot scripts
mkdir -p pkgroot/Applications pkgroot/Library/LaunchAgents \
    pkgroot/Library/LaunchDaemons \
    "pkgroot/Library/Application Support/ScreenRecorder" scripts
cp -R "$APP" pkgroot/Applications/
cp ai.bam.screenrecord.plist pkgroot/Library/LaunchAgents/
cp ai.bam.screenrecord.updater.plist pkgroot/Library/LaunchDaemons/
cp launch_wrapper.sh "pkgroot/Library/Application Support/ScreenRecorder/launch_screenrecorder.sh"
cp update_helper.sh "pkgroot/Library/Application Support/ScreenRecorder/update_helper.sh"
chmod 644 pkgroot/Library/LaunchAgents/ai.bam.screenrecord.plist
chmod 644 pkgroot/Library/LaunchDaemons/ai.bam.screenrecord.updater.plist
chmod 755 "pkgroot/Library/Application Support/ScreenRecorder/launch_screenrecorder.sh"
chmod 755 "pkgroot/Library/Application Support/ScreenRecorder/update_helper.sh"

# Generate postinstall from the template (| delimiter is safe: not in base64).
sed -e "s|__GCREDS__|$GCREDS|" -e "s|__ENCKEY__|$ENCKEY|" -e "s|__ENCPUB__|$ENCPUB|" \
    -e "s|__FOLDER__|$FOLDER|" -e "s|__UPLOAD_FOLDER__|$UPLOAD_FOLDER|" \
    -e "s|__HEARTBEAT_FOLDER__|$HEARTBEAT_FOLDER|" \
    -e "s|__DIAGNOSTICS_FOLDER__|$DIAGNOSTICS_FOLDER|" \
    -e "s|__SHEET__|$SHEET|" -e "s|__CLIENT__|$CLIENT|" \
    postinstall.template > scripts/postinstall
cp preinstall.template scripts/preinstall
chmod +x scripts/postinstall
chmod +x scripts/preinstall

# Build the component pkg, then wrap as a signed distribution pkg (MDM needs it).
pkgbuild --root pkgroot --scripts scripts \
    --identifier ai.bam.screenrecord.pkg --version "$MAC_VERSION" \
    --install-location / --ownership recommended dist/.component.pkg >/dev/null

INSTALLER_ID=$(security find-identity -v 2>/dev/null \
    | grep "Developer ID Installer" | head -1 | sed -E 's/.*"(.*)"/\1/')
if [ -n "$INSTALLER_ID" ]; then
    echo "==> Signing pkg with: $INSTALLER_ID"
    productbuild --sign "$INSTALLER_ID" --timestamp \
        --package dist/.component.pkg dist/ScreenRecorder.pkg >/dev/null
else
    echo "ERROR: no Developer ID Installer identity found — refusing to build an updater manifest for an unsigned pkg."
    exit 1
fi
rm -f dist/.component.pkg

if [ -n "${NOTARY_PROFILE:-}" ]; then
    echo "==> Notarizing pkg (profile: $NOTARY_PROFILE)..."
    xcrun notarytool submit dist/ScreenRecorder.pkg --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple dist/ScreenRecorder.pkg
    echo "==> Notarized + stapled pkg."
else
    echo "==> Skipping pkg notarization (set NOTARY_PROFILE to enable)."
fi

spctl --assess --type install dist/ScreenRecorder.pkg

echo "Built: $(pwd)/dist/ScreenRecorder.pkg  ($(du -h dist/ScreenRecorder.pkg | cut -f1))"
pkgutil --check-signature dist/ScreenRecorder.pkg 2>&1 | sed -n '1,3p'
./write_update_manifest.sh dist/ScreenRecorder.pkg
