"""Google Sheets backend for the screen recording service.

Provides a centralized Google Sheets "dashboard" that tracks machine status,
recording history, and remote commands.  The sheet is created automatically
on first use and made publicly readable so it can serve as the data source
for a static HTML dashboard.

Tab layout
----------
Machines   : computer_name | employee_name | client_name | status |
             last_heartbeat | segments_uploaded | uptime_hours | installed_at
Recordings : timestamp | computer_name | employee_name | filename |
             drive_file_id | drive_link | size_mb
Commands   : timestamp | computer_name | command | status | executed_at
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

# Header rows for each tab
MACHINES_HEADERS = [
    "computer_name",
    "employee_name",
    "client_name",
    "status",
    "last_heartbeat",
    "segments_uploaded",
    "uptime_hours",
    "installed_at",
]

RECORDINGS_HEADERS = [
    "timestamp",
    "computer_name",
    "employee_name",
    "filename",
    "drive_file_id",
    "drive_link",
    "size_mb",
]

COMMANDS_HEADERS = [
    "timestamp",
    "computer_name",
    "command",
    "status",
    "executed_at",
]

# Tab names
TAB_MACHINES = "Machines"
TAB_RECORDINGS = "Recordings"
TAB_COMMANDS = "Commands"


class SheetsBackend:
    """Google Sheets integration for the screen recording service.

    Manages a single Google Sheet with three tabs (Machines, Recordings,
    Commands) that acts as a lightweight database for the monitoring
    dashboard.

    Args:
        config: The full application configuration dictionary.  Expected
            keys: ``google_drive.credentials_file``,
            ``google_sheets.sheet_id`` (optional -- auto-created if empty).
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        drive_cfg = config["google_drive"]
        credentials_file = drive_cfg["credentials_file"]

        sheets_cfg = config.get("google_sheets", {})
        self._sheet_id: Optional[str] = sheets_cfg.get("sheet_id") or None

        logger.info("Authenticating with Google Sheets service account.")
        try:
            creds = Credentials.from_service_account_file(
                credentials_file, scopes=SCOPES
            )
            self._sheets_service = build("sheets", "v4", credentials=creds)
            self._drive_service = build("drive", "v3", credentials=creds)
        except Exception:
            logger.exception("Failed to authenticate with Google APIs.")
            raise

    # ------------------------------------------------------------------
    # Sheet ID property
    # ------------------------------------------------------------------

    @property
    def sheet_id(self) -> Optional[str]:
        """Return the current Google Sheet ID (may be ``None`` before init)."""
        return self._sheet_id

    # ------------------------------------------------------------------
    # Initialization / auto-creation
    # ------------------------------------------------------------------

    def ensure_sheet(self) -> str:
        """Ensure the Google Sheet exists, creating it if necessary.

        If ``sheet_id`` was provided in the config and the sheet is
        accessible, it is reused.  Otherwise a brand-new sheet is created
        with the correct tab structure and made publicly readable.

        Returns:
            The Google Sheet ID (suitable for persisting back to config).
        """
        if self._sheet_id is not None:
            # Validate that the sheet is accessible
            try:
                self._sheets_service.spreadsheets().get(
                    spreadsheetId=self._sheet_id
                ).execute()
                logger.info(
                    "Using existing Google Sheet: %s", self._sheet_id
                )
                return self._sheet_id
            except HttpError:
                logger.warning(
                    "Configured sheet_id '%s' is not accessible; "
                    "creating a new sheet.",
                    self._sheet_id,
                )

        return self._create_sheet()

    def _create_sheet(self) -> str:
        """Create a new Google Sheet with the required tabs and headers.

        Returns:
            The newly created Google Sheet ID.
        """
        body: Dict[str, Any] = {
            "properties": {"title": "Screen Recording Dashboard"},
            "sheets": [
                {
                    "properties": {
                        "title": TAB_MACHINES,
                        "index": 0,
                    }
                },
                {
                    "properties": {
                        "title": TAB_RECORDINGS,
                        "index": 1,
                    }
                },
                {
                    "properties": {
                        "title": TAB_COMMANDS,
                        "index": 2,
                    }
                },
            ],
        }

        try:
            spreadsheet = (
                self._sheets_service.spreadsheets()
                .create(body=body, fields="spreadsheetId")
                .execute()
            )
            self._sheet_id = spreadsheet["spreadsheetId"]
            logger.info("Created Google Sheet: %s", self._sheet_id)
        except HttpError:
            logger.exception("Failed to create Google Sheet.")
            raise

        # Write header rows
        self._write_headers()

        # Make the sheet publicly readable
        self._make_public()

        return self._sheet_id

    def _write_headers(self) -> None:
        """Write header rows to all three tabs."""
        data = [
            {
                "range": f"{TAB_MACHINES}!A1",
                "values": [MACHINES_HEADERS],
            },
            {
                "range": f"{TAB_RECORDINGS}!A1",
                "values": [RECORDINGS_HEADERS],
            },
            {
                "range": f"{TAB_COMMANDS}!A1",
                "values": [COMMANDS_HEADERS],
            },
        ]
        try:
            self._sheets_service.spreadsheets().values().batchUpdate(
                spreadsheetId=self._sheet_id,
                body={
                    "valueInputOption": "RAW",
                    "data": data,
                },
            ).execute()
            logger.info("Header rows written to all tabs.")
        except HttpError:
            logger.exception("Failed to write header rows.")

    def _make_public(self) -> None:
        """Make the Google Sheet readable by anyone with the link."""
        permission = {"type": "anyone", "role": "reader"}
        try:
            self._drive_service.permissions().create(
                fileId=self._sheet_id,
                body=permission,
                fields="id",
            ).execute()
            logger.info(
                "Sheet %s is now publicly readable.", self._sheet_id
            )
        except HttpError:
            logger.exception(
                "Failed to set public read permission on sheet %s.",
                self._sheet_id,
            )

    # ------------------------------------------------------------------
    # Machines tab
    # ------------------------------------------------------------------

    def update_machine(
        self,
        computer_name: str,
        employee_name: str,
        client_name: str,
        status: str,
        segments_uploaded: int,
        uptime_hours: float,
    ) -> None:
        """Upsert a machine row in the Machines tab.

        If a row with the given ``computer_name`` already exists it is
        updated in-place; otherwise a new row is appended.

        Args:
            computer_name: Unique machine identifier.
            employee_name: Name of the employee using the machine.
            client_name: Client / practice the machine belongs to.
            status: Current status string (e.g. "recording", "stopped").
            segments_uploaded: Total number of segments uploaded so far.
            uptime_hours: Hours of uptime since the service started.
        """
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            result = (
                self._sheets_service.spreadsheets()
                .values()
                .get(
                    spreadsheetId=self._sheet_id,
                    range=f"{TAB_MACHINES}!A:A",
                )
                .execute()
            )
            values = result.get("values", [])
        except HttpError:
            logger.exception("Failed to read Machines tab.")
            return

        # Find existing row (skip header at index 0)
        row_index: Optional[int] = None
        for idx, row in enumerate(values):
            if idx == 0:
                continue
            if row and row[0] == computer_name:
                row_index = idx
                break

        if row_index is not None:
            # Update existing row -- preserve installed_at from existing data
            installed_at = ""
            try:
                existing = (
                    self._sheets_service.spreadsheets()
                    .values()
                    .get(
                        spreadsheetId=self._sheet_id,
                        range=f"{TAB_MACHINES}!A{row_index + 1}:H{row_index + 1}",
                    )
                    .execute()
                )
                existing_values = existing.get("values", [[]])
                if existing_values and len(existing_values[0]) >= 8:
                    installed_at = existing_values[0][7]
            except HttpError:
                logger.warning("Could not read existing installed_at value.")

            row_data = [
                computer_name,
                employee_name,
                client_name,
                status,
                now_iso,
                segments_uploaded,
                uptime_hours,
                installed_at,
            ]
            cell_range = f"{TAB_MACHINES}!A{row_index + 1}:H{row_index + 1}"
            try:
                self._sheets_service.spreadsheets().values().update(
                    spreadsheetId=self._sheet_id,
                    range=cell_range,
                    valueInputOption="RAW",
                    body={"values": [row_data]},
                ).execute()
                logger.debug(
                    "Updated machine row for '%s' at row %d.",
                    computer_name,
                    row_index + 1,
                )
            except HttpError:
                logger.exception(
                    "Failed to update machine row for '%s'.", computer_name
                )
        else:
            # Append new row
            row_data = [
                computer_name,
                employee_name,
                client_name,
                status,
                now_iso,
                segments_uploaded,
                uptime_hours,
                now_iso,  # installed_at = first heartbeat time
            ]
            try:
                self._sheets_service.spreadsheets().values().append(
                    spreadsheetId=self._sheet_id,
                    range=f"{TAB_MACHINES}!A:H",
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body={"values": [row_data]},
                ).execute()
                logger.info(
                    "Appended new machine row for '%s'.", computer_name
                )
            except HttpError:
                logger.exception(
                    "Failed to append machine row for '%s'.", computer_name
                )

    # ------------------------------------------------------------------
    # Recordings tab
    # ------------------------------------------------------------------

    def log_recording(
        self,
        computer_name: str,
        employee_name: str,
        filename: str,
        drive_file_id: str,
        size_mb: float,
    ) -> None:
        """Append a recording entry to the Recordings tab.

        Args:
            computer_name: Machine that produced the recording.
            employee_name: Employee associated with the machine.
            filename: Original filename of the recording.
            drive_file_id: Google Drive file ID of the uploaded recording.
            size_mb: File size in megabytes.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        drive_link = (
            f"https://drive.google.com/file/d/{drive_file_id}/view"
        )

        row_data = [
            now_iso,
            computer_name,
            employee_name,
            filename,
            drive_file_id,
            drive_link,
            round(size_mb, 2),
        ]

        try:
            self._sheets_service.spreadsheets().values().append(
                spreadsheetId=self._sheet_id,
                range=f"{TAB_RECORDINGS}!A:G",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [row_data]},
            ).execute()
            logger.debug(
                "Logged recording '%s' for '%s'.", filename, computer_name
            )
        except HttpError:
            logger.exception(
                "Failed to log recording '%s' for '%s'.",
                filename,
                computer_name,
            )

    # ------------------------------------------------------------------
    # Commands tab
    # ------------------------------------------------------------------

    def check_commands(self, computer_name: str) -> List[Dict[str, Any]]:
        """Check for pending commands for a specific machine.

        Reads the Commands tab and returns all rows where
        ``computer_name`` matches and ``status`` is ``"pending"``.

        Args:
            computer_name: The machine to check commands for.

        Returns:
            A list of dictionaries, each containing ``row_number``
            (1-indexed, suitable for :meth:`mark_command_executed`),
            ``timestamp``, ``command``, and ``status``.
        """
        try:
            result = (
                self._sheets_service.spreadsheets()
                .values()
                .get(
                    spreadsheetId=self._sheet_id,
                    range=f"{TAB_COMMANDS}!A:E",
                )
                .execute()
            )
            values = result.get("values", [])
        except HttpError:
            logger.exception("Failed to read Commands tab.")
            return []

        pending: List[Dict[str, Any]] = []
        for idx, row in enumerate(values):
            if idx == 0:
                # Skip header row
                continue
            # Ensure the row has enough columns
            if len(row) < 4:
                continue
            row_computer = row[1]
            row_status = row[3]
            if row_computer == computer_name and row_status == "pending":
                pending.append(
                    {
                        "row_number": idx + 1,  # 1-indexed sheet row
                        "timestamp": row[0],
                        "command": row[2],
                        "status": row_status,
                    }
                )

        logger.debug(
            "Found %d pending command(s) for '%s'.",
            len(pending),
            computer_name,
        )
        return pending

    def mark_command_executed(self, row_number: int) -> None:
        """Mark a command row as executed.

        Sets the ``status`` column to ``"executed"`` and writes the
        current UTC timestamp into the ``executed_at`` column.

        Args:
            row_number: The 1-indexed row number in the Commands tab
                (as returned by :meth:`check_commands`).
        """
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            self._sheets_service.spreadsheets().values().update(
                spreadsheetId=self._sheet_id,
                range=f"{TAB_COMMANDS}!D{row_number}:E{row_number}",
                valueInputOption="RAW",
                body={"values": [["executed", now_iso]]},
            ).execute()
            logger.info(
                "Marked command at row %d as executed.", row_number
            )
        except HttpError:
            logger.exception(
                "Failed to mark command at row %d as executed.",
                row_number,
            )
