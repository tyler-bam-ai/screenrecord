"""Small helpers for safe Google Drive queries and tenant-scoped folders."""

from __future__ import annotations

from typing import Any, Mapping


def drive_query_literal(value: Any) -> str:
    """Return a safely quoted Drive query string literal."""
    text = str(value)
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def scoped_folder_id(drive_cfg: Mapping[str, Any], specific_key: str) -> str:
    """Pick the best folder scope for a Drive artifact family.

    ``specific_key`` lets deployments send diagnostics or heartbeats to a
    separate pre-provisioned folder. If it is unset, a hardened deployment's
    upload folder becomes the tenant boundary. Legacy deployments fall back to
    the shared root folder.
    """
    return (
        str(drive_cfg.get(specific_key) or "").strip()
        or str(drive_cfg.get("upload_folder_id") or "").strip()
        or str(drive_cfg.get("root_folder_id") or "").strip()
    )
