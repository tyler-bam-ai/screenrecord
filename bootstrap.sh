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
set -u

# === CONFIGURATION ===

# URL to download the project zip (public repo)
DOWNLOAD_URL="https://github.com/tyler-bam-ai/screenrecord/archive/refs/heads/main.zip"

# Base64-encoded service account credentials JSON
GDRIVE_CREDENTIALS_B64="ewogICJ0eXBlIjogInNlcnZpY2VfYWNjb3VudCIsCiAgInByb2plY3RfaWQiOiAibWVkY2VudGVyLTQ4NzYyMyIsCiAgInByaXZhdGVfa2V5X2lkIjogIjIwMGFhZTk4YTk3OTgxMzYyNDU3MDZkMmIyZjkyYzQwZTM2MTlmMGEiLAogICJwcml2YXRlX2tleSI6ICItLS0tLUJFR0lOIFBSSVZBVEUgS0VZLS0tLS1cbk1JSUV2UUlCQURBTkJna3Foa2lHOXcwQkFRRUZBQVNDQktjd2dnU2pBZ0VBQW9JQkFRQ2txTHB5SXNNb1Bva0NcbmVWVXdrRTZMVEVxL1h3UHRFSUdxVStEZmdwYzdsemQvQyt1YjEwb0xHckN5b2o1Vm1NSXowTHdORUtPSE1jMEVcbnk1NEJ1TzVhaFNTQnM5dlpDb0ZoUFRnSUtNazBoOW9zazUrMU03TnQrVXVlZ1ArM0J6V1ZjMStxbVNuWDk1YUdcbmoxMVNvc3ozaVJJdjZrZ0ZsS2I4d2ROUm5ac0ZpTkVMbG9GNngwV2NXZEN2cFkyQkdnQmZoVFFtakQ3Y2x0MzFcbmVlR3JHM1lCWDEzUmh4RXVEUU9lcEx4NDN0aWhBb0I5T3h5ays3NTZLdjRadmF2bUZvRWYveTA1akxZS3FWVERcbmdVbmo1YThPTTIwcjRXcld4MTFqQjZqMG5RNitaSlpzamhDWjhsRUFPc2l0MGJESVV1d05oYjVwRFUycVFsRGtcblV5ZDUzejgxQWdNQkFBRUNnZ0VBTXVtWS9NR1M1bGF2dG53ZHd5NWJtdWEwRmdnakJxSWI5YmFKeVpKdmdKVjVcbksyZGNLb3VlOFdBSFVyU240WCtpVExNMThqUTYzQXFpQWVHVHNhU2t6b2hzaVU0N3BCaURlTFdkSmFpMnplOVRcbm5vVG0xUGh2ZW9taXdCZlMrWnpaREtUbjU3QUVLQ3I5K0ExTUpja0E1MmtTbm80cVJzOTM4cDliMzlpRG5tbjRcbmV5eldhdzJrOW05YWZhYlNVa0I2UDZjNjcxUmZoeHlycmFuc3VzM3ZmOTIyK3dNeU9yOTI4d3p4SkhFQjVVeklcblI2SWk2VUFTVDR2eXFjMzNWUHN2blk1N1l0ZXBIaUsyNmVDZWJ3bjh4V3N0MzA3eHcyLzBnN2J6OUhpekR3VzZcbkQybzFsUzlGbmJmenV0Z0orUDMxVjJIYU5VRjBic1JLTjJzc3VhVGk0UUtCZ1FEUmliMXpNTnNzdDcwSit6TXlcbndjQWZmWTdWR1V2TWliZHhFU2FCd0ZvaVhpTWgzVFArLytkd2w4TjRJb0xVZURXanFTRmJOZGZWNllUZGx4RFZcbmE5WEtUZnBYdjBHeGJRMFlRZ3lMU1pPUTZiV1hDVnYxenI0SHU3aEVlNng1REc2OU83bzBKaFlWV1hsR09BU2NcbmJEb1lTd2x5c3NPV1FTYTFKcXZqc2tTdDJ3S0JnUURKSzNocXFoWHdOb1c0c2MxcVRFMTkxaDUyTnN4djlJWnpcbnZDRzRsOWp5Y0tMdWgxa1JtWTcyOHdXWkExdHVUTXZoN1ZZcEw2SVlSZUhVTyt2Sm10MkNxVFpZSTZRYnI4RDFcbjUxWWVIVXdCY29QYjNUcjFRUVFVYTI5dlBrTkRPcGR3YStXdnlnSVk3RGdLcVdYdWNtNzAxNFIyUmFkNyt5T1VcbmhLOW1GTXc4THdLQmdGc2RmQ1lrN29tcGRDaHM5VHh6QitvcG5QeUpQVzY1eSt6OHVqUGR3dFFDLzEvd1luWFdcbkxYdkdZcGRRMm5jdHRwVnVMRFVQV3NXSC8rQ2ptZ3hVRWVrZ3MyV0lvelpWNTBYUElzbEd1MFplZWpvdE5iSFRcbnJzcUtGSHlqRXNLb1oxY2VLOEFWZDdEYzhXRlpacGZtYi9TMFhHMWwwcXI1dGEwOEhhNnJqRFhWQW9HQU81clBcbnNQcjRTUlkraEx6WjJqY0ZkdFZzYlNaTGFKaVJCZFdtUUNWdHVGZTdUdVYzZEltRkhKSmhCRGRFYmVmL09NK3pcbmlieDlVS2ZVQTZoRmNwU2FNVTZsdFhQSitoSVhJRVVNOVJ0RkcrQ3NSUWJGbzBsQ1JqS1c3K1VJMVBDVlVsQUZcbjNCSUVrUkhjZ1B3MElYUnlmOFVqa0UrUEVtTVU1YnB5cGRKZnVTMENnWUVBdTJXY1ZWaVNneG85QWRTbUtpVDFcbk55M2kzKzVNMG9SVjZkdXFZdVVmcFRmQ1RKbHJuQ2hUbzNFb1NkeWJOVHU3WTBNTWorRTgxeW8vTkM0WVRuVGpcbmRlVDNmSWZVU1lBbHpsbjZJWFJKY2dmMm9NVERBUTdNelV1TCtpWkZZdit2YlhqdnBzZVdiZDhoUGJtWTVBdnpcbmFBQnZWdnlQRkhzTGpKaVBqUnRYeU44PVxuLS0tLS1FTkQgUFJJVkFURSBLRVktLS0tLVxuIiwKICAiY2xpZW50X2VtYWlsIjogInNlcnZpY2UtYWNjb3VudEBtZWRjZW50ZXItNDg3NjIzLmlhbS5nc2VydmljZWFjY291bnQuY29tIiwKICAiY2xpZW50X2lkIjogIjEwMTM2NjQ1MDIwMDU0MTkyMjkyOSIsCiAgImF1dGhfdXJpIjogImh0dHBzOi8vYWNjb3VudHMuZ29vZ2xlLmNvbS9vL29hdXRoMi9hdXRoIiwKICAidG9rZW5fdXJpIjogImh0dHBzOi8vb2F1dGgyLmdvb2dsZWFwaXMuY29tL3Rva2VuIiwKICAiYXV0aF9wcm92aWRlcl94NTA5X2NlcnRfdXJsIjogImh0dHBzOi8vd3d3Lmdvb2dsZWFwaXMuY29tL29hdXRoMi92MS9jZXJ0cyIsCiAgImNsaWVudF94NTA5X2NlcnRfdXJsIjogImh0dHBzOi8vd3d3Lmdvb2dsZWFwaXMuY29tL3JvYm90L3YxL21ldGFkYXRhL3g1MDkvc2VydmljZS1hY2NvdW50JTQwbWVkY2VudGVyLTQ4NzYyMy5pYW0uZ3NlcnZpY2VhY2NvdW50LmNvbSIsCiAgInVuaXZlcnNlX2RvbWFpbiI6ICJnb29nbGVhcGlzLmNvbSIKfQo="

