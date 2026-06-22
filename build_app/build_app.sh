#!/bin/bash
# Build, sign, (optionally) notarize ScreenRecorder.app, and emit the code
# requirement the client's MDM needs for the Screen Recording PPPC profile.
#
# Prereqs:
#   - Developer ID Application certificate in the login keychain
#     (Xcode > Settings > Accounts > Manage Certificates > + Developer ID Application)
#   - To notarize: store credentials once, then export NOTARY_PROFILE:
#       xcrun notarytool store-credentials bam-notary \
#         --apple-id you@bam.ai --team-id <TEAMID> --password <app-specific-pw>
#       export NOTARY_PROFILE=bam-notary
#
# Usage: ./build_app.sh            # build + sign (+ notarize if NOTARY_PROFILE set)
set -euo pipefail
cd "$(dirname "$0")"

APP="dist/ScreenRecorder.app"
BUNDLE_ID="ai.bam.screenrecord"
ENTITLEMENTS="entitlements.plist"

# 1) Find the Developer ID Application signing identity.
IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null \
    | grep "Developer ID Application" | head -1 | awk -F'"' '{print $2}') || true
if [ -z "$IDENTITY" ]; then
    echo "ERROR: No 'Developer ID Application' certificate found in your keychain."
    echo "Add it via Xcode > Settings > Accounts > Manage Certificates > + Developer ID Application."
    exit 1
fi
TEAM_ID=$(echo "$IDENTITY" | sed -n 's/.*(\([A-Z0-9]\{10\}\)).*/\1/p')
echo "Signing identity: $IDENTITY  (Team $TEAM_ID)"

# 2) Bake deployment values into the app so the agent self-provisions its config
#    on first login — covers MDM installs where the postinstall's console-user
#    detection fails and no config gets written. Single source of truth: bootstrap.sh.
echo "==> Baking provisioning values from bootstrap.sh..."
val() { grep -m1 "^$1=" ../bootstrap.sh | cut -d'"' -f2; }
python3 - "$(val GDRIVE_CREDENTIALS_B64)" "$(val ENCRYPTION_KEY_B64)" \
         "$(val GDRIVE_FOLDER_ID)" "$(val GSHEET_ID)" "$(val CLIENT_NAME)" <<'PY'
import json, sys
g, e, f, s, c = sys.argv[1:6]
assert g and s, "missing baked values from bootstrap.sh"
json.dump({"gcreds_b64": g, "enckey_b64": e, "folder": f, "sheet": s, "client": c},
          open("_provision.json", "w"))
PY
[ -s _provision.json ] || { echo "ERROR: failed to bake _provision.json"; exit 1; }
# Cleanup on exit — _provision.json holds secrets; once bundled it's not needed.
trap 'rm -f _provision.json' EXIT

# 3) Build the bundle.
echo "==> Building app..."
rm -rf build dist
./venv-u2/bin/pyinstaller --noconfirm --distpath dist --workpath build screenrecorder.spec >/dev/null
[ -d "$APP" ] || { echo "ERROR: build produced no app"; exit 1; }

# 3) Sign EVERY nested Mach-O binary (detected by content, not extension — this
#    catches Python.framework/.../Python and any other extensionless binaries),
#    inner-first, always with a secure timestamp (notarization requires it).
echo "==> Signing all nested Mach-O binaries..."
while IFS= read -r -d '' f; do
    if file -b "$f" | grep -q "Mach-O"; then
        codesign --force --options runtime --timestamp --sign "$IDENTITY" "$f"
    fi
done < <(find "$APP/Contents" -type f -print0)

echo "==> Signing the app bundle..."
codesign --force --options runtime --timestamp \
         --entitlements "$ENTITLEMENTS" \
         --sign "$IDENTITY" "$APP"

echo "==> Verifying signature..."
codesign --verify --deep --strict --verbose=2 "$APP"

# 4) Notarize + staple (only if a notary profile is configured).
if [ -n "${NOTARY_PROFILE:-}" ]; then
    echo "==> Notarizing (profile: $NOTARY_PROFILE)..."
    ZIP="dist/ScreenRecorder.zip"
    ditto -c -k --keepParent "$APP" "$ZIP"
    xcrun notarytool submit "$ZIP" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$APP"
    echo "==> Notarized + stapled."
else
    echo "==> Skipping notarization (set NOTARY_PROFILE to enable)."
fi

# 5) Package for distribution + emit the MDM code requirement.
ditto -c -k --keepParent "$APP" "dist/ScreenRecorder.zip"
echo ""
echo "================ DELIVERABLES FOR THE MDM (give these to Jake) ================"
echo "Bundle identifier : $BUNDLE_ID"
echo "Team ID           : $TEAM_ID"
echo -n "Code requirement  : "
codesign -d -r- "$APP" 2>&1 | sed -n 's/^designated => //p'
echo "Signed app (zip)  : $(pwd)/dist/ScreenRecorder.zip"
echo "==============================================================================="
