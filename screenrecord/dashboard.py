"""Monitoring dashboard for the screen recording service.

A standalone Flask web application that reads heartbeat JSON files from
Google Drive and displays a live status overview of all recording machines,
grouped by client/practice.

Usage::

    python -m screenrecord.dashboard --config config.yaml --port 8080

The dashboard password can be set via:
  - ``dashboard_password`` key in the YAML config file
  - ``DASHBOARD_PASSWORD`` environment variable
  - Falls back to ``bam2024``
"""

import argparse
import io
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Dict, List, Optional

import yaml
from flask import Flask, Response, redirect, render_template_string, request, session, url_for
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
HEARTBEAT_FOLDER_NAME = "_heartbeats"

# Threshold constants (in minutes)
ACTIVE_THRESHOLD_MIN = 10
STALE_THRESHOLD_MIN = 30

app = Flask(__name__)

# --------------------------------------------------------------------------
# Configuration holder (populated at startup)
# --------------------------------------------------------------------------
_app_config: Dict[str, Any] = {}
_drive_service = None


def _init_drive_service(config: Dict[str, Any]):
    """Authenticate with Google Drive and store the service globally."""
    global _drive_service
    drive_cfg = config["google_drive"]
    credentials_file = drive_cfg["credentials_file"]
    creds = Credentials.from_service_account_file(
        credentials_file, scopes=SCOPES
    )
    _drive_service = build("drive", "v3", credentials=creds)
    logger.info("Google Drive service initialized for dashboard.")


def _get_password() -> str:
    """Resolve the dashboard password from config, env, or default."""
    if _app_config.get("dashboard_password"):
        return _app_config["dashboard_password"]
    return os.environ.get("DASHBOARD_PASSWORD", "bam2024")


# --------------------------------------------------------------------------
# Auth decorator
# --------------------------------------------------------------------------