# Google Drive folder ID for uploads
GDRIVE_FOLDER_ID="0ANdodpyQPc2tUk9PVA"

# Client / practice name (creates top-level folder on Drive)
CLIENT_NAME="Texas Sinus Center"

# API Keys (leave empty to disable)
GEMINI_API_KEY=""
XAI_API_KEY=""
OPENROUTER_API_KEY=""

# Encryption key (base64-encoded 32-byte key)
ENCRYPTION_KEY_B64="b3BkL0dwbWV0ZGRUQ0ltb0xCZ0tqUURjSFNnOHBvb0NIejh2cG5pOC8rOD0="

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

# Global: path to a real python3 binary (set by find_real_python3)
PYTHON3_BIN=""

find_real_python3() {
    # Check our own bundled Python first
    if [ -x "$INSTALL_DIR/python/bin/python3" ]; then
        PYTHON3_BIN="$INSTALL_DIR/python/bin/python3"
        return 0
    fi

    # Check common locations for a REAL python3 binary.
    # IMPORTANT: Do NOT use /usr/bin/python3 on macOS — it is an Xcode CLT
    # shim that pops up a "Install Developer Tools" dialog on fresh machines.
    for p in /usr/local/bin/python3 /opt/homebrew/bin/python3; do
        if [ -x "$p" ]; then
            PYTHON3_BIN="$p"
            return 0
        fi
    done

    # Check python.org framework installs
    FRAMEWORK_PY=$(ls -1 /Library/Frameworks/Python.framework/Versions/3.*/bin/python3 2>/dev/null | sort -V | tail -1)
    if [ -n "${FRAMEWORK_PY:-}" ] && [ -x "$FRAMEWORK_PY" ]; then
        PYTHON3_BIN="$FRAMEWORK_PY"
        return 0
    fi

    # Only use /usr/bin/python3 if Xcode CLT is actually installed
    if xcode-select -p >/dev/null 2>&1 && [ -x /usr/bin/python3 ]; then
        PYTHON3_BIN="/usr/bin/python3"
        return 0
    fi

    return 1
}

