#!/bin/bash
# Root LaunchDaemon helper for silent macOS package updates.
set -u

LABEL="ai.bam.screenrecord.updater"
EXPECTED_TEAM_ID="A9LNE3KDJ9"
EXPECTED_BUNDLE_ID="ai.bam.screenrecord"
MANIFEST_URL="${SCREENRECORDER_MAC_MANIFEST_URL:-https://github.com/tyler-bam-ai/screenrecord/releases/download/mac-latest/update-mac.json}"
APP="/Applications/ScreenRecorder.app"
PKG_ID="ai.bam.screenrecord.pkg"
WORK_DIR="/Library/Application Support/ScreenRecorder"
UPDATE_DIR="$WORK_DIR/updates"
LOG_DIR="/Library/Logs/ScreenRecorder"
LOG="$LOG_DIR/updater.log"
SHARED_DIR="/Users/Shared/ScreenRecorder"
SHARED_LOG="$SHARED_DIR/ScreenRecorder_updater.log"
STATUS="$WORK_DIR/updater_status.json"
SHARED_STATUS="$SHARED_DIR/ScreenRecorder_updater_status.json"
TRIGGER="/Users/Shared/ScreenRecorder_update_now"
LOCK_DIR="/var/run/$LABEL.lock"

prepare_shared_dir() {
    mkdir -p /Users/Shared 2>/dev/null || true
    if [ -L "$SHARED_DIR" ] || { [ -e "$SHARED_DIR" ] && [ ! -d "$SHARED_DIR" ]; }; then
        SHARED_DIR="/var/tmp"
        SHARED_LOG="$SHARED_DIR/ScreenRecorder_updater.log"
        SHARED_STATUS="$SHARED_DIR/ScreenRecorder_updater_status.json"
        return
    fi
    mkdir -p "$SHARED_DIR" 2>/dev/null || true
    chown root:wheel "$SHARED_DIR" 2>/dev/null || true
    chmod 755 "$SHARED_DIR" 2>/dev/null || true
}

prepare_shared_dir
mkdir -p "$UPDATE_DIR" "$LOG_DIR" 2>/dev/null || true
touch "$LOG" "$SHARED_LOG" 2>/dev/null || true
chmod 644 "$LOG" "$SHARED_LOG" 2>/dev/null || true

log() {
    MSG="$(date -u '+%Y-%m-%dT%H:%M:%SZ') $*"
    echo "$MSG" >> "$LOG"
    echo "$MSG" >> "$SHARED_LOG"
}

json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

write_status() {
    ST="$1"; MSG="$2"; REMOTE="${3:-}"; VERSION="${4:-}"
    NOW="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    cat > "$STATUS" <<JSON
{"status":"$(json_escape "$ST")","message":"$(json_escape "$MSG")","platform":"mac","local_version":"$(json_escape "$VERSION")","remote_version":"$(json_escape "$REMOTE")","last_checked":"$NOW","manifest_url":"$(json_escape "$MANIFEST_URL")"}
JSON
    cp "$STATUS" "$SHARED_STATUS" 2>/dev/null || true
    chmod 644 "$STATUS" "$SHARED_STATUS" 2>/dev/null || true
}

cleanup() {
    rm -rf "$LOCK_DIR" 2>/dev/null || true
}
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "Another updater run is already active."
    exit 0
fi
trap cleanup EXIT

json_get() {
    KEY="$1"; FILE="$2"
    VALUE="$(/usr/bin/plutil -extract "$KEY" raw -o - "$FILE" 2>/dev/null || true)"
    if [ -n "$VALUE" ]; then
        printf '%s' "$VALUE"
        return
    fi
    sed -nE 's/.*"'"$KEY"'":[[:space:]]*"([^"]*)".*/\1/p' "$FILE" | head -1
}

local_version() {
    V="$(pkgutil --pkg-info "$PKG_ID" 2>/dev/null | awk '/^version:/{print $2; exit}')"
    if [ -n "$V" ]; then
        printf '%s' "$V"
        return
    fi
    /usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist" 2>/dev/null || printf '0'
}

