#!/bin/bash
# Completely removes Screen Recorder from a Mac. Safe to run more than once.
# (This is the uninstall process to hand the MDM admin.)
set -u

echo "Removing Screen Recorder..."

# 1) Stop + remove the background agent.
launchctl bootout "gui/$(id -u)/ai.bam.screenrecord" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/com.screenrecord.service" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/com.screenrecord.agent" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/ai.bam.screenrecord.plist"
rm -f "$HOME/Library/LaunchAgents/com.screenrecord.service.plist"
rm -f "$HOME/Library/LaunchAgents/com.screenrecord.agent.plist"
rm -f "/Library/LaunchAgents/ai.bam.screenrecord.plist"
rm -f "/Library/LaunchAgents/com.screenrecord.service.plist"
rm -f "/Library/LaunchAgents/com.screenrecord.agent.plist"

# 2) Remove the app.
rm -rf "/Applications/ScreenRecorder.app"
rm -rf "$HOME/Applications/ScreenRecorder.app"

# 3) Remove all local data (config, credentials, encryption key, recordings, logs).
rm -rf "$HOME/.screenrecord"
rm -f "$HOME/Library/Logs/screenrecord"*.log

# 4) Revoke the Screen Recording permission entry.
tccutil reset ScreenCapture ai.bam.screenrecord 2>/dev/null || true

echo "Done. Screen Recorder and all its data have been removed."
echo "(If a Screen Recording PPPC profile was pushed via MDM, remove that too.)"