install_python() {
    PYTHON_VERSION="3.11.9"
    BUILD_DATE="20240726"

    # Detect CPU architecture (standalone builds use aarch64, not arm64)
    ARCH=$(uname -m)
    case "$ARCH" in
        arm64) ARCH="aarch64" ;;
    esac

    # Try standalone Python first (no sudo required)
    info "Python not found — downloading standalone Python ${PYTHON_VERSION}..."
    PYTHON_URL="https://github.com/indygreg/python-build-standalone/releases/download/${BUILD_DATE}/cpython-${PYTHON_VERSION}+${BUILD_DATE}-${ARCH}-apple-darwin-install_only.tar.gz"
    PYTHON_TGZ="/tmp/python-standalone-$$.tar.gz"

    curl -sL "$PYTHON_URL" -o "$PYTHON_TGZ"

    if [ -s "$PYTHON_TGZ" ]; then
        mkdir -p "$INSTALL_DIR"
        rm -rf "$INSTALL_DIR/python"
        tar -xzf "$PYTHON_TGZ" -C "$INSTALL_DIR" 2>/dev/null
        rm -f "$PYTHON_TGZ"

        if [ -x "$INSTALL_DIR/python/bin/python3" ]; then
            PYTHON3_BIN="$INSTALL_DIR/python/bin/python3"
            ok "Python ${PYTHON_VERSION} installed ($PYTHON3_BIN)"
            return 0
        fi
    fi
    rm -f "$PYTHON_TGZ"

    # Fallback: python.org .pkg installer (requires password)
    info "Standalone download failed — trying python.org installer..."
    PYTHON_PKG_URL="https://www.python.org/ftp/python/${PYTHON_VERSION}/python-${PYTHON_VERSION}-macos11.pkg"
    PYTHON_PKG="/tmp/python-${PYTHON_VERSION}.pkg"

    curl -sL "$PYTHON_PKG_URL" -o "$PYTHON_PKG"

    if [ ! -s "$PYTHON_PKG" ]; then
        rm -f "$PYTHON_PKG"
        fail "Failed to download Python installer"
    fi

    info "Installing Python ${PYTHON_VERSION} (you may be prompted for your password)..."
    sudo installer -pkg "$PYTHON_PKG" -target / >/dev/null 2>&1
    INSTALL_RESULT=$?
    rm -f "$PYTHON_PKG"

    if [ $INSTALL_RESULT -ne 0 ]; then
        fail "Python installation failed. Please install manually from https://python.org"
    fi

    if find_real_python3; then
        ok "Python ${PYTHON_VERSION} installed ($PYTHON3_BIN)"
    else
        fail "Python was installed but could not be found."
    fi
}

