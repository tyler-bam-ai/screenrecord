"""Configuration manager for the screenrecord application.

Loads configuration from a YAML file, auto-detects system defaults,
validates required fields, and provides sensible defaults for optional ones.
"""

import getpass
import logging
import os
import socket
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG: Dict[str, Any] = {
    "employee_name": "",
    "computer_name": "",
    "client_name": "",
    "recording": {
        "fps": 5,
        "crf": 28,
        "segment_duration": 3600,
        "output_dir": "recordings",
        "audio_device": "",
    },
    "google_drive": {
        "credentials_file": "credentials.json",
        "root_folder_id": "",
    },
    "encryption": {
        "key_file": "",
    },
    "analysis": {
        "enabled": False,
        "gemini_api_key": "",
        "xai_api_key": "",
        "openrouter_api_key": "",
    },
    "rag": {
        "enabled": False,
        "db_path": "rag_db",
        "synthesis_interval": 3600,
        "bible_path": "company_operations_bible.md",
    },
}


class ConfigError(Exception):
    """Raised when configuration is invalid or cannot be loaded."""


class ConfigManager:
    """Manages application configuration loaded from a YAML file.

    Handles loading, validation, auto-detection of system-specific values,
    and merging with default settings.
    """

    def __init__(self, config_path: Optional[str] = None) -> None:
        """Initialize the configuration manager.

        Args:
            config_path: Path to the YAML configuration file. Defaults to
                ``config.yaml`` in the current working directory.
        """
        self.config_path = Path(config_path) if config_path else Path("config.yaml")
        self._config: Dict[str, Any] = {}

    @property
    def config(self) -> Dict[str, Any]:
        """Return the current configuration dictionary."""
        return self._config

    # -- public helpers for common lookups ----------------------------------

    @property
    def employee_name(self) -> str:
        return self._config.get("employee_name", "")

    @property
    def computer_name(self) -> str:
        return self._config.get("computer_name", "")

    @property
    def recording(self) -> Dict[str, Any]:
        return self._config.get("recording", {})

    @property
    def google_drive(self) -> Dict[str, Any]:
        return self._config.get("google_drive", {})

    @property
    def analysis(self) -> Dict[str, Any]:
        return self._config.get("analysis", {})

    @property
    def rag(self) -> Dict[str, Any]:
        return self._config.get("rag", {})

    # -- core lifecycle -----------------------------------------------------

    def load(self) -> Dict[str, Any]:
        """Load configuration from the YAML file, apply defaults, and validate.

        Returns:
            The fully-resolved configuration dictionary.

        Raises:
            ConfigError: If the file cannot be read/parsed or validation fails.
        """
        raw = self._read_yaml()
        merged = self._merge_defaults(raw)
        self._auto_detect(merged)
        self._validate(merged)
        self._config = merged
        logger.info(
            "Configuration loaded successfully (employee=%s, computer=%s)",
            merged["employee_name"],
            merged["computer_name"],
        )
        return self._config

    def save(self) -> None:
        """Persist the current in-memory configuration back to the YAML file."""
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as fh:
                yaml.dump(
                    self._config,
                    fh,
                    default_flow_style=False,
                    sort_keys=False,
                    allow_unicode=True,
                )
            logger.info("Configuration saved to %s", self.config_path)
        except OSError as exc:
            raise ConfigError(f"Failed to save configuration: {exc}") from exc

    def create_default_config(self) -> None:
        """Write a default configuration file if one does not already exist."""
        if self.config_path.exists():
            logger.debug("Config file already exists at %s; skipping creation.", self.config_path)
            return
        self._config = dict(DEFAULT_CONFIG)
        self._auto_detect(self._config)
        self.save()
        logger.info("Default configuration created at %s", self.config_path)

    # -- internal helpers ---------------------------------------------------

    def _read_yaml(self) -> Dict[str, Any]:
        """Read and parse the YAML configuration file.

        Returns:
            A dictionary of raw configuration values (may be empty/partial).

        Raises:
            ConfigError: If the file is missing or cannot be parsed.
        """
        if not self.config_path.exists():
            raise ConfigError(
                f"Configuration file not found: {self.config_path}. "
                "Run with --init to create a default configuration."
            )
        try:
            with open(self.config_path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise ConfigError(f"Failed to parse YAML configuration: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"Failed to read configuration file: {exc}") from exc

        if data is None:
            logger.warning("Configuration file is empty; using defaults.")
            return {}
        if not isinstance(data, dict):
            raise ConfigError("Configuration file must contain a YAML mapping at the top level.")
        return data

    @staticmethod
    def _merge_defaults(raw: Dict[str, Any]) -> Dict[str, Any]:
        """Deep-merge *raw* values on top of ``DEFAULT_CONFIG``.

        Keys present in *raw* override the defaults. Nested dictionaries are
        merged recursively so that partial overrides work as expected.
        """

        def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
            merged: Dict[str, Any] = {}
            for key in set(base) | set(override):
                base_val = base.get(key)
                over_val = override.get(key)
                if key in override and key in base:
                    if isinstance(base_val, dict) and isinstance(over_val, dict):
                        merged[key] = _deep_merge(base_val, over_val)
                    else:
                        merged[key] = over_val
                elif key in override:
                    merged[key] = over_val
                else:
                    # Deep-copy dicts so mutations don't bleed into DEFAULT_CONFIG.
                    merged[key] = dict(base_val) if isinstance(base_val, dict) else base_val
            return merged

        return _deep_merge(DEFAULT_CONFIG, raw)

    @staticmethod
    def _auto_detect(cfg: Dict[str, Any]) -> None:
        """Fill in ``computer_name`` and ``employee_name`` when not provided."""
        if not cfg.get("computer_name"):
            cfg["computer_name"] = socket.gethostname()
            logger.debug("Auto-detected computer_name: %s", cfg["computer_name"])

        if not cfg.get("employee_name"):
            cfg["employee_name"] = getpass.getuser()
            logger.debug("Auto-detected employee_name: %s", cfg["employee_name"])

    @staticmethod
    def _validate(cfg: Dict[str, Any]) -> None:
        """Validate that the configuration contains all required fields.

        Raises:
            ConfigError: If a required field is missing or has an invalid value.
        """
        errors: list[str] = []

        # Top-level identity fields (auto-detect should have filled these).
        if not cfg.get("employee_name"):
            errors.append("employee_name is required but could not be determined.")
        if not cfg.get("computer_name"):
            errors.append("computer_name is required but could not be determined.")

        # Recording section.
        rec = cfg.get("recording", {})
        if not isinstance(rec, dict):
            errors.append("recording section must be a mapping.")
        else:
            fps = rec.get("fps", 0)
            if not isinstance(fps, (int, float)) or fps <= 0:
                errors.append(f"recording.fps must be a positive number, got {fps!r}.")
            crf = rec.get("crf", -1)
            if not isinstance(crf, int) or not (0 <= crf <= 51):
                errors.append(f"recording.crf must be an integer between 0 and 51, got {crf!r}.")
            seg = rec.get("segment_duration", 0)
            if not isinstance(seg, (int, float)) or seg <= 0:
                errors.append(f"recording.segment_duration must be positive, got {seg!r}.")
            if not rec.get("output_dir"):
                errors.append("recording.output_dir must be a non-empty string.")

        # Google Drive section.
        gd = cfg.get("google_drive", {})
        if not isinstance(gd, dict):
            errors.append("google_drive section must be a mapping.")
        else:
            if not gd.get("credentials_file"):
                errors.append("google_drive.credentials_file must be a non-empty string.")

        # RAG section.
        rag = cfg.get("rag", {})
        if isinstance(rag, dict) and rag.get("enabled"):
            if not rag.get("db_path"):
                errors.append("rag.db_path must be set when RAG is enabled.")
            synthesis = rag.get("synthesis_interval", 0)
            if not isinstance(synthesis, (int, float)) or synthesis <= 0:
                errors.append(
                    f"rag.synthesis_interval must be positive, got {synthesis!r}."
                )

        if errors:
            combined = "\n  - ".join(errors)
            raise ConfigError(f"Configuration validation failed:\n  - {combined}")

    # -- convenience --------------------------------------------------------

    def get(self, dotted_key: str, default: Any = None) -> Any:
        """Retrieve a nested value using dot-separated keys.

        Example::

            manager.get("recording.fps")  # returns 5

        Args:
            dotted_key: Dot-delimited path into the configuration tree.
            default: Value returned when the key does not exist.
        """
        parts = dotted_key.split(".")
        node: Any = self._config
        for part in parts:
            if isinstance(node, dict):
                node = node.get(part)
            else:
                return default
            if node is None:
                return default
        return node

    def __repr__(self) -> str:
        return f"<ConfigManager path={self.config_path!r} loaded={bool(self._config)}>"


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Load and return configuration from a YAML file.

    This is a convenience wrapper around :class:`ConfigManager`.
    """
    manager = ConfigManager(config_path)
    return manager.load()


def validate_config(config: Dict[str, Any]) -> None:
    """Validate an already-loaded configuration dictionary.

    Raises:
        ConfigError: If validation fails.
    """
    ConfigManager._validate(config)
