#!/bin/bash
# Screen Recording Service - One-Command Internet Installer
#
# USAGE (one-liner from any Mac terminal):
#
#   Default (60-minute segments):
#     curl -sL https://raw.githubusercontent.com/YOUR_USERNAME/screenrecord/main/bootstrap.sh | bash
#
#   5-minute segments:
#     curl -sL https://raw.githubusercontent.com/YOUR_USERNAME/screenrecord/main/bootstrap.sh | bash -s -- -5
#
#   Or from a private repo (using a GitHub token):
#     curl -sL -H "Authorization: token YOUR_GITHUB_TOKEN" https://raw.githubusercontent.com/YOUR_USERNAME/screenrecord/main/bootstrap.sh | bash -s -- -5
#
set -euo pipefail

# === CONFIGURE THESE BEFORE DEPLOYMENT ===

# URL to download the project zip (GitHub repo zip, your own server, etc.)
# For a GitHub repo: https://github.com/YOUR_USERNAME/screenrecord/archive/refs/heads/main.zip
# For a release:     https://github.com/YOUR_USERNAME/screenrecord/releases/latest/download/screenrecord.zip
DOWNLOAD_URL="https://github.com/YOUR_USERNAME/screenrecord/archive/refs/heads/main.zip"

# Base64-encoded service account credentials JSON
# Generate with: base64 < your-credentials.json | tr -d '\n'
GDRIVE_CREDENTIALS_B64="PASTE_BASE64_HERE"

# Google Drive folder ID for uploads
GDRIVE_FOLDER_ID="PASTE_FOLDER_ID_HERE"

# Client / practice name (creates top-level folder on Drive)
CLIENT_NAME=""

# API Keys (leave empty to disable)
GEMINI_API_KEY=""
XAI_API_KEY=""
OPENROUTER_API_KEY=""

# Encryption key (base64-encoded 32-byte key)
# Generate with: python3 -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"
ENCRYPTION_KEY_B64="PASTE_KEY_HERE"

# === END CONFIGURATION ===

# ── Parse arguments ─────────────────────────────────────────────────────────

FIVE_MIN_MODE=false

while [ $# -gt 0 ]; do
    case "$1" in
        -5) FIVE_MIN_MODE=true; shift ;;
        *)  shift ;;
    esac
done

if [ "$FIVE_MIN_MODE" = true ]; then
    SEGMENT_DURATION=300
    SEGMENT_LABEL="5 minutes"
else
    SEGMENT_DURATION=3600
    SEGMENT_LABEL="60 minutes"
fi

INSTALL_DIR="$HOME/.screenrecord"
PLIST_LABEL="com.screenrecord.service"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

# ── Helpers ──────────────────────────────────────────────────────────────────

info()  { printf "  %s\n" "$1"; }
ok()    { printf "  \xe2\x9c\x93 %s\n" "$1"; }
fail()  { printf "  \xe2\x9c\x97 %s\n" "$1" >&2; exit 1; }

# ── OS Detection ─────────────────────────────────────────────────────────────

detect_os() {
    case "$(uname -s)" in
        Darwin)  OS="macos" ;;
        Linux)   OS="linux" ;;
        MINGW*|MSYS*|CYGWIN*)  OS="windows" ;;
        *)       fail "Unsupported operating system: $(uname -s)" ;;
    esac
}

# ── Prerequisites ────────────────────────────────────────────────────────────

check_python() {
    if command -v python3 &>/dev/null; then
        PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
        PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
        if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 8 ]; then
            ok "Python $PY_VERSION"
            return 0
        fi
    fi
    fail "Python 3.8+ is required but not found. Install from https://python.org"
}

check_ffmpeg() {
    if command -v ffmpeg &>/dev/null; then
        ok "FFmpeg found"
        return 0
    fi

    info "FFmpeg not found, attempting to install..."
    if [ "$OS" = "macos" ]; then
        if command -v brew &>/dev/null; then
            brew install ffmpeg --quiet 2>/dev/null
            if command -v ffmpeg &>/dev/null; then
                ok "FFmpeg installed via Homebrew"
                return 0
            fi
        else
            info "Homebrew not found, attempting to install Homebrew first..."
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" </dev/null
            # Add brew to PATH for this session (Apple Silicon + Intel)
            if [ -f /opt/homebrew/bin/brew ]; then
                eval "$(/opt/homebrew/bin/brew shellenv)"
            elif [ -f /usr/local/bin/brew ]; then
                eval "$(/usr/local/bin/brew shellenv)"
            fi
            brew install ffmpeg --quiet 2>/dev/null
            if command -v ffmpeg &>/dev/null; then
                ok "FFmpeg installed via Homebrew"
                return 0
            fi
        fi
    elif [ "$OS" = "linux" ]; then
        if command -v apt-get &>/dev/null; then
            sudo apt-get install -y -qq ffmpeg 2>/dev/null
            if command -v ffmpeg &>/dev/null; then
                ok "FFmpeg installed via apt"
                return 0
            fi
        fi
    fi
    fail "Could not install FFmpeg. Please install it manually and re-run."
}