version_gt() {
    awk -v a="$1" -v b="$2" 'BEGIN {
        split(a,A,"."); split(b,B,".");
        for (i=1;i<=4;i++) {
            x=A[i]+0; y=B[i]+0;
            if (x>y) exit 0;
            if (x<y) exit 1;
        }
        exit 1;
    }'
}

LOCAL="$(local_version)"
write_status "checking" "Checking for update." "" "$LOCAL"
log "Checking manifest $MANIFEST_URL (local=$LOCAL)"

MANIFEST="$UPDATE_DIR/update-mac.json"
if ! /usr/bin/curl --fail --location --silent --show-error --retry 3 --connect-timeout 20 --max-time 120 \
    "$MANIFEST_URL" -o "$MANIFEST"; then
    log "Manifest download failed."
    write_status "check_failed" "Could not download update manifest." "" "$LOCAL"
    exit 0
fi

REMOTE="$(json_get version "$MANIFEST")"
URL="$(json_get url "$MANIFEST")"
SHA="$(json_get sha256 "$MANIFEST" | tr '[:upper:]' '[:lower:]')"
TEAM_ID="$(json_get team_id "$MANIFEST")"
FORCE="$(json_get force "$MANIFEST")"

if [ -z "$REMOTE" ] || [ -z "$URL" ] || [ -z "$SHA" ]; then
    log "Invalid manifest: missing version/url/sha256."
    write_status "manifest_invalid" "Manifest missing version, url, or sha256." "$REMOTE" "$LOCAL"
    exit 0
fi

FORCE_NORM="false"
case "$FORCE" in
    true|TRUE|1|yes|YES) FORCE_NORM="true" ;;
esac

if [ "$FORCE_NORM" != "true" ] && ! version_gt "$REMOTE" "$LOCAL"; then
    log "Already up to date (local=$LOCAL remote=$REMOTE)."
    write_status "up_to_date" "Already up to date." "$REMOTE" "$LOCAL"
    rm -f "$TRIGGER" 2>/dev/null || true
    exit 0
fi

PKG="$UPDATE_DIR/ScreenRecorder-$REMOTE.pkg"
TMP="$PKG.tmp"
log "Downloading package $URL"
write_status "downloading" "Downloading package." "$REMOTE" "$LOCAL"
if ! /usr/bin/curl --fail --location --silent --show-error --retry 3 --connect-timeout 20 --max-time 900 \
    "$URL" -o "$TMP"; then
    log "Package download failed."
    write_status "download_failed" "Could not download package." "$REMOTE" "$LOCAL"
    rm -f "$TMP" 2>/dev/null || true
    exit 0
fi

ACTUAL="$(/usr/bin/shasum -a 256 "$TMP" | awk '{print tolower($1)}')"
if [ "$ACTUAL" != "$SHA" ]; then
    log "Hash mismatch expected=$SHA actual=$ACTUAL"
    write_status "hash_mismatch" "Package hash did not match manifest." "$REMOTE" "$LOCAL"
    rm -f "$TMP" 2>/dev/null || true
    exit 0
fi
mv "$TMP" "$PKG"

SIG="$(pkgutil --check-signature "$PKG" 2>&1 || true)"
if ! printf '%s' "$SIG" | grep -q "Developer ID Installer"; then
    log "Signature check failed: $SIG"
    write_status "signature_failed" "Package is not signed by Developer ID Installer." "$REMOTE" "$LOCAL"
    exit 0
fi
if ! printf '%s' "$SIG" | grep -Fq "($EXPECTED_TEAM_ID)"; then
    log "Team ID check failed; expected $EXPECTED_TEAM_ID. Signature: $SIG"
    write_status "team_failed" "Package signature Team ID did not match expected Team ID." "$REMOTE" "$LOCAL"
    exit 0
fi
if ! /usr/sbin/spctl --assess --type install "$PKG" >/dev/null 2>&1; then
    log "spctl assessment failed for $PKG"
    write_status "assessment_failed" "Gatekeeper install assessment failed." "$REMOTE" "$LOCAL"
    exit 0
fi

