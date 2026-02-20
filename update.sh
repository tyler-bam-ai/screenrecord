#!/bin/bash
# Quick updater — paste this one-liner into Terminal:
#
#   curl -sL https://raw.githubusercontent.com/tyler-bam-ai/screenrecord/main/update.sh | bash
#
set -u

INSTALL_DIR="$HOME/.screenrecord"
PLIST_LABEL="com.screenrecord.service"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
DOWNLOAD_URL="https://github.com/tyler-bam-ai/screenrecord/archive/refs/heads/main.zip"

echo ""
echo "  Updating Screen Recording Service..."

# Stop the service
if [ -f "$PLIST_PATH" ]; then
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
fi

# Download latest code
TMPZIP=$(mktemp /tmp/screenrecord_XXXXXX).zip
curl -sL "$DOWNLOAD_URL" -o "$TMPZIP"
if [ ! -s "$TMPZIP" ]; then
    rm -f "$TMPZIP"
    echo "  ✗ Download failed."
    exit 1
fi

# Extract and replace code (preserves config, credentials, recordings, logs)
TMPDIR_EXTRACT=$(mktemp -d /tmp/sr_update_XXXXXX)
unzip -qo "$TMPZIP" -d "$TMPDIR_EXTRACT"
rm -f "$TMPZIP"
NESTED=$(find "$TMPDIR_EXTRACT" -maxdepth 1 -type d | tail -1)

# Only replace the Python package and root-level non-data files
if [ -d "$NESTED/screenrecord" ]; then
    rm -rf "$INSTALL_DIR/screenrecord"
    cp -R "$NESTED/screenrecord" "$INSTALL_DIR/screenrecord"
    # Copy updated requirements and tools (but not config/credentials)
    for f in requirements.txt requirements-core.txt tools; do
        [ -e "$NESTED/$f" ] && cp -R "$NESTED/$f" "$INSTALL_DIR/"
    done
fi
rm -rf "$TMPDIR_EXTRACT"

# Record the commit SHA so auto-updater knows we're current
SHA=$(curl -s "https://api.github.com/repos/tyler-bam-ai/screenrecord/commits/main" 2>/dev/null | grep '"sha"' | head -1 | sed 's/.*"sha"[[:space:]]*:[[:space:]]*"\([a-f0-9]*\)".*/\1/' || true)
[ -n "${SHA:-}" ] && echo "$SHA" > "$INSTALL_DIR/.commit_sha"

# Restart the service
if [ -f "$PLIST_PATH" ]; then
    launchctl load "$PLIST_PATH" 2>/dev/null
    launchctl start "$PLIST_LABEL" 2>/dev/null || true
fi

echo "  ✓ Updated and restarted. All future updates will happen automatically."
echo ""