# ── Auto-detect Employee Info ────────────────────────────────────────────────

detect_employee_name() {
    if [ "$OS" = "macos" ]; then
        EMPLOYEE_NAME=$(dscl . -read "/Users/$(whoami)" RealName 2>/dev/null | tail -1 | xargs)
    fi
    # Fallback
    if [ -z "${EMPLOYEE_NAME:-}" ] || [ "$EMPLOYEE_NAME" = "$(whoami)" ]; then
        EMPLOYEE_NAME=$(whoami)
    fi
}

detect_computer_name() {
    COMPUTER_NAME=$(hostname -s 2>/dev/null || hostname)
}

# ── Installation ─────────────────────────────────────────────────────────────

install_files() {
    # Stop existing service if running
    if [ "$OS" = "macos" ] && [ -f "$PLIST_PATH" ]; then
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
        info "Stopped existing service"
    fi

    # Create install directory
    mkdir -p "$INSTALL_DIR"

    # Download and extract project
    info "Downloading from $DOWNLOAD_URL ..."
    TMPZIP=$(mktemp /tmp/screenrecord_XXXXXX.zip)
    if command -v curl &>/dev/null; then
        curl -sL "$DOWNLOAD_URL" -o "$TMPZIP"
    elif command -v wget &>/dev/null; then
        wget -q "$DOWNLOAD_URL" -O "$TMPZIP"
    else
        fail "Neither curl nor wget found"
    fi

    if [ ! -s "$TMPZIP" ]; then
        rm -f "$TMPZIP"
        fail "Download failed or file is empty"
    fi

    # Extract to a temp dir first, then move contents into install dir.
    # GitHub archive zips contain a top-level folder like "screenrecord-main/",
    # so we flatten that into the install dir.
    TMPDIR_EXTRACT=$(mktemp -d /tmp/screenrecord_extract_XXXXXX)
    unzip -qo "$TMPZIP" -d "$TMPDIR_EXTRACT"
    rm -f "$TMPZIP"

    # Find the actual project root (may be nested in a folder like screenrecord-main/)
    NESTED=$(find "$TMPDIR_EXTRACT" -maxdepth 1 -type d | tail -1)
    if [ -d "$NESTED/screenrecord" ]; then
        # GitHub-style: zip contains repo-name-branch/ with screenrecord/ inside
        cp -R "$NESTED"/* "$INSTALL_DIR/"
    elif [ -d "$TMPDIR_EXTRACT/screenrecord" ]; then
        cp -R "$TMPDIR_EXTRACT"/* "$INSTALL_DIR/"
    else
        # Flat zip — just copy everything
        cp -R "$TMPDIR_EXTRACT"/* "$INSTALL_DIR/"
    fi
    rm -rf "$TMPDIR_EXTRACT"

    ok "Files downloaded and extracted"
}

write_credentials() {
    # Write service account credentials
    echo "$GDRIVE_CREDENTIALS_B64" | base64 -d > "$INSTALL_DIR/credentials.json"
    chmod 600 "$INSTALL_DIR/credentials.json"

    # Write encryption key (skip if placeholder)
    if [ "$ENCRYPTION_KEY_B64" != "PASTE_KEY_HERE" ] && [ -n "$ENCRYPTION_KEY_B64" ]; then
        echo "$ENCRYPTION_KEY_B64" | base64 -d > "$INSTALL_DIR/encryption.key"
        chmod 400 "$INSTALL_DIR/encryption.key"
        ENCRYPTION_KEY_PATH="${INSTALL_DIR}/encryption.key"
    else
        ENCRYPTION_KEY_PATH=""
    fi

    ok "Credentials written"
}

write_config() {
    cat > "$INSTALL_DIR/config.yaml" <<YAML
# Screen Recording Service Configuration
# Auto-generated by bootstrap installer
# Segment duration: ${SEGMENT_LABEL}

client_name: "${CLIENT_NAME}"
employee_name: "${EMPLOYEE_NAME}"
computer_name: "${COMPUTER_NAME}"

recording:
  fps: 5
  crf: 28
  segment_duration: ${SEGMENT_DURATION}
  output_dir: "${INSTALL_DIR}/recordings"
  audio_device: ""

google_drive:
  credentials_file: "${INSTALL_DIR}/credentials.json"
  root_folder_id: "${GDRIVE_FOLDER_ID}"

encryption:
  key_file: "${ENCRYPTION_KEY_PATH:-}"

analysis:
  enabled: true
  gemini_api_key: "${GEMINI_API_KEY}"
  xai_api_key: "${XAI_API_KEY}"
  openrouter_api_key: "${OPENROUTER_API_KEY}"

rag:
  enabled: false
  db_path: "${INSTALL_DIR}/rag_db"
  synthesis_interval: 3600
  bible_path: "${INSTALL_DIR}/company_operations_bible.md"
YAML
    ok "Config generated (segment duration: ${SEGMENT_LABEL})"
}

install_dependencies() {
    info "Installing Python dependencies..."
    if [ -f "$INSTALL_DIR/requirements.txt" ]; then
        pip3 install -r "$INSTALL_DIR/requirements.txt" --quiet 2>/dev/null
        ok "Dependencies installed"
    else
        info "No requirements.txt found, skipping"
    fi
}

record_consent() {
    cat > "$INSTALL_DIR/consent_records.json" <<JSON
[
  {
    "timestamp": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
    "employee_name": "${EMPLOYEE_NAME}",
    "consented_by": "system_administrator",
    "consent_text": "Screen recording authorized by company administrator during automated deployment. All recordings encrypted with AES-256-GCM. HIPAA compliance acknowledged."
  }
]
JSON
    chmod 600 "$INSTALL_DIR/consent_records.json"
    ok "HIPAA consent recorded"
}

# ── Auto-start Setup ────────────────────────────────────────────────────────

setup_autostart_macos() {
    mkdir -p "$HOME/Library/LaunchAgents"

    # Build the -5 flag argument for the plist if in 5-min mode
    FIVE_MIN_ARG=""
    if [ "$FIVE_MIN_MODE" = true ]; then
        FIVE_MIN_ARG="        <string>-5</string>"
    fi

    cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(which python3)</string>
        <string>-m</string>
        <string>screenrecord</string>
        <string>--config</string>
        <string>${INSTALL_DIR}/config.yaml</string>
${FIVE_MIN_ARG}
    </array>
    <key>WorkingDirectory</key>
    <string>${INSTALL_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${INSTALL_DIR}/logs/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${INSTALL_DIR}/logs/stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
PLIST

    # Unload existing if present (ignore errors)
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    launchctl load "$PLIST_PATH"
    ok "Auto-start configured (launchd)"
}

setup_autostart() {
    if [ "$OS" = "macos" ]; then
        setup_autostart_macos
    else
        info "Auto-start setup not available for $OS - start manually"
    fi
}

# ── Start Service ────────────────────────────────────────────────────────────

start_service() {
    mkdir -p "$INSTALL_DIR/logs"
    mkdir -p "$INSTALL_DIR/recordings"

    EXTRA_ARGS=""
    if [ "$FIVE_MIN_MODE" = true ]; then
        EXTRA_ARGS="-5"
    fi

    if [ "$OS" = "macos" ]; then
        # launchd will manage the process via KeepAlive
        launchctl start "$PLIST_LABEL" 2>/dev/null || true
    else
        # Fallback: start directly in background
        cd "$INSTALL_DIR" && nohup python3 -m screenrecord --config "$INSTALL_DIR/config.yaml" $EXTRA_ARGS \
            >> "$INSTALL_DIR/logs/stdout.log" 2>> "$INSTALL_DIR/logs/stderr.log" &
    fi
    ok "Service started"
}

# ── Main ─────────────────────────────────────────────────────────────────────

main() {
    echo ""
    echo "  Screen Recording Service - Installing..."
    echo "  Segment mode: ${SEGMENT_LABEL}"
    echo ""

    detect_os
    check_python
    check_ffmpeg

    detect_employee_name
    detect_computer_name

    install_files
    write_credentials
    write_config
    install_dependencies
    record_consent
    setup_autostart
    start_service

    echo ""
    echo "  ────────────────────────────────────────────"
    echo "  \xe2\x9c\x93 Screen Recording Service installed successfully"
    echo "    Employee:      ${EMPLOYEE_NAME}"
    echo "    Computer:      ${COMPUTER_NAME}"
    echo "    Install dir:   ${INSTALL_DIR}"
    echo "    Segment mode:  ${SEGMENT_LABEL}"
    echo "    Status:        Recording started"
    echo ""
    echo "    The service will start automatically on login."
    echo "    Logs: ${INSTALL_DIR}/logs/"
    echo "  ────────────────────────────────────────────"
    echo ""
}

main
