"""
HIPAA compliance module with audit logging and consent tracking.

Provides a tamper-evident audit trail, consent management, and
compliance reporting for a screen recording system that may capture
Protected Health Information (PHI).
"""

import json
import logging
import os
import socket
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_HIPAA_NOTICE = """\
HIPAA COMPLIANCE NOTICE

This system records screen activity which may capture Protected Health \
Information (PHI). By authorizing this installation, you acknowledge:

1. All recordings are encrypted using AES-256-GCM encryption before \
storage or transmission.
2. Recordings are transmitted via encrypted HTTPS connections.
3. Local recordings are securely deleted after confirmed upload.
4. Access to recordings is restricted to authorized personnel only.
5. All file access and transfers are logged in a tamper-evident audit trail.
6. This system complies with HIPAA Security Rule requirements for \
electronic Protected Health Information (ePHI).

Authorization is required from the organization before installation."""

VALID_EVENT_TYPES = frozenset(
    {
        "recording_start",
        "recording_stop",
        "file_encrypted",
        "file_uploaded",
        "file_deleted",
        "analysis_performed",
        "consent_recorded",
        "error",
        "security_event",
    }
)


class ComplianceManager:
    """Manage HIPAA audit logging, consent records, and compliance reporting.

    Parameters
    ----------
    config : dict
        Configuration dictionary.  Recognised keys:

        * ``audit_log_path`` -- path to the audit log file
          (default: ``"audit.log"``).
        * ``consent_records_path`` -- path to the consent records JSON
          (default: ``"consent_records.json"``).
        * ``employee_name`` -- the employee whose machine is being recorded.
        * ``computer_name`` -- override for the hostname
          (default: ``socket.gethostname()``).
    """

    def __init__(self, config: dict) -> None:
        self._config = dict(config)

        # Resolve paths.
        self._audit_log_path = Path(
            self._config.get("audit_log_path", "audit.log")
        )
        self._consent_path = Path(
            self._config.get("consent_records_path", "consent_records.json")
        )

        self._employee_name: str = self._config.get("employee_name", "unknown")
        self._computer_name: str = self._config.get(
            "computer_name", socket.gethostname()
        )

        # Set up a dedicated audit logger that writes JSON lines.
        # Uses TimedRotatingFileHandler for 12-month retention with daily rotation.
        self._audit_logger = logging.getLogger("hipaa.audit")
        self._audit_logger.setLevel(logging.INFO)
        self._audit_logger.propagate = False

        # Avoid adding duplicate handlers if __init__ is called again with
        # the same log path (e.g. in tests).
        if not self._audit_logger.handlers:
            handler = TimedRotatingFileHandler(
                str(self._audit_log_path),
                when="midnight",
                backupCount=365,  # 12 months of daily logs
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._audit_logger.addHandler(handler)

        # Set restrictive file permissions on audit log (owner read/write only).
        self._set_secure_permissions(self._audit_log_path)

        # Ensure the consent records file exists with secure permissions.
        if not self._consent_path.exists():
            self._consent_path.write_text(
                json.dumps([], indent=2) + "\n", encoding="utf-8"
            )
        self._set_secure_permissions(self._consent_path)

        logger.info(
            "ComplianceManager initialised (audit_log=%s, consent_records=%s).",
            self._audit_log_path,
            self._consent_path,
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    @property
    def audit_log_path(self) -> Path:
        return self._audit_log_path

    @property
    def consent_records_path(self) -> Path:
        return self._consent_path

    # ------------------------------------------------------------------
    # Audit logging
    # ------------------------------------------------------------------
    def log_event(self, event_type: str, details: Optional[dict] = None) -> dict:
        """Write a JSON-line audit entry and return the record dict.

        Parameters
        ----------
        event_type:
            One of the recognised event type strings (e.g.
            ``"recording_start"``, ``"file_encrypted"``).
        details:
            Arbitrary dict with event-specific information.

        Returns
        -------
        dict
            The full audit record that was written.
        """
        if event_type not in VALID_EVENT_TYPES:
            logger.warning(
                "Unrecognised event_type '%s'; logging anyway.", event_type
            )

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "employee_name": self._employee_name,
            "computer_name": self._computer_name,
            "details": details or {},
        }

        self._audit_logger.info(json.dumps(record, default=str))
        logger.debug("Audit event logged: %s", event_type)
        return record

    # ------------------------------------------------------------------
    # Consent management
    # ------------------------------------------------------------------
    def record_consent(
        self,
        employee_name: str,
        consented_by: str,
        consent_text: str,
    ) -> dict:
        """Record that *employee_name* has authorised screen recording.

        Parameters
        ----------
        employee_name:
            The employee whose consent is being recorded.
        consented_by:
            The person (e.g. a manager or compliance officer) who
            authorised the recording.
        consent_text:
            The full text of the notice/agreement that was acknowledged.

        Returns
        -------
        dict
            The consent record that was persisted.
        """
        consent_record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "employee_name": employee_name,
            "consented_by": consented_by,
            "consent_text": consent_text,
        }

        # Append to the consent records file.
        records = self._load_consent_records()
        records.append(consent_record)
        self._save_consent_records(records)

        # Also write an audit event.
        self.log_event(
            "consent_recorded",
            {
                "employee_name": employee_name,
                "consented_by": consented_by,
            },
        )

        logger.info(
            "Consent recorded for employee '%s' by '%s'.",
            employee_name,
            consented_by,
        )
        return consent_record

    def check_consent(self, employee_name: str) -> bool:
        """Return ``True`` if consent has been recorded for *employee_name*."""
        records = self._load_consent_records()
        return any(r["employee_name"] == employee_name for r in records)

    # ------------------------------------------------------------------
    # HIPAA notice
    # ------------------------------------------------------------------
    @staticmethod
    def get_hipaa_notice() -> str:
        """Return the standard HIPAA compliance notice text."""
        return _HIPAA_NOTICE

    # ------------------------------------------------------------------
    # Compliance reporting
    # ------------------------------------------------------------------
    def generate_compliance_report(self) -> str:
        """Parse the audit log and return a human-readable compliance summary.

        The report includes:
        * Total recordings started / stopped
        * Total files encrypted and uploaded
        * Errors and security events
        * Consent status for all employees mentioned in the log
        """
        events = self._load_audit_events()
        consent_records = self._load_consent_records()

        # Aggregate counts.
        counts: dict[str, int] = {}
        employees_seen: set[str] = set()
        errors: list[dict] = []
        security_events: list[dict] = []

        for evt in events:
            etype = evt.get("event_type", "unknown")
            counts[etype] = counts.get(etype, 0) + 1
            emp = evt.get("employee_name")
            if emp:
                employees_seen.add(emp)
            if etype == "error":
                errors.append(evt)
            elif etype == "security_event":
                security_events.append(evt)

        consented_employees = {r["employee_name"] for r in consent_records}

        # Build the report.
        lines: list[str] = []
        lines.append("=" * 60)
        lines.append("HIPAA COMPLIANCE REPORT")
        lines.append(
            f"Generated: {datetime.now(timezone.utc).isoformat()}"
        )
        lines.append("=" * 60)
        lines.append("")

        lines.append("--- Event Summary ---")
        lines.append(
            f"  Recordings started : {counts.get('recording_start', 0)}"
        )
        lines.append(
            f"  Recordings stopped : {counts.get('recording_stop', 0)}"
        )
        lines.append(
            f"  Files encrypted    : {counts.get('file_encrypted', 0)}"
        )
        lines.append(
            f"  Files uploaded     : {counts.get('file_uploaded', 0)}"
        )
        lines.append(
            f"  Files deleted      : {counts.get('file_deleted', 0)}"
        )
        lines.append(
            f"  Analyses performed : {counts.get('analysis_performed', 0)}"
        )
        lines.append(
            f"  Consent events     : {counts.get('consent_recorded', 0)}"
        )
        lines.append(
            f"  Total audit events : {len(events)}"
        )
        lines.append("")

        lines.append("--- Errors & Security Events ---")
        if errors:
            for err in errors:
                ts = err.get("timestamp", "?")
                details = err.get("details", {})
                lines.append(f"  [ERROR] {ts}: {json.dumps(details, default=str)}")
        else:
            lines.append("  No errors recorded.")
        if security_events:
            for sev in security_events:
                ts = sev.get("timestamp", "?")
                details = sev.get("details", {})
                lines.append(
                    f"  [SECURITY] {ts}: {json.dumps(details, default=str)}"
                )
        else:
            lines.append("  No security events recorded.")
        lines.append("")

        lines.append("--- Consent Status ---")
        all_employees = employees_seen | consented_employees
        if all_employees:
            for emp in sorted(all_employees):
                status = (
                    "CONSENTED" if emp in consented_employees else "NO CONSENT"
                )
                lines.append(f"  {emp}: {status}")
        else:
            lines.append("  No employees recorded.")
        lines.append("")

        lines.append("=" * 60)
        lines.append("END OF REPORT")
        lines.append("=" * 60)

        report = "\n".join(lines)
        logger.info("Compliance report generated (%d audit events).", len(events))
        return report

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _set_secure_permissions(path: Path) -> None:
        """Set file permissions to owner-read/write only (0600)."""
        try:
            if path.exists():
                os.chmod(path, 0o600)
        except OSError as exc:
            logger.warning("Could not set permissions on %s: %s", path, exc)

    def _load_consent_records(self) -> list[dict]:
        """Load the consent records JSON file, returning a list of dicts."""
        try:
            text = self._consent_path.read_text(encoding="utf-8").strip()
            if not text:
                return []
            records = json.loads(text)
            if not isinstance(records, list):
                logger.error(
                    "Consent records file is malformed (expected a JSON array)."
                )
                return []
            return records
        except FileNotFoundError:
            return []
        except json.JSONDecodeError as exc:
            logger.error("Failed to parse consent records: %s", exc)
            return []

    def _save_consent_records(self, records: list[dict]) -> None:
        """Persist the consent records list to disk."""
        self._consent_path.write_text(
            json.dumps(records, indent=2, default=str) + "\n",
            encoding="utf-8",
        )

    def _load_audit_events(self) -> list[dict]:
        """Parse the JSON-lines audit log and return a list of event dicts."""
        events: list[dict] = []
        try:
            with open(self._audit_log_path, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning(
                            "Skipping malformed audit log line %d.", lineno
                        )
        except FileNotFoundError:
            logger.warning("Audit log file not found: %s", self._audit_log_path)
        return events
