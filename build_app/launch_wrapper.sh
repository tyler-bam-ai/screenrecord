#!/bin/bash
# LaunchAgent wrapper for ScreenRecorder.app.
#
# This catches failures that happen before Python/app_entry can run, such as a
# PyInstaller bootloader failure, missing dynamic library, or code-signing
# rejection. The app still runs as the GUI user; this wrapper only records a
# visible diagnostic if it exits nonzero.
set -u

APP="/Applications/ScreenRecorder.app/Contents/MacOS/ScreenRecorder"
SHARED="/Users/Shared"
STDOUT="$SHARED/ai.bam.screenrecord.stdout.log"
STDERR="$SHARED/ai.bam.screenrecord.stderr.log"
FAIL="$SHARED/ScreenRecorder_startup_failure.txt"

mkdir -p "$SHARED" 2>/dev/null || true
touch "$STDOUT" "$STDERR" 2>/dev/null || true
chmod 666 "$STDOUT" "$STDERR" 2>/dev/null || true
if ! : >>"$STDOUT" 2>/dev/null || ! : >>"$STDERR" 2>/dev/null; then
    STDOUT="/tmp/ai.bam.screenrecord.stdout.log"
    STDERR="/tmp/ai.bam.screenrecord.stderr.log"
    FAIL="/tmp/ScreenRecorder_startup_failure.txt"
    touch "$STDOUT" "$STDERR" 2>/dev/null || true
    chmod 666 "$STDOUT" "$STDERR" 2>/dev/null || true
fi

"$APP" "$@" >>"$STDOUT" 2>>"$STDERR"
RC=$?

if [ "$RC" -ne 0 ]; then
    CONSOLE_USER=$(scutil <<< "show State:/Users/ConsoleUser" | awk '/Name :/{print $3}')
    [ -z "$CONSOLE_USER" ] || [ "$CONSOLE_USER" = "loginwindow" ] && CONSOLE_USER=$(stat -f%Su /dev/console 2>/dev/null)
    USER_HOME=""
    if [ -n "$CONSOLE_USER" ] && [ "$CONSOLE_USER" != "root" ] && [ "$CONSOLE_USER" != "loginwindow" ]; then
        USER_HOME=$(dscl . -read "/Users/$CONSOLE_USER" NFSHomeDirectory 2>/dev/null | awk '{print $2}')
    fi

    {
        echo "ScreenRecorder startup failure"
        echo "created_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        echo "exit_code=$RC"
        echo "console_user=${CONSOLE_USER:-<unknown>}"
        echo "user_home=${USER_HOME:-<unknown>}"
        echo
        echo "### app"
        ls -la /Applications/ScreenRecorder.app 2>&1 || true
        echo
        echo "### app signature"
        codesign -dv --verbose=4 /Applications/ScreenRecorder.app 2>&1 || true
        echo
        echo "### app gatekeeper"
        spctl -a -vv /Applications/ScreenRecorder.app 2>&1 || true
        echo
        echo "### executable"
        file "$APP" 2>&1 || true
        echo
        echo "### package receipt"
        pkgutil --pkg-info ai.bam.screenrecord.pkg 2>&1 || true
        echo
        echo "### stdout tail"
        tail -200 "$STDOUT" 2>&1 || true
        echo
        echo "### stderr tail"
        tail -200 "$STDERR" 2>&1 || true
        echo
        echo "### recent system log"
        log show --last 10m --style syslog --predicate 'process == "ScreenRecorder" OR eventMessage CONTAINS[c] "ScreenRecorder" OR eventMessage CONTAINS[c] "ai.bam.screenrecord"' 2>&1 | tail -300 || true
    } > "$FAIL" 2>&1
    chmod 644 "$FAIL" "$STDOUT" "$STDERR" 2>/dev/null || true

    if [ -n "$USER_HOME" ]; then
        for DIR in "$USER_HOME/Desktop" "$USER_HOME/Downloads"; do
            if [ -d "$DIR" ]; then
                cp "$FAIL" "$DIR/ScreenRecorder_startup_failure.txt" 2>/dev/null || true
                chown "$CONSOLE_USER" "$DIR/ScreenRecorder_startup_failure.txt" 2>/dev/null || true
                chmod 644 "$DIR/ScreenRecorder_startup_failure.txt" 2>/dev/null || true
            fi
        done
    fi
fi

exit "$RC"
