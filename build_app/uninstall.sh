#!/bin/bash
# Completely removes Screen Recorder from a Mac. Safe to run more than once.
# (This is the uninstall process to hand the MDM admin.)
#
# MUST be run as root (sudo): it removes a system LaunchDaemon and the pkg
# receipt. Without root the root-updater daemon survives and SILENTLY
# REINSTALLS the app the next time a newer build is published.
set -u

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: run this uninstaller with sudo (it must remove a root LaunchDaemon)." >&2
    echo "  sudo bash uninstall.sh" >&2
    exit 1
fi

echo "Removing Screen Recorder..."

# Resolve the real console user so we clean their per-user agent + data even
# though we're running as root.
CONSOLE_USER=$(stat -f%Su /dev/console 2>/dev/null)
if [ -n "$CONSOLE_USER" ] && [ "$CONSOLE_USER" != "root" ]; then
    UID_N=$(id -u "$CONSOLE_USER" 2>/dev/null)
    USER_HOME=$(dscl . -read "/Users/$CONSOLE_USER" NFSHomeDirectory 2>/dev/null | awk '{print $2}')
else
    UID_N=""
    USER_HOME="$HOME"
fi

# 1) Stop + remove the root updater LaunchDaemon FIRST — otherwise it can
#    reinstall the app after we delete it.
launchctl bootout system/ai.bam.screenrecord.updater 2>/dev/null || true
rm -f "/Library/LaunchDaemons/ai.bam.screenrecord.updater.plist"

# 2) Stop + remove the per-user background agent (in the console user's GUI domain).
if [ -n "$UID_N" ]; then
    launchctl bootout "gui/$UID_N/ai.bam.screenrecord" 2>/dev/null || true
    launchctl bootout "gui/$UID_N/com.screenrecord.service" 2>/dev/null || true
    launchctl bootout "gui/$UID_N/com.screenrecord.agent" 2>/dev/null || true
fi
rm -f "$USER_HOME/Library/LaunchAgents/ai.bam.screenrecord.plist"
rm -f "$USER_HOME/Library/LaunchAgents/com.screenrecord.service.plist"
rm -f "$USER_HOME/Library/LaunchAgents/com.screenrecord.agent.plist"
rm -f "/Library/LaunchAgents/ai.bam.screenrecord.plist"
rm -f "/Library/LaunchAgents/com.screenrecord.service.plist"
rm -f "/Library/LaunchAgents/com.screenrecord.agent.plist"

# 3) Remove the app and the shared support/updater files.
rm -rf "/Applications/ScreenRecorder.app"
rm -rf "$USER_HOME/Applications/ScreenRecorder.app"
rm -rf "/Library/Application Support/ScreenRecorder"
rm -rf "/Library/Logs/ScreenRecorder"
rm -rf "/Users/Shared/ScreenRecorder"
rm -f "/Users/Shared/ScreenRecorder_update_now"

# 4) Remove all local data (config, credentials, encryption key, recordings, logs).
rm -rf "$USER_HOME/.screenrecord"
rm -f "$USER_HOME/Library/Logs/screenrecord"*.log

# 5) Forget the package receipt so a stale receipt can't confuse a future install
#    and the updater can't treat the machine as "has an old version".
pkgutil --forget ai.bam.screenrecord.pkg 2>/dev/null || true

# 6) Revoke the privacy permission entries.
tccutil reset ScreenCapture ai.bam.screenrecord 2>/dev/null || true
tccutil reset ListenEvent ai.bam.screenrecord 2>/dev/null || true
tccutil reset Accessibility ai.bam.screenrecord 2>/dev/null || true

echo "Done. Screen Recorder and all its data have been removed."
echo "(If a Screen Recording PPPC profile was pushed via MDM, remove that too.)"
