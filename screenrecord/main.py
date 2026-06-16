"""Main orchestrator for the screen recording service.

Coordinates the recorder, uploader, analyzer, and RAG system components
into a unified pipeline that captures screen segments, uploads them to
Google Drive, analyzes their content, and indexes results for retrieval.
"""

import logging
import os
import queue
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


# Commands older than this are ignored on pickup, so a machine that was
# offline for a long time does not replay an ancient stop/start on boot.
COMMAND_MAX_AGE_SECONDS = 24 * 60 * 60


logger = logging.getLogger(__name__)


class ScreenRecordService:
    """Top-level service that coordinates all screen recording components."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self._setup_logging()

        self.stop_event = threading.Event()
        self.analysis_queue: queue.Queue = queue.Queue()

        # Components initialized lazily in start()
        self.recorder = None
        self.uploader = None
        self.analyzer = None
        self.rag_system = None
        self.encryptor = None
        self.compliance = None
        self.heartbeat = None
        self.update_checker = None
        self.sheets_backend = None

        # Pipeline thread
        self._upload_thread: Optional[threading.Thread] = None
        self._update_thread: Optional[threading.Thread] = None
        self._command_thread: Optional[threading.Thread] = None

        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _setup_logging(self):
        """Configure logging with both file and console handlers."""
        log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        log_level = getattr(
            logging,
            self.config.get("logging", {}).get("level", "INFO").upper(),
            logging.INFO,
        )

        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)

        # Avoid adding duplicate handlers on repeated calls
        if not root_logger.handlers:
            # Console handler
            console_handler = logging.StreamHandler()
            console_handler.setLevel(log_level)
            console_handler.setFormatter(logging.Formatter(log_format))
            root_logger.addHandler(console_handler)

            # File handler
            file_handler = logging.FileHandler("screenrecord.log")
            file_handler.setLevel(log_level)
            file_handler.setFormatter(logging.Formatter(log_format))
            root_logger.addHandler(file_handler)

    @staticmethod
    def _paused_flag_path() -> Path:
        """Return the path of the paused flag file."""
        return Path.home() / ".screenrecord" / ".paused"

    @staticmethod
    def _is_paused() -> bool:
        return ScreenRecordService._paused_flag_path().exists()

    @staticmethod
    def _set_paused(paused: bool) -> None:
        flag = ScreenRecordService._paused_flag_path()
        if paused:
            flag.touch()
        elif flag.exists():
            flag.unlink()

    @staticmethod
    def _command_is_stale(timestamp: Optional[str]) -> bool:
        """Return True if a queued command is too old to act on.

        Commands carry an ISO-8601 timestamp (UTC) written by the dashboard.
        A machine that was offline for days should not replay an ancient
        stop/start when it finally comes back online. If the timestamp can't
        be parsed we treat the command as fresh (fail open).
        """
        if not timestamp:
            return False
        try:
            ts = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        except ValueError:
            return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age > COMMAND_MAX_AGE_SECONDS

    def start(self):
        """Initialize all components and start the recording pipeline."""
        employee_name = self.config.get("employee_name", "Unknown")
        computer_name = self.config.get("computer_name", "Unknown")
        paused = self._is_paused()
        logger.info(
            "Starting ScreenRecordService for %s on %s (paused=%s)",
            employee_name, computer_name, paused,
        )

        # Import and initialize components
        from .compliance import ComplianceManager
        from .encryption import FileEncryptor
        from .heartbeat import HeartbeatSender
        from .recorder import ScreenRecorder
        from .updater import UpdateChecker
        from .uploader import DriveUploader

        if not paused:
            self.recorder = ScreenRecorder(self.config)
        self.uploader = DriveUploader(self.config)

        analysis_cfg = self.config.get("analysis", {})
        if analysis_cfg.get("enabled", False):
            try:
                from .analyzer import VideoAnalyzer
                self.analyzer = VideoAnalyzer(self.config)
            except ImportError:
                logger.warning("Analysis dependencies not installed; disabling analysis")
                self.analyzer = None
        else:
            self.analyzer = None
            logger.info("Video analysis disabled")

        rag_config = self.config.get("rag", {})
        if rag_config.get("enabled", False):
            try:
                from .rag_system import RAGSystem
                self.rag_system = RAGSystem(self.config)
            except ImportError:
                logger.warning("RAG dependencies not installed; disabling RAG")
                self.rag_system = None
        else:
            self.rag_system = None
            logger.info("RAG system disabled")

        # Initialize encryption if a key file is configured
        encryption_cfg = self.config.get("encryption", {})
        key_file = encryption_cfg.get("key_file", "")
        if key_file and os.path.isfile(key_file):
            self.encryptor = FileEncryptor.load_key(key_file)
            logger.info("Encryption enabled (key loaded from %s)", key_file)
        else:
            self.encryptor = None
            logger.info("Encryption disabled (no key_file configured)")

        # Initialize HIPAA compliance manager
        self.compliance = ComplianceManager(self.config)
        self.compliance.log_event("recording_start" if not paused else "security_event", {
            "employee_name": employee_name,
            "computer_name": computer_name,
            "mode": "paused" if paused else "recording",
        })

        if not paused:
            # Verify screen recording permission before starting
            from . import platform_utils
            if not platform_utils.check_screen_recording_permission():
                # Pop the user straight to the Screen Recording toggle.
                if sys.platform == "darwin":
                    try:
                        import subprocess
                        subprocess.run(
                            ["open",
                             "x-apple.systempreferences:com.apple.preference.security"
                             "?Privacy_ScreenCapture"],
                            check=False,
                        )
                    except Exception:
                        pass
                msg = (
                    "\n"
                    "========================================================\n"
                    "  FATAL: Screen recording permission is NOT granted.\n"
                    "\n"
                    "  A System Settings window was opened to the right place.\n"
                    "  Turn ON the switch next to python3 / Terminal.\n"
                    "\n"
                    "  (If it didn't open: System Settings → Privacy & Security\n"
                    "   → Screen Recording → enable python3 / Terminal.)\n"
                    "\n"
                    "  Then restart the service:\n"
                    "    launchctl unload ~/Library/LaunchAgents/com.screenrecord.service.plist\n"
                    "    launchctl load ~/Library/LaunchAgents/com.screenrecord.service.plist\n"
                    "========================================================"
                )
                logger.critical(msg)
                print(msg, flush=True)
                raise RuntimeError("Screen recording permission not granted.")

            # Start the screen recorder
            self.recorder.start()
            logger.info("Screen recorder started")

            # Start the processing pipeline thread (handles upload + analysis)
            self._upload_thread = threading.Thread(
                target=self._processing_pipeline,
                name="processing-pipeline",
                daemon=True,
            )
            self._upload_thread.start()
        else:
            logger.info("Service starting in PAUSED mode — recording is suspended.")

        # Start heartbeat sender
        try:
            self.heartbeat = HeartbeatSender(self.config)
            self.heartbeat.start()
            logger.info("Heartbeat sender started")
        except Exception:
            logger.exception("Failed to start heartbeat sender; continuing without it")
            self.heartbeat = None

        # Initialize Google Sheets backend (for dashboard)
        try:
            from .sheets_backend import SheetsBackend
            self.sheets_backend = SheetsBackend(self.config)
            sheet_id = self.sheets_backend.ensure_sheet()
            logger.info("Google Sheets backend ready (sheet_id: %s)", sheet_id)
        except Exception:
            logger.exception("Failed to initialize Sheets backend; continuing without it")
            self.sheets_backend = None

        # Start command polling thread (checks for restart commands)
        if self.sheets_backend is not None:
            self._command_thread = threading.Thread(
                target=self._command_poll_loop,
                name="command-poller",
                daemon=True,
            )
            self._command_thread.start()
            logger.info("Command poller started")

        # Start periodic RAG synthesis if enabled
        if self.rag_system is not None:
            self.rag_system.start_periodic_synthesis()
            logger.info("RAG periodic synthesis started")

        # Start update checker thread (checks hourly). The bundled/signed .app must
        # NEVER self-update from git: it would try to overwrite its own read-only
        # signed bundle (breaking the signature and the Screen Recording grant).
        # Bundled builds ship updates as new notarized .pkgs via MDM instead. Skip
        # the updater when frozen (PyInstaller bundle) or when explicitly disabled
        # via SCREENRECORD_DISABLE_UPDATER (set by app_entry for the bundle).
        _disable = os.environ.get("SCREENRECORD_DISABLE_UPDATER", "")
        if getattr(sys, "frozen", False) or _disable not in ("", "0", "false", "False"):
            logger.info("Auto-updater disabled (bundled/frozen build or env override)")
            self.update_checker = None
        else:
            try:
                self.update_checker = UpdateChecker(self.config)
                self._update_thread = threading.Thread(
                    target=self._update_check_loop,
                    name="update-checker",
                    daemon=True,
                )
                self._update_thread.start()
                logger.info("Update checker started")
            except Exception:
                logger.exception("Failed to start update checker; continuing without it")
                self.update_checker = None

        logger.info("All pipelines running. Waiting for shutdown signal...")
        self.stop_event.wait()

    def _update_check_loop(self):
        """Check for updates on startup, then every hour. Auto-restart on update."""
        update_interval = 3600  # 1 hour

        # Check immediately on startup (short delay to let service stabilize)
        self.stop_event.wait(timeout=30)

        while not self.stop_event.is_set():
            if self.stop_event.is_set():
                break
            try:
                if self.update_checker and self.update_checker.check_and_apply():
                    logger.info("Update applied. Restarting service...")
                    if self.heartbeat:
                        self.heartbeat.set_status("updating - restarting")
                    # Gracefully stop, then restart via exec
                    self.stop()
                    os.execv(sys.executable, [sys.executable] + sys.argv)
            except Exception:
                logger.exception("Error during update check")
            self.stop_event.wait(timeout=update_interval)

    def _command_poll_loop(self):
        """Poll Google Sheets for commands and update machine status every 30 seconds."""
        poll_interval = 30
        computer_name = self.config.get("computer_name", "Unknown")
        employee_name = self.config.get("employee_name", "Unknown")
        client_name = self.config.get("client_name", "Unknown")
        paused = self._is_paused()
        while not self.stop_event.is_set():
            self.stop_event.wait(timeout=poll_interval)
            if self.stop_event.is_set():
                break
            try:
                if self.sheets_backend is None:
                    continue
                # Update machine status in Sheets
                segments = self.heartbeat.segments_uploaded if self.heartbeat else 0
                uptime = self.heartbeat.uptime_hours if self.heartbeat else 0
                current_status = "paused" if paused else "recording"
                self.sheets_backend.update_machine(
                    computer_name=computer_name,
                    employee_name=employee_name,
                    client_name=client_name,
                    status=current_status,
                    segments_uploaded=segments,
                    uptime_hours=round(uptime, 2),
                )
                commands = self.sheets_backend.check_commands(computer_name)
                for cmd in commands:
                    command = cmd["command"]
                    if command not in ("restart", "stop", "start", "record_test"):
                        continue
                    if self._command_is_stale(cmd.get("timestamp")):
                        logger.warning(
                            "Ignoring stale '%s' command (row %d, queued %s).",
                            command, cmd["row_number"], cmd.get("timestamp"),
                        )
                        self.sheets_backend.mark_command_executed(cmd["row_number"])
                        continue
                    logger.info(
                        "Received '%s' command from dashboard (row %d).",
                        command, cmd["row_number"],
                    )
                    self.sheets_backend.mark_command_executed(cmd["row_number"])
                    if command == "restart":
                        logger.info("Restarting service per remote command...")
                        self._set_paused(False)
                        self.stop()
                        os.execv(sys.executable, [sys.executable] + sys.argv)
                    elif command == "stop":
                        logger.info("Pausing service per remote command...")
                        self._set_paused(True)
                        self.stop()
                    elif command == "start":
                        logger.info("Starting recording per remote command...")
                        self._set_paused(False)
                        self.stop()
                        os.execv(sys.executable, [sys.executable] + sys.argv)
                    elif command == "record_test":
                        logger.info("Recording a 60s test clip per remote command...")
                        try:
                            self._record_test_clip(duration=60)
                        except Exception:
                            logger.exception("record_test: failed to capture clip")
            except Exception:
                logger.exception("Error polling for commands")

    def _record_test_clip(self, duration: int = 60) -> None:
        """Capture a single short clip on demand and push it through the pipeline.

        Triggered by the dashboard's "record_test" command. Lets an operator
        verify the full capture -> encrypt -> upload -> dashboard path on any
        machine, remotely, without changing that machine's configured segment
        length or disturbing the main recording loop.

        Implementation: spin up a throwaway ScreenRecorder limited to *duration*
        seconds, grab the first completed segment, and hand it to the existing
        ``_process_segment`` (which encrypts, uploads, and logs to the dashboard
        using the already-initialized uploader/encryptor/sheets backend). Any
        extra partial segments from the throwaway recorder are discarded.

        Intended to run while the agent is paused/idle; if the main recorder is
        already capturing, a concurrent screen capture may fail on macOS, so the
        recommended flow is to pause the agent first.
        """
        import copy
        from . import platform_utils
        from .recorder import ScreenRecorder

        if not platform_utils.check_screen_recording_permission():
            logger.error(
                "record_test: screen recording permission not granted; skipping."
            )
            return

        test_cfg = copy.deepcopy(self.config)
        test_cfg.setdefault("recording", {})["segment_duration"] = duration
        test_recorder = ScreenRecorder(test_cfg)

        logger.info("record_test: capturing a %ds clip...", duration)
        test_recorder.start()

        # Wait for the first full segment (duration + headroom for encode/flush).
        clip: Optional[str] = None
        deadline = duration + 30
        waited = 0.0
        while waited < deadline:
            seg = test_recorder.get_completed_segment(timeout=2.0)
            if seg is not None:
                clip = str(seg)
                break
            waited += 2.0

        test_recorder.stop()

        # Discard any extra partial segments the throwaway recorder produced.
        while True:
            extra = test_recorder.get_completed_segment(timeout=0.5)
            if extra is None:
                break
            try:
                os.remove(str(extra))
            except OSError:
                pass

        if clip is None:
            logger.error("record_test: no clip was produced.")
            return

        logger.info("record_test: clip ready (%s); processing.", clip)
        self._process_segment(clip, analysis_enabled=False)
        logger.info("record_test: done.")

    def _processing_pipeline(self):
        """Process completed segments: analyze -> encrypt -> upload -> index -> cleanup.

        Runs in a dedicated thread. For each completed segment from the recorder:
        1. Analyze the raw video (while still unencrypted) with Gemini/Grok
        2. Encrypt the video if encryption is enabled
        3. Upload the (encrypted) video to Google Drive
        4. Upload the analysis text file to Google Drive
        5. Index the analysis in the RAG system
        6. Clean up local files
        """
        logger.info("Processing pipeline started")
        analysis_config = self.config.get("analysis", {})
        analysis_enabled = analysis_config.get("enabled", True)

        while not self.stop_event.is_set():
            try:
                segment_path = self.recorder.get_completed_segment(timeout=2.0)
            except Exception:
                continue

            if segment_path is None:
                continue

            self._process_segment(str(segment_path), analysis_enabled)

        # Drain any remaining segments after stop signal
        logger.info("Processing pipeline draining remaining segments")
        while True:
            try:
                segment_path = self.recorder.get_completed_segment(timeout=0.5)
            except Exception:
                break
            if segment_path is None:
                break
            self._process_segment(str(segment_path), analysis_enabled)

        logger.info("Processing pipeline stopped")

    def _process_segment(self, segment_path: str, analysis_enabled: bool):
        """Process a single segment through the full pipeline."""
        filename = os.path.basename(segment_path)
        logger.info("Processing segment: %s", filename)

        analysis_result = None
        text_file = ""

        try:
            # Step 1: Analyze the raw (unencrypted) video
            if analysis_enabled:
                logger.info("Analyzing segment: %s", filename)
                try:
                    analysis_result = self.analyzer.analyze_video(
                        segment_path, None
                    )
                    text_file = (
                        analysis_result.get("text_file", "")
                        if analysis_result
                        else ""
                    )
                    if self.compliance:
                        self.compliance.log_event("analysis_performed", {
                            "filename": filename,
                        })
                except Exception:
                    logger.exception(
                        "Analysis failed for %s; continuing with upload",
                        filename,
                    )

            # Step 2: Encrypt the video before uploading
            upload_path = segment_path
            if self.encryptor is not None:
                logger.info("Encrypting segment: %s", filename)
                enc_path = self.encryptor.encrypt_in_place(segment_path)
                upload_path = str(enc_path)
                if self.compliance:
                    self.compliance.log_event("file_encrypted", {
                        "filename": os.path.basename(upload_path),
                    })
                logger.info("Encryption complete: %s", os.path.basename(upload_path))

            # Step 3: Upload the (encrypted) video to Google Drive
            upload_filename = os.path.basename(upload_path)
            drive_file_id = None
            try:
                drive_file_id = self.uploader.upload_with_retry(
                    upload_path, delete_after=False
                )
                if drive_file_id:
                    logger.info(
                        "Upload successful: %s -> %s",
                        upload_filename,
                        drive_file_id,
                    )
                    if self.compliance:
                        self.compliance.log_event("file_uploaded", {
                            "filename": upload_filename,
                            "drive_file_id": drive_file_id,
                        })
                    if self.heartbeat:
                        self.heartbeat.increment_segments()
                    # Log recording to Google Sheets dashboard
                    if self.sheets_backend:
                        try:
                            size_mb = os.path.getsize(upload_path) / (1024 * 1024)
                            self.sheets_backend.log_recording(
                                computer_name=self.config.get("computer_name", "Unknown"),
                                employee_name=self.config.get("employee_name", "Unknown"),
                                filename=upload_filename,
                                drive_file_id=drive_file_id,
                                size_mb=size_mb,
                            )
                        except Exception:
                            logger.exception("Failed to log recording to Sheets")
                else:
                    logger.error(
                        "Upload returned no file ID for %s; keeping local file",
                        upload_filename,
                    )
            except Exception:
                logger.exception(
                    "Failed to upload segment %s; keeping local file",
                    upload_filename,
                )

            # Step 4: Upload the analysis text file to Drive
            if text_file:
                try:
                    self.uploader.upload_file(text_file)
                    logger.info("Analysis text uploaded for %s", filename)
                except Exception:
                    logger.exception(
                        "Failed to upload analysis text for %s", filename
                    )

            # Step 5: Index analysis in the RAG system
            combined_text = (
                analysis_result.get("combined", "")
                if analysis_result
                else ""
            )
            if combined_text:
                try:
                    metadata = {
                        "employee_name": self.config.get(
                            "employee_name", "unknown"
                        ),
                        "computer_name": self.config.get(
                            "computer_name", "unknown"
                        ),
                        "video_date": (
                            filename.split("_")[-2] if "_" in filename else ""
                        ),
                        "video_filename": filename,
                    }
                    self.rag_system.index_analysis(combined_text, metadata)
                except Exception:
                    logger.exception(
                        "Failed to index analysis for %s", filename
                    )

            # Step 6: Clean up local files
            for local_file in [upload_path, text_file]:
                if local_file and os.path.exists(local_file):
                    try:
                        os.remove(local_file)
                        if self.compliance:
                            self.compliance.log_event("file_deleted", {
                                "filename": os.path.basename(local_file),
                            })
                    except OSError:
                        logger.exception(
                            "Failed to delete local file %s", local_file
                        )

            logger.info(
                "Segment fully processed and cleaned up: %s", filename
            )

        except Exception:
            logger.exception("Error processing segment %s", filename)

    def stop(self):
        """Gracefully shut down all components and wait for pipelines to finish."""
        logger.info("Stopping ScreenRecordService...")

        if self.compliance:
            self.compliance.log_event("recording_stop", {
                "employee_name": self.config.get("employee_name", "unknown"),
                "computer_name": self.config.get("computer_name", "unknown"),
            })

        # Stop the recorder FIRST. recorder.stop() enqueues the final in-progress
        # segment, and we must let the processing pipeline drain it before we tell
        # the pipeline to exit. Previously stop_event was set here first, so the
        # pipeline saw the stop signal and drained an empty queue, exiting before
        # the last segment was enqueued — silently dropping the final recording.
        if self.recorder is not None:
            try:
                self.recorder.stop()
            except Exception:
                logger.exception("Error stopping recorder")

        # The final segment is now queued; signal the pipeline to finish draining.
        self.stop_event.set()

        # Stop heartbeat sender
        if self.heartbeat is not None:
            try:
                self.heartbeat.stop()
            except Exception:
                logger.exception("Error stopping heartbeat sender")

        # Stop RAG periodic synthesis
        if self.rag_system is not None:
            try:
                self.rag_system.stop()
            except Exception:
                logger.exception("Error stopping RAG system")

        # Wait for the processing pipeline thread to finish
        timeout = 30.0
        if self._upload_thread is not None and self._upload_thread.is_alive():
            self._upload_thread.join(timeout=timeout)
            if self._upload_thread.is_alive():
                logger.warning("Processing pipeline thread did not exit in time")

        logger.info("Service stopped gracefully")

    def _signal_handler(self, sig, frame):
        """Handle SIGTERM/SIGINT for graceful shutdown."""
        sig_name = signal.Signals(sig).name
        logger.info("Received shutdown signal: %s", sig_name)
        self.stop()