def login_required(f):
    """Simple session-based authentication check."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# --------------------------------------------------------------------------
# Drive helpers
# --------------------------------------------------------------------------

def _find_heartbeat_folder(root_folder_id: str) -> Optional[str]:
    """Find the _heartbeats folder under the Drive root. Returns ID or None."""
    query = (
        f"name='{HEARTBEAT_FOLDER_NAME}' and '{root_folder_id}' in parents "
        f"and mimeType='application/vnd.google-apps.folder' "
        f"and trashed=false"
    )
    try:
        results = (
            _drive_service.files()
            .list(q=query, spaces="drive", fields="files(id)")
            .execute()
        )
        files = results.get("files", [])
        return files[0]["id"] if files else None
    except HttpError:
        logger.exception("Error searching for heartbeat folder.")
        return None


def _list_heartbeat_files(folder_id: str) -> List[Dict[str, Any]]:
    """List all heartbeat JSON files in the given folder."""
    query = (
        f"'{folder_id}' in parents "
        f"and mimeType='application/json' "
        f"and trashed=false"
    )
    try:
        results = (
            _drive_service.files()
            .list(
                q=query,
                spaces="drive",
                fields="files(id, name)",
                pageSize=200,
            )
            .execute()
        )
        return results.get("files", [])
    except HttpError:
        logger.exception("Error listing heartbeat files.")
        return []


def _download_json(file_id: str) -> Optional[Dict[str, Any]]:
    """Download and parse a JSON file from Drive."""
    try:
        content = (
            _drive_service.files()
            .get_media(fileId=file_id)
            .execute()
        )
        if isinstance(content, bytes):
            return json.loads(content.decode("utf-8"))
        return json.loads(content)
    except (HttpError, json.JSONDecodeError, UnicodeDecodeError):
        logger.exception("Error downloading/parsing heartbeat file %s.", file_id)
        return None


def _fetch_all_heartbeats() -> List[Dict[str, Any]]:
    """Fetch all heartbeat records from Google Drive.

    Returns a list of heartbeat dictionaries augmented with computed
    ``status_color`` and ``minutes_ago`` fields.
    """
    root_folder_id = _app_config.get("google_drive", {}).get("root_folder_id", "")
    if not root_folder_id:
        logger.error("No root_folder_id configured; cannot fetch heartbeats.")
        return []

    folder_id = _find_heartbeat_folder(root_folder_id)
    if folder_id is None:
        logger.warning("Heartbeat folder not found in Drive.")
        return []

    files = _list_heartbeat_files(folder_id)
    heartbeats: List[Dict[str, Any]] = []

    for f in files:
        data = _download_json(f["id"])
        if data is None:
            continue

        # Compute how long since the last heartbeat
        last_hb = data.get("last_heartbeat", "")
        minutes_ago = _minutes_since(last_hb)
        data["minutes_ago"] = minutes_ago

        # Determine color based on age
        if data.get("status") == "stopped":
            data["status_color"] = "red"
            data["status_label"] = "stopped"
        elif data.get("status") == "error":
            data["status_color"] = "red"
            data["status_label"] = "error"
        elif minutes_ago is not None and minutes_ago <= ACTIVE_THRESHOLD_MIN:
            data["status_color"] = "#22c55e"  # green
            data["status_label"] = "recording"
        elif minutes_ago is not None and minutes_ago <= STALE_THRESHOLD_MIN:
            data["status_color"] = "#eab308"  # yellow
            data["status_label"] = "stale"
        else:
            data["status_color"] = "#ef4444"  # red
            data["status_label"] = "offline"

        # Human-readable "last seen"
        if minutes_ago is not None:
            if minutes_ago < 1:
                data["last_seen"] = "just now"
            elif minutes_ago < 60:
                data["last_seen"] = f"{int(minutes_ago)}m ago"
            elif minutes_ago < 1440:
                data["last_seen"] = f"{minutes_ago / 60:.1f}h ago"
            else:
                data["last_seen"] = f"{minutes_ago / 1440:.1f}d ago"
        else:
            data["last_seen"] = "unknown"

        heartbeats.append(data)

    # Sort: active first, then by client and employee
    color_order = {"#22c55e": 0, "#eab308": 1, "#ef4444": 2}
    heartbeats.sort(key=lambda h: (
        color_order.get(h.get("status_color", ""), 3),
        h.get("client_name", ""),
        h.get("employee_name", ""),
    ))

    return heartbeats


def _minutes_since(iso_timestamp: str) -> Optional[float]:
    """Return the number of minutes since the given ISO timestamp."""
    if not iso_timestamp:
        return None
    try:
        dt = datetime.fromisoformat(iso_timestamp)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return delta.total_seconds() / 60.0
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------
# Group heartbeats by client
# --------------------------------------------------------------------------

def _group_by_client(heartbeats: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Group heartbeat records by client_name, preserving order."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for hb in heartbeats:
        client = hb.get("client_name", "Unknown")
        groups.setdefault(client, []).append(hb)
    return groups


# --------------------------------------------------------------------------
# HTML template
# --------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>Screen Recorder Dashboard</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: #0f172a;
    color: #e2e8f0;
    min-height: 100vh;
    padding: 24px;
  }
  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 32px;
    padding-bottom: 16px;
    border-bottom: 1px solid #1e293b;
  }
  .header h1 {
    font-size: 22px;
    font-weight: 600;
    color: #f8fafc;
  }
  .header-right {
    display: flex;
    align-items: center;
    gap: 16px;
    font-size: 13px;
    color: #94a3b8;
  }
  .header-right a {
    color: #94a3b8;
    text-decoration: none;
    font-size: 13px;
  }
  .header-right a:hover { color: #e2e8f0; }
  .summary {
    display: flex;
    gap: 16px;
    margin-bottom: 28px;
    flex-wrap: wrap;
  }
  .summary-card {
    background: #1e293b;
    border-radius: 8px;
    padding: 16px 24px;
    min-width: 140px;
  }
  .summary-card .label {
    font-size: 12px;
    color: #94a3b8;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 4px;
  }
  .summary-card .value {
    font-size: 28px;
    font-weight: 700;
  }
  .client-group {
    margin-bottom: 28px;
  }
  .client-group h2 {
    font-size: 15px;
    font-weight: 600;
    color: #94a3b8;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 10px;
    padding-left: 4px;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    background: #1e293b;
    border-radius: 8px;
    overflow: hidden;
  }
  th {
    text-align: left;
    padding: 10px 16px;
    font-size: 12px;
    font-weight: 600;
    color: #64748b;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    background: #1e293b;
    border-bottom: 1px solid #334155;
  }
  td {
    padding: 12px 16px;
    font-size: 14px;
    border-bottom: 1px solid rgba(51, 65, 85, 0.5);
  }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(51, 65, 85, 0.3); }
  .status-dot {
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    margin-right: 8px;
    vertical-align: middle;
  }
  .status-text {
    font-size: 12px;
    text-transform: capitalize;
  }
  .mono { font-family: "SF Mono", "Fira Code", monospace; font-size: 13px; color: #cbd5e1; }
  .empty-state {
    text-align: center;
    padding: 60px 20px;
    color: #64748b;
  }
  .empty-state p { margin-top: 8px; font-size: 14px; }

  /* Login page */
  .login-container {
    max-width: 360px;
    margin: 120px auto;
    background: #1e293b;
    border-radius: 12px;
    padding: 40px;
  }
  .login-container h1 {
    font-size: 20px;
    font-weight: 600;
    margin-bottom: 24px;
    text-align: center;
    color: #f8fafc;
  }
  .login-container input[type="password"] {
    width: 100%;
    padding: 10px 14px;
    border: 1px solid #334155;
    border-radius: 6px;
    background: #0f172a;
    color: #e2e8f0;
    font-size: 14px;
    margin-bottom: 16px;
    outline: none;
  }
  .login-container input[type="password"]:focus {
    border-color: #3b82f6;
  }
  .login-container button {
    width: 100%;
    padding: 10px;
    border: none;
    border-radius: 6px;
    background: #3b82f6;
    color: #fff;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
  }
  .login-container button:hover { background: #2563eb; }
  .login-error {
    color: #ef4444;
    font-size: 13px;
    text-align: center;
    margin-bottom: 12px;
  }
</style>
</head>
<body>
{% if not authenticated %}
<div class="login-container">
  <h1>Screen Recorder Dashboard</h1>
  {% if error %}
  <p class="login-error">{{ error }}</p>
  {% endif %}
  <form method="post" action="{{ url_for('login') }}">
    <input type="password" name="password" placeholder="Enter dashboard password" autofocus>
    <button type="submit">Sign In</button>
  </form>
</div>
{% else %}
<div class="header">
  <h1>Screen Recorder Dashboard</h1>
  <div class="header-right">
    <span>Auto-refresh: 30s</span>
    <span>|</span>
    <span>{{ now }}</span>
    <span>|</span>
    <a href="{{ url_for('logout') }}">Sign Out</a>
  </div>
</div>

<div class="summary">
  <div class="summary-card">
    <div class="label">Total Machines</div>
    <div class="value" style="color: #f8fafc;">{{ total }}</div>
  </div>
  <div class="summary-card">
    <div class="label">Active</div>
    <div class="value" style="color: #22c55e;">{{ active }}</div>
  </div>
  <div class="summary-card">
    <div class="label">Stale</div>
    <div class="value" style="color: #eab308;">{{ stale }}</div>
  </div>
  <div class="summary-card">
    <div class="label">Offline</div>
    <div class="value" style="color: #ef4444;">{{ offline }}</div>
  </div>
  <div class="summary-card">
    <div class="label">Total Segments</div>
    <div class="value" style="color: #f8fafc;">{{ total_segments }}</div>
  </div>
</div>

{% if groups %}
{% for client, machines in groups.items() %}
<div class="client-group">
  <h2>{{ client }} ({{ machines|length }})</h2>
  <table>
    <thead>
      <tr>
        <th style="width:120px;">Status</th>
        <th>Employee</th>
        <th>Computer</th>
        <th>Last Seen</th>
        <th style="width:100px;">Segments</th>
        <th style="width:100px;">Uptime</th>
      </tr>
    </thead>
    <tbody>
    {% for m in machines %}
      <tr>
        <td>
          <span class="status-dot" style="background:{{ m.status_color }};"></span>
          <span class="status-text">{{ m.status_label }}</span>
        </td>
        <td>{{ m.employee_name }}</td>
        <td class="mono">{{ m.computer_name }}</td>
        <td>{{ m.last_seen }}</td>
        <td class="mono">{{ m.segments_uploaded }}</td>
        <td class="mono">{{ m.uptime_hours }}h</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
</div>
{% endfor %}
{% else %}
<div class="empty-state">
  <h2>No heartbeat data found</h2>
  <p>Make sure recorders are running and the _heartbeats folder exists in Google Drive.</p>
</div>
{% endif %}
{% endif %}
</body>
</html>
"""

# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    """Login page with simple password authentication."""
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if secrets.compare_digest(password, _get_password()):
            session["authenticated"] = True
            return redirect(url_for("dashboard"))
        error = "Incorrect password."

    return render_template_string(
        DASHBOARD_HTML,
        authenticated=False,
        error=error,
    )


@app.route("/logout")
def logout():
    """Clear the session and redirect to login."""
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    """Main dashboard view."""
    heartbeats = _fetch_all_heartbeats()
    groups = _group_by_client(heartbeats)

    # Compute summary counts
    active = sum(1 for h in heartbeats if h.get("status_color") == "#22c55e")
    stale = sum(1 for h in heartbeats if h.get("status_color") == "#eab308")
    offline = sum(1 for h in heartbeats if h.get("status_color") == "#ef4444")
    total_segments = sum(h.get("segments_uploaded", 0) for h in heartbeats)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return render_template_string(
        DASHBOARD_HTML,
        authenticated=True,
        groups=groups,
        total=len(heartbeats),
        active=active,
        stale=stale,
        offline=offline,
        total_segments=total_segments,
        now=now_str,
    )


@app.route("/api/heartbeats")
@login_required
def api_heartbeats():
    """JSON API endpoint for programmatic access."""
    heartbeats = _fetch_all_heartbeats()
    return Response(
        json.dumps(heartbeats, indent=2),
        mimetype="application/json",
    )


# --------------------------------------------------------------------------
# CLI entry point
# --------------------------------------------------------------------------

def main():
    """Parse arguments, load config, and start the Flask development server."""
    parser = argparse.ArgumentParser(
        description="Screen Recorder Monitoring Dashboard"
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to the YAML configuration file (default: config.yaml)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to run the dashboard on (default: 8080)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind to (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Run in Flask debug mode",
    )
    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Load configuration
    global _app_config
    with open(args.config, "r", encoding="utf-8") as fh:
        _app_config = yaml.safe_load(fh) or {}

    # Initialize Google Drive service
    _init_drive_service(_app_config)

    # Set Flask secret key for sessions
    app.secret_key = _app_config.get(
        "dashboard_secret_key",
        os.environ.get("DASHBOARD_SECRET_KEY", secrets.token_hex(32)),
    )

    logger.info(
        "Starting dashboard on %s:%d (password: %s)",
        args.host,
        args.port,
        "****" + _get_password()[-4:] if len(_get_password()) > 4 else "****",
    )
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