EXPAND_DIR="$UPDATE_DIR/pkgcheck.$$"
rm -rf "$EXPAND_DIR" 2>/dev/null || true
if /usr/sbin/pkgutil --expand-full "$PKG" "$EXPAND_DIR" >/dev/null 2>&1; then
    CANDIDATE_APP="$(find "$EXPAND_DIR" -path "*/Applications/ScreenRecorder.app" -type d 2>/dev/null | head -1)"
    if [ -z "$CANDIDATE_APP" ]; then
        log "Payload app verification failed: ScreenRecorder.app not found."
        write_status "payload_failed" "Package payload does not contain ScreenRecorder.app." "$REMOTE" "$LOCAL"
        rm -rf "$EXPAND_DIR" 2>/dev/null || true
        exit 0
    fi
    if ! /usr/bin/codesign --verify --deep --strict "$CANDIDATE_APP" >/dev/null 2>&1; then
        log "Payload app codesign verification failed."
        write_status "payload_signature_failed" "Payload app signature verification failed." "$REMOTE" "$LOCAL"
        rm -rf "$EXPAND_DIR" 2>/dev/null || true
        exit 0
    fi
    APP_REQ="$(/usr/bin/codesign -d -r- "$CANDIDATE_APP" 2>&1 || true)"
    if ! printf '%s' "$APP_REQ" | grep -Fq "identifier \"$EXPECTED_BUNDLE_ID\"" || \
       ! printf '%s' "$APP_REQ" | grep -Fq "subject.OU] = $EXPECTED_TEAM_ID"; then
        log "Payload app requirement verification failed: $APP_REQ"
        write_status "payload_team_failed" "Payload app bundle id or Team ID did not match expected values." "$REMOTE" "$LOCAL"
        rm -rf "$EXPAND_DIR" 2>/dev/null || true
        exit 0
    fi
    rm -rf "$EXPAND_DIR" 2>/dev/null || true
else
    log "Package expansion failed for payload verification."
    write_status "payload_expand_failed" "Could not expand package for payload verification." "$REMOTE" "$LOCAL"
    exit 0
fi

log "Installing ScreenRecorder $REMOTE"
write_status "installing" "Installing package." "$REMOTE" "$LOCAL"
if SCREENRECORDER_UPDATER_INSTALL=1 /usr/sbin/installer -pkg "$PKG" -target / >> "$LOG" 2>&1; then
    NEW_LOCAL="$(local_version)"
    if ! /usr/bin/codesign --verify --deep --strict "$APP" >/dev/null 2>&1; then
        log "Installed app signature verification failed."
        write_status "installed_signature_failed" "Installed app signature verification failed." "$REMOTE" "$NEW_LOCAL"
        exit 0
    fi

    CONSOLE_USER="$(scutil <<< "show State:/Users/ConsoleUser" | awk '/Name :/{print $3}')"
    if [ -n "$CONSOLE_USER" ] && [ "$CONSOLE_USER" != "root" ] && [ "$CONSOLE_USER" != "loginwindow" ]; then
        sleep 8
        if ! pgrep -f "ScreenRecorder.app/Contents/MacOS/ScreenRecorder" >/dev/null 2>&1; then
            log "Install completed but app did not launch for active GUI user."
            write_status "launch_failed" "Package installed but recorder did not launch." "$REMOTE" "$NEW_LOCAL"
            exit 0
        fi
    fi

    log "Install complete (now=$NEW_LOCAL)."
    write_status "updated" "Installed and restarted." "$REMOTE" "$NEW_LOCAL"
    ( sleep 10
      launchctl bootout system/ai.bam.screenrecord.updater 2>/dev/null || true
      launchctl bootstrap system /Library/LaunchDaemons/ai.bam.screenrecord.updater.plist 2>/dev/null || true
      launchctl kickstart -k system/ai.bam.screenrecord.updater 2>/dev/null || true
    ) >/dev/null 2>&1 &
    rm -f "$TRIGGER" 2>/dev/null || true
else
    RC=$?
    log "Installer failed rc=$RC"
    write_status "install_failed" "installer failed with rc=$RC." "$REMOTE" "$LOCAL"
fi
