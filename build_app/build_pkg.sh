#!/bin/bash
# Builds ScreenRecorder.pkg — a one-step installer for MDM deployment:
#   /Applications/ScreenRecorder.app  (signed + notarized)
#   /Library/LaunchAgents/ai.bam.screenrecord.plist
#   postinstall provisions ~/.screenrecord (config + creds + key) and starts it.
# The pkg MUST be signed with a Developer ID Installer cert — MDM
# (InstallEnterpriseApplication) rejects unsigned packages (they hang at
# "waiting to install"). Notarize the signed pkg separately after this.
set -euo pipefail
cd "$(dirname "$0")"

BOOT=../bootstrap.sh
APP=dist/ScreenRecorder.app
[ -d "$APP" ] || { echo "Build the app first: ./build_app.sh"; exit 1; }

# Pull the baked deployment values from bootstrap.sh (single source of truth).
val() { grep -m1 "^$1=" "$BOOT" | cut -d'"' -f2; }
GCREDS=$(val GDRIVE_CREDENTIALS_B64)
ENCKEY=$(val ENCRYPTION_KEY_B64)
FOLDER=$(val GDRIVE_FOLDER_ID)
SHEET=$(val GSHEET_ID)
CLIENT=$(val CLIENT_NAME)
[ -n "$GCREDS" ] && [ -n "$SHEET" ] || { echo "Failed to read baked values from bootstrap.sh"; exit 1; }

# Assemble the payload.
rm -rf pkgroot scripts
mkdir -p pkgroot/Applications pkgroot/Library/LaunchAgents \
    "pkgroot/Library/Application Support/ScreenRecorder" scripts
cp -R "$APP" pkgroot/Applications/
cp ai.bam.screenrecord.plist pkgroot/Library/LaunchAgents/
cp launch_wrapper.sh "pkgroot/Library/Application Support/ScreenRecorder/launch_screenrecorder.sh"
chmod 755 "pkgroot/Library/Application Support/ScreenRecorder/launch_screenrecorder.sh"

# Generate postinstall from the template (| delimiter is safe: not in base64).
sed -e "s|__GCREDS__|$GCREDS|" -e "s|__ENCKEY__|$ENCKEY|" \
    -e "s|__FOLDER__|$FOLDER|" -e "s|__SHEET__|$SHEET|" -e "s|__CLIENT__|$CLIENT|" \
    postinstall.template > scripts/postinstall
chmod +x scripts/postinstall

# Build the component pkg, then wrap as a signed distribution pkg (MDM needs it).
pkgbuild --root pkgroot --scripts scripts \
    --identifier ai.bam.screenrecord.pkg --version 1.4.6 \
    --install-location / dist/.component.pkg >/dev/null

INSTALLER_ID=$(security find-identity -v 2>/dev/null \
    | grep "Developer ID Installer" | head -1 | sed -E 's/.*"(.*)"/\1/')
if [ -n "$INSTALLER_ID" ]; then
    echo "==> Signing pkg with: $INSTALLER_ID"
    productbuild --sign "$INSTALLER_ID" --timestamp \
        --package dist/.component.pkg dist/ScreenRecorder.pkg >/dev/null
else
    echo "WARNING: no Developer ID Installer identity found — pkg will be UNSIGNED and MDM will reject it."
    productbuild --package dist/.component.pkg dist/ScreenRecorder.pkg >/dev/null
fi
rm -f dist/.component.pkg

echo "Built: $(pwd)/dist/ScreenRecorder.pkg  ($(du -h dist/ScreenRecorder.pkg | cut -f1))"
pkgutil --check-signature dist/ScreenRecorder.pkg 2>&1 | sed -n '1,3p'
