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
from pathlib import Path
from typing import Any, Dict, Optional


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

    def start(self):
        """Initialize all components and start the recording pipeline."""
        employee_name = self.config.get("employee_name", "Unknown")
        computer_name = self.config.get("computer_name", "Unknown")
        logger.info(
            "Starting ScreenRecordService for %s on %s",
            employee_name,
            computer_name,
        )

        # Import and initialize components
        from .compliance import ComplianceManager
        from .encryption import FileEncryptor
        from .heartbeat import HeartbeatSender
        from .recorder import ScreenRecorder
        from .updater import UpdateChecker
        from .uploader import DriveUploader

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
        self.compliance.log_event("recording_start", {
            "employee_name": employee_name,
            "computer_name": computer_name,
        })

        # Verify screen recording permission before starting
        from . import platform_utils
        if not platform_utils.check_screen_recording_permission():
            msg = (
                "\n"
                "========================================================\n"
                "  FATAL: Screen recording permission is NOT granted.\n"
                "\n"
                "  Go to: System Settings → Privacy & Security\n"
                "         → Screen Recording → Enable python3 / Terminal\n"
                "\n"
                "  Then restart the service:\n"
                "    launchctl unload ~/Library/LaunchAgents/com.screenrecord.service.plist\n"
                "    launchctl load ~/Library/LaunchAgents/com.screenrecord.service.plist\n"
                "========================================================"
            )
            logger.critical(msg)
            print(msg, flush=True)
            raise RuntimeError("Screen recording permission not granted. See above for instructions.")

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

        # Start update checker thread (checks hourly)
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
        """Poll Google Sheets for restart commands and update machine status every 30 seconds."""
        poll_interval = 30
        computer_name = self.config.get("computer_name", "Unknown")
        employee_name = self.config.get("employee_name", "Unknown")
        client_name = self.config.get("client_name", "Unknown")
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
                self.sheets_backend.update_machine(
                    computer_name=computer_name,
                    employee_name=employee_name,
                    client_name=client_name,
                    status="recording",
                    segments_uploaded=segments,
                    uptime_hours=round(uptime, 2),
                )
                commands = self.sheets_backend.check_commands(computer_name)
                for cmd in commands:
                    if cmd["command"] in ("restart", "stop"):
                        logger.info(
                            "Received '%s' command from dashboard (row %d).",
                            cmd["command"],
                            cmd["row_number"],
                        )
                        self.sheets_backend.mark_command_executed(cmd["row_number"])
                        if cmd["command"] == "restart":
                            logger.info("Restarting service per remote command...")
                            self.stop()
                            os.execv(
                                sys.executable,
                                [sys.executable] + sys.argv,
                            )
                        elif cmd["command"] == "stop":
                            logger.info("Stopping service per remote command...")
                            self.stop()
            except Exception:
                logger.exception("Error polling for commands")

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
        self.stop_event.set()

        if self.compliance:
            self.compliance.log_event("recording_stop", {
                "employee_name": self.config.get("employee_name", "unknown"),
                "computer_name": self.config.get("computer_name", "unknown"),
            })

        # Stop the recorder so no new segments are produced
        if self.recorder is not None:
            try:
                self.recorder.stop()
            except Exception:
                logger.exception("Error stopping recorder")

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