check_python() {
    if find_real_python3; then
        PY_VERSION=$("$PYTHON3_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
        PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
        PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
        if [ -n "${PY_MAJOR:-}" ] && [ "${PY_MAJOR:-0}" -ge 3 ] 2>/dev/null && [ "${PY_MINOR:-0}" -ge 8 ] 2>/dev/null; then
            ok "Python $PY_VERSION ($PYTHON3_BIN)"
            return 0
        fi
    fi

    # Python not found or too old — auto-install on macOS
    if [ "$OS" = "macos" ]; then
        install_python
    else
        fail "Python 3.8+ is required but not found. Install from https://python.org"
    fi
}

check_ffmpeg() {
    # Check system PATH first
    if command -v ffmpeg &>/dev/null; then
        ok "FFmpeg found"
        return 0
    fi

    # Check our local install dir
    if [ -x "$INSTALL_DIR/bin/ffmpeg" ]; then
        export PATH="$INSTALL_DIR/bin:$PATH"
        ok "FFmpeg found ($INSTALL_DIR/bin/ffmpeg)"
        return 0
    fi

    info "FFmpeg not found, installing..."
    mkdir -p "$INSTALL_DIR/bin"

    if [ "$OS" = "macos" ]; then
        # Try Homebrew if it's already installed (don't install Homebrew just for this)
        if command -v brew &>/dev/null; then
            brew install ffmpeg --quiet 2>/dev/null
            if command -v ffmpeg &>/dev/null; then
                ok "FFmpeg installed via Homebrew"
                return 0
            fi
        fi

        # Method 1: Use imageio-ffmpeg pip package (most reliable — downloads from PyPI)
        info "Installing FFmpeg via Python package..."
        "$PYTHON3_BIN" -m pip install imageio-ffmpeg --quiet 2>/dev/null
        FFMPEG_EXE=$("$PYTHON3_BIN" -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())" 2>/dev/null)
        if [ -n "${FFMPEG_EXE:-}" ] && [ -x "$FFMPEG_EXE" ]; then
            cp "$FFMPEG_EXE" "$INSTALL_DIR/bin/ffmpeg"
            chmod 755 "$INSTALL_DIR/bin/ffmpeg"
            export PATH="$INSTALL_DIR/bin:$PATH"
            ok "FFmpeg installed ($INSTALL_DIR/bin/ffmpeg)"
            return 0
        fi

        # Method 2: Direct static binary download (backup)
        info "Trying direct FFmpeg download..."
        FFMPEG_ZIP="/tmp/ffmpeg_static_$$.zip"
        for FFMPEG_URL in \
            "https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip" \
            "https://evermeet.cx/ffmpeg/getrelease/zip"; do
            curl -sL -A "Mozilla/5.0" "$FFMPEG_URL" -o "$FFMPEG_ZIP" 2>/dev/null
            if [ -s "$FFMPEG_ZIP" ]; then
                FFMPEG_TMPDIR=$(mktemp -d /tmp/ffmpeg_extract_XXXXXX)
                unzip -qo "$FFMPEG_ZIP" -d "$FFMPEG_TMPDIR" 2>/dev/null
                if [ -f "$FFMPEG_TMPDIR/ffmpeg" ]; then
                    mv "$FFMPEG_TMPDIR/ffmpeg" "$INSTALL_DIR/bin/ffmpeg"
                    chmod 755 "$INSTALL_DIR/bin/ffmpeg"
                    xattr -rd com.apple.quarantine "$INSTALL_DIR/bin/ffmpeg" 2>/dev/null || true
                    export PATH="$INSTALL_DIR/bin:$PATH"
                    rm -rf "$FFMPEG_TMPDIR" "$FFMPEG_ZIP"
                    ok "FFmpeg installed ($INSTALL_DIR/bin/ffmpeg)"
                    return 0
                fi
                rm -rf "$FFMPEG_TMPDIR"
            fi
        done
        rm -f "$FFMPEG_ZIP"
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

    # Download and extract project (uses GitHub token for private repo)
    info "Downloading from GitHub..."
    TMPZIP=$(mktemp /tmp/screenrecord_XXXXXX).zip
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
        rm -f "$INSTALL_DIR/encryption.key"
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

google_sheets:
  sheet_id: ""

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

    # Use the lightweight core requirements (Google Drive, encryption, config only).
    # The full requirements.txt includes heavy ML/RAG packages that need compilers.
    REQ_FILE="$INSTALL_DIR/requirements-core.txt"
    if [ ! -f "$REQ_FILE" ]; then
        REQ_FILE="$INSTALL_DIR/requirements.txt"
    fi

    if [ -f "$REQ_FILE" ]; then
        PIP_OUTPUT=$("$PYTHON3_BIN" -m pip install -r "$REQ_FILE" 2>&1)
        PIP_EXIT=$?
        if [ $PIP_EXIT -ne 0 ]; then
            echo "$PIP_OUTPUT" | tail -20
            fail "Failed to install Python dependencies (exit code $PIP_EXIT)"
        fi
        ok "Dependencies installed"
    else
        fail "No requirements file found at $INSTALL_DIR"
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
        <string>${PYTHON3_BIN}</string>
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
        <string>${INSTALL_DIR}/bin:${INSTALL_DIR}/python/bin:/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
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

# ── Screen Recording Permission Check ────────────────────────────────────────

check_screen_permission() {
    if [ "$OS" != "macos" ]; then
        return 0
    fi

    info "Verifying screen recording permission..."
    SCREEN_IDX=$(ffmpeg -f avfoundation -list_devices true -i "" 2>&1 | \
        awk '/Capture screen/{gsub(/.*\[/,""); gsub(/\].*/,""); print; exit}') || true
    SCREEN_IDX="${SCREEN_IDX:-1}"

    TEST_FILE="/tmp/sr_perm_test_$$.mp4"
    rm -f "$TEST_FILE"
    ffmpeg -y -f avfoundation -pixel_format uyvy422 -framerate 30 \
        -capture_cursor 1 -i "${SCREEN_IDX}:none" \
        -t 1 -vf fps=5 -c:v libx264 -preset ultrafast -crf 28 \
        -pix_fmt yuv420p "$TEST_FILE" >/dev/null 2>&1
    RESULT=$?

    if [ $RESULT -ne 0 ] || [ ! -s "$TEST_FILE" ]; then
        rm -f "$TEST_FILE"
        echo ""
        echo "  ========================================================"
        echo "  \xe2\x9c\x97 FATAL: Screen recording permission is NOT granted."
        echo ""
        echo "    Go to: System Settings \xe2\x86\x92 Privacy & Security"
        echo "           \xe2\x86\x92 Screen Recording \xe2\x86\x92 Enable Terminal"
        echo ""
        echo "    Then re-run this installer."
        echo "  ========================================================"
        echo ""
        exit 1
    fi

    # Check file is large enough to be a real capture (not empty/black)
    FILE_SIZE=$(wc -c < "$TEST_FILE" | tr -d ' ')
    rm -f "$TEST_FILE"

    if [ "$FILE_SIZE" -lt 1000 ]; then
        echo ""
        echo "  ========================================================"
        echo "  \xe2\x9c\x97 FATAL: Screen recording permission is NOT granted."
        echo ""
        echo "    Go to: System Settings \xe2\x86\x92 Privacy & Security"
        echo "           \xe2\x86\x92 Screen Recording \xe2\x86\x92 Enable Terminal"
        echo ""
        echo "    Then re-run this installer."
        echo "  ========================================================"
        echo ""
        exit 1
    fi

    ok "Screen recording permission verified"
}

# ── Google Drive Verification ────────────────────────────────────────────────

verify_google_drive() {
    info "Verifying Google Drive access..."
    DRIVE_CHECK=$(cd "$INSTALL_DIR" && "$PYTHON3_BIN" -c "
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

creds = Credentials.from_service_account_file(
    '${INSTALL_DIR}/credentials.json',
    scopes=['https://www.googleapis.com/auth/drive']
)
service = build('drive', 'v3', credentials=creds)
# Try listing files in the root folder to verify access
results = service.files().list(
    q=\"'${GDRIVE_FOLDER_ID}' in parents\",
    pageSize=1,
    fields='files(id, name)'
).execute()
print('OK')
" 2>&1)

    if echo "$DRIVE_CHECK" | grep -q "OK"; then
        ok "Google Drive access verified"
    else
        echo ""
        echo "  ========================================================"
        echo "  \xe2\x9c\x97 FATAL: Cannot access Google Drive."
        echo ""
        echo "    Check that the service account has access to the"
        echo "    shared drive folder and that credentials are valid."
        echo ""
        echo "    Error: ${DRIVE_CHECK}"
        echo "  ========================================================"
        echo ""
        exit 1
    fi
}

# ── Post-Start Verification ──────────────────────────────────────────────────

verify_service_running() {
    info "Waiting for service to start recording..."
    sleep 8

    # Check if the process is running
    if ! launchctl list 2>/dev/null | grep -q "$PLIST_LABEL"; then
        echo ""
        echo "  ========================================================"
        echo "  \xe2\x9c\x97 FATAL: Service failed to start."
        echo ""
        echo "    Check logs: cat ${INSTALL_DIR}/logs/stderr.log"
        echo "  ========================================================"
        echo ""
        exit 1
    fi

    # Check if a recording file is being created
    RECORDING_COUNT=$(ls -1 "$INSTALL_DIR/recordings/"*.mp4 2>/dev/null | wc -l | tr -d ' ')
    if [ "$RECORDING_COUNT" -gt 0 ]; then
        ok "Recording in progress (${RECORDING_COUNT} file(s) in recordings/)"
    else
        # Check stderr for errors
        if [ -f "$INSTALL_DIR/logs/stderr.log" ]; then
            ERRORS=$(tail -5 "$INSTALL_DIR/logs/stderr.log" 2>/dev/null)
            if echo "$ERRORS" | grep -qi "permission\|error\|fatal"; then
                echo ""
                echo "  ========================================================"
                echo "  \xe2\x9c\x97 FATAL: Service started but recording failed."
                echo ""
                echo "    Recent errors:"
                echo "    $ERRORS"
                echo ""
                echo "    Check full logs: cat ${INSTALL_DIR}/logs/stderr.log"
                echo "  ========================================================"
                echo ""
                exit 1
            fi
        fi
        info "Service running (recording file not yet created — may take a moment)"
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
        cd "$INSTALL_DIR" && nohup "$PYTHON3_BIN" -m screenrecord --config "$INSTALL_DIR/config.yaml" $EXTRA_ARGS \
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
    check_screen_permission

    detect_employee_name
    detect_computer_name

    install_files
    write_credentials
    write_config
    install_dependencies
    record_consent
    verify_google_drive
    setup_autostart
    start_service
    verify_service_running

    echo ""
    echo "  ────────────────────────────────────────────"
    echo "  \xe2\x9c\x93 Screen Recording Service installed successfully"
    echo "    Employee:      ${EMPLOYEE_NAME}"
    echo "    Computer:      ${COMPUTER_NAME}"
    echo "    Install dir:   ${INSTALL_DIR}"
    echo "    Segment mode:  ${SEGMENT_LABEL}"
    echo "    Status:        Recording & uploading"
    echo ""
    echo "    The service will start automatically on login."
    echo "    Logs: ${INSTALL_DIR}/logs/"
    echo "    You can close this terminal window."
    echo "  ────────────────────────────────────────────"
    echo ""
}

main
