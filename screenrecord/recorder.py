"""
Screen recording module using FFmpeg with timer-based segmentation.

Instead of using FFmpeg's segment muxer (which has compatibility issues
with macOS AVFoundation), each segment is a separate FFmpeg invocation
with ``-t <duration>``.  When FFmpeg exits after the time limit, the
completed file is enqueued and a new instance is started for the next
segment.
"""

import logging
import platform
import queue
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from screenrecord import platform_utils

logger = logging.getLogger(__name__)

# Minimum free disk space (in bytes) before we stop recording.
MIN_DISK_SPACE_BYTES = 500 * 1024 * 1024  # 500 MB


class ScreenRecorder:
    """Records the screen using FFmpeg, one file per segment.

    Each segment is recorded by a separate FFmpeg invocation limited to
    ``segment_duration`` seconds via ``-t``.  When the process exits
    (duration reached), the completed file is placed onto
    ``completed_queue`` and a new FFmpeg instance is started automatically.

    Args:
        config: Application configuration dictionary.
    """

    def __init__(self, config: dict) -> None:
        rec = config.get("recording", {})

        self._fps: int = rec.get("fps", 5)
        self._crf: int = rec.get("crf", 28)
        self._segment_duration: int = rec.get("segment_duration", 3600)
        self._output_dir: Path = Path(rec.get("output_dir", "recordings")).resolve()
        self._audio_device: str = rec.get("audio_device", "") or ""

        self._employee_name: str = config.get("employee_name", "unknown")
        self._computer_name: str = config.get("computer_name", "unknown")

        self._config: dict = config

        # FFmpeg subprocess handle.
        self._process: subprocess.Popen | None = None
        self._current_output: Path | None = None

        # Threading.
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._manager_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None

        # Completed segment queue for downstream processing.
        self.completed_queue: queue.Queue[Path] = queue.Queue()

        # Bookkeeping.
        self._segments_completed: int = 0
        self._is_recording: bool = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def segments_completed(self) -> int:
        return self._segments_completed

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start recording the screen."""
        if self._is_recording:
            logger.warning("Recording is already in progress.")
            return

        self._stop_event.clear()

        self._output_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Output directory: %s", self._output_dir)

        if not self._check_disk_space():
            raise RuntimeError(
                f"Insufficient disk space in {self._output_dir}. "
                f"Need at least {MIN_DISK_SPACE_BYTES / (1024 * 1024):.0f} MB free."
            )

        self._is_recording = True

        # The manager thread handles the record-enqueue-restart loop.
        self._manager_thread = threading.Thread(
            target=self._recording_loop,
            name="recording-manager",
            daemon=True,
        )
        self._manager_thread.start()

        logger.info("Screen recording started.")

    def stop(self) -> None:
        """Stop recording and finalize any in-progress segment."""
        if not self._is_recording:
            logger.warning("Recording is not in progress.")
            return

        logger.info("Stopping screen recording...")
        self._stop_event.set()

        self._terminate_ffmpeg()

        if self._manager_thread is not None and self._manager_thread.is_alive():
            self._manager_thread.join(timeout=15)

        if self._stderr_thread is not None and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=5)

        # Enqueue the final partial segment if it has content.
        self._enqueue_current_if_valid()

        self._is_recording = False
        logger.info(
            "Screen recording stopped. Total segments: %d",
            self._segments_completed,
        )

    def get_completed_segment(self, timeout: float = 5.0) -> Path | None:
        """Return the next completed segment path, or ``None``."""
        if self._stop_event.is_set() and self.completed_queue.empty():
            return None
        try:
            return self.completed_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    # ------------------------------------------------------------------
    # Recording loop
    # ------------------------------------------------------------------

    def _recording_loop(self) -> None:
        """Main loop: launch FFmpeg, wait for it to finish, enqueue, repeat."""
        while not self._stop_event.is_set():
            if not self._check_disk_space():
                logger.error("Insufficient disk space. Pausing for 60s.")
                self._stop_event.wait(timeout=60)
                continue

            output_path = self._generate_output_path()
            self._current_output = output_path

            try:
                self._launch_ffmpeg(output_path)
            except Exception:
                logger.exception("Failed to launch FFmpeg. Retrying in 10s.")
                self._stop_event.wait(timeout=10)
                continue

            # Wait for FFmpeg to exit (either duration reached or error).
            proc = self._process
            while proc is not None and proc.poll() is None:
                if self._stop_event.is_set():
                    self._terminate_ffmpeg()
                    break
                time.sleep(1)

            # Wait for stderr thread to finish reading.
            if self._stderr_thread is not None and self._stderr_thread.is_alive():
                self._stderr_thread.join(timeout=5)

            if self._stop_event.is_set():
                break

            # FFmpeg exited normally (duration reached) — enqueue segment.
            retcode = proc.returncode if proc else -1
            if retcode == 0:
                self._enqueue_current_if_valid()
            else:
                logger.error("FFmpeg exited with code %d.", retcode)
                # Still try to enqueue if file has content.
                self._enqueue_current_if_valid()
                # Brief pause before retry.
                self._stop_event.wait(timeout=5)

    # ------------------------------------------------------------------
    # FFmpeg lifecycle
    # ------------------------------------------------------------------

    def _generate_output_path(self) -> Path:
        """Generate a timestamped output file path."""
        safe_computer = self._sanitize(self._computer_name)
        safe_employee = self._sanitize(self._employee_name)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"{safe_computer}_{safe_employee}_{timestamp}.mp4"
        return self._output_dir / filename

    @staticmethod
    def _sanitize(name: str) -> str:
        return "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)

    def _launch_ffmpeg(self, output_path: Path) -> None:
        """Build and start the FFmpeg subprocess for one segment."""
        rec = self._config.get("recording", {})

        cmd = platform_utils.build_ffmpeg_command(
            recording_config=rec,
            computer_name=self._computer_name,
            employee_name=self._employee_name,
            output_path=str(output_path),
        )
        logger.info("FFmpeg command: %s", " ".join(cmd))

        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            raise RuntimeError(
                "FFmpeg executable not found. Please install FFmpeg."
            )

        self._stderr_thread = threading.Thread(
            target=self._read_stderr,
            name="ffmpeg-stderr",
            daemon=True,
        )
        self._stderr_thread.start()

    def _terminate_ffmpeg(self) -> None:
        """Gracefully terminate the FFmpeg process."""
        if self._process is None:
            return

        with self._lock:
            proc = self._process

        if proc.poll() is not None:
            return

        try:
            if platform.system() == "Windows":
                proc.terminate()
            else:
                proc.send_signal(signal.SIGTERM)
        except OSError as exc:
            logger.warning("Error sending termination signal to FFmpeg: %s", exc)

        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            logger.warning("FFmpeg did not exit in time; killing.")
            proc.kill()
            proc.wait(timeout=5)

        logger.debug("FFmpeg exited with code %d.", proc.returncode)

    def _enqueue_current_if_valid(self) -> None:
        """Enqueue the current output file if it exists and has content."""
        path = self._current_output
        if path is None:
            return

        if path.exists() and path.stat().st_size > 0:
            self._segments_completed += 1
            self.completed_queue.put(path)
            logger.info(
                "Segment #%d completed: %s (%.2f MB)",
                self._segments_completed,
                path.name,
                path.stat().st_size / (1024 * 1024),
            )
        else:
            logger.warning("Segment file missing or empty: %s", path)

        self._current_output = None

    def _read_stderr(self) -> None:
        """Consume FFmpeg stderr to prevent pipe blocking."""
        proc = self._process
        if proc is None or proc.stderr is None:
            return

        try:
            for raw_line in proc.stderr:
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue

                lower = line.lower()
                if any(kw in lower for kw in ("error", "fatal", "failed", "no space left")):
                    logger.error("FFmpeg: %s", line)
                    if "no space left" in lower:
                        logger.critical("Disk full detected. Stopping recording.")
                        self._stop_event.set()
                elif "warning" in lower:
                    logger.warning("FFmpeg: %s", line)
                else:
                    logger.debug("FFmpeg: %s", line)
        except Exception:
            if not self._stop_event.is_set():
                logger.exception("Error reading FFmpeg stderr.")

    # ------------------------------------------------------------------
    # Disk space check
    # ------------------------------------------------------------------

    def _check_disk_space(self) -> bool:
        try:
            usage = shutil.disk_usage(self._output_dir)
            if usage.free < MIN_DISK_SPACE_BYTES:
                logger.warning(
                    "Low disk space: %.1f MB free in %s.",
                    usage.free / (1024 * 1024),
                    self._output_dir,
                )
                return False
            return True
        except OSError:
            logger.warning("Could not check disk space; proceeding anyway.")
            return True
