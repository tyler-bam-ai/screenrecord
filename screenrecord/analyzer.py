"""Video analyzer module for the screenrecord application.

Analyzes recorded screen capture videos using Google Gemini and xAI Grok,
producing exhaustive text descriptions of everything observed in the recording.
Both models are run independently; if one fails the other still proceeds.
Results are written to a paired .txt file alongside the source video.
"""

import base64
import logging
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class VideoAnalysisError(Exception):
    """Raised when video analysis encounters an unrecoverable error."""


class VideoAnalyzer:
    """Analyzes screen recording videos using Gemini and Grok models.

    Accepts a configuration dictionary that must contain an ``analysis`` key
    with API keys and an ``enabled`` flag.
    """

    ANALYSIS_PROMPT = """\
You are analyzing a screen recording of an employee working at their computer. \
Provide an EXHAUSTIVE, DETAILED analysis of everything that happens in this recording.

For every moment in the video, describe:
1. APPLICATIONS: What applications/programs are open and being used. Name the \
specific app (e.g., "Google Chrome", "Microsoft Excel", "Slack").
2. ACTIONS: Every click, keystroke pattern, menu selection, and navigation action taken.
3. SCREEN CONTENT: What is visible on screen - toolbars, menus, dialog boxes, \
web pages, documents.
4. WORKFLOW: The sequence of tasks being performed and how they connect.
5. URLs/WEBSITES: Any visible URLs, website names, or web applications being accessed.
6. DOCUMENTS: Any documents, spreadsheets, or files being worked on - include \
visible names/titles.
7. COMMUNICATION: Any emails, chats, or messages being read or composed.
8. TIME SPENT: Approximate time spent on each activity or application.

After the detailed chronological analysis, provide:

## WORKFLOW SUMMARY
- List the main tasks/activities performed in order
- Estimate percentage of time spent on each task
- Note any patterns or recurring activities

## TOOLS & APPLICATIONS USED
- List every application/tool observed with how it was used

## JOB FUNCTION ANALYSIS
- Based on what you observed, describe what this person's job role appears to involve
- What skills are they demonstrating?
- What processes do they follow?

Be as specific and detailed as possible. Include EVERYTHING you can observe."""

    # Retry / back-off settings for API rate limits.
    _MAX_RETRIES = 3
    _INITIAL_BACKOFF_SECONDS = 5

    def __init__(self, config: Dict[str, Any]) -> None:
        analysis_cfg = config.get("analysis", {})
        self._enabled: bool = bool(analysis_cfg.get("enabled", False))
        self._gemini_api_key: str = analysis_cfg.get("gemini_api_key", "")
        self._xai_api_key: str = analysis_cfg.get("xai_api_key", "")
        self._openrouter_api_key: str = analysis_cfg.get("openrouter_api_key", "")

        if self._enabled:
            if not self._gemini_api_key and not self._xai_api_key:
                logger.warning(
                    "Analysis is enabled but neither gemini_api_key nor "
                    "xai_api_key is configured. Analysis calls will fail."
                )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze_video(
        self,
        video_path: str,
        drive_file_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Analyze a video file using Gemini and Grok.

        Args:
            video_path: Path to the video file on disk.
            drive_file_id: Optional Google Drive file ID (reserved for future
                use, e.g. passing a Drive link into the analysis context).

        Returns:
            A dictionary with the following keys:
                - ``gemini_analysis``: The Gemini model response text (or error string).
                - ``grok_analysis``: The Grok model response text (or error string).
                - ``combined``: The merged analysis text written to the output file.
                - ``text_file``: Path to the generated ``.txt`` sidecar file.
        """
        video = Path(video_path)
        if not video.is_file():
            raise VideoAnalysisError(f"Video file does not exist: {video}")

        if not self._enabled:
            logger.info("Analysis is disabled in configuration; skipping.")
            return {
                "gemini_analysis": "",
                "grok_analysis": "",
                "combined": "",
                "text_file": "",
            }

        logger.info("Starting analysis for %s", video)

        # Run both models independently -- failures are isolated.
        gemini_result = self._run_gemini(video)
        grok_result = self._run_grok(video)

        combined = self._build_combined_text(gemini_result, grok_result, video)

        # Write the sidecar text file next to the video.
        text_file = video.with_suffix(".txt")
        try:
            text_file.write_text(combined, encoding="utf-8")
            logger.info("Analysis written to %s", text_file)
        except OSError:
            logger.exception("Failed to write analysis text file %s", text_file)

        return {
            "gemini_analysis": gemini_result,
            "grok_analysis": grok_result,
            "combined": combined,
            "text_file": str(text_file),
        }

    # ------------------------------------------------------------------
    # Gemini
    # ------------------------------------------------------------------

    def _run_gemini(self, video: Path) -> str:
        """Wrapper that handles retries and error isolation for Gemini."""
        if not self._gemini_api_key:
            msg = "[Gemini] Skipped -- no API key configured."
            logger.warning(msg)
            return msg

        backoff = self._INITIAL_BACKOFF_SECONDS
        last_error: Optional[Exception] = None

        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                return self._analyze_with_gemini(video)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.error(
                    "[Gemini] Attempt %d/%d failed: %s",
                    attempt,
                    self._MAX_RETRIES,
                    exc,
                )
                if attempt < self._MAX_RETRIES:
                    logger.info("[Gemini] Retrying in %d seconds ...", backoff)
                    time.sleep(backoff)
                    backoff *= 2

        return f"[Gemini] Analysis failed after {self._MAX_RETRIES} attempts: {last_error}"

    def _analyze_with_gemini(self, video_path: Path) -> str:
        """Upload a video to Gemini and request a detailed analysis.

        Uses the ``google-genai`` library to upload the file, wait for it
        to become ``ACTIVE``, and then generate content with the analysis
        prompt.

        Args:
            video_path: Path to the video file.

        Returns:
            The model's text response.
        """
        from google import genai  # type: ignore[import-untyped]

        logger.info("[Gemini] Uploading video %s ...", video_path)
        client = genai.Client(api_key=self._gemini_api_key)

        # Upload the video file.
        video_file = client.files.upload(file=str(video_path))
        logger.info(
            "[Gemini] Upload complete (name=%s). Waiting for processing ...",
            getattr(video_file, "name", "unknown"),
        )

        # Poll until the file is ready (state == ACTIVE).
        poll_interval = 10  # seconds
        max_wait = 600  # 10 minutes
        elapsed = 0
        while True:
            state = getattr(video_file, "state", None)
            # The google-genai library exposes state as a string or enum.
            state_str = str(state).upper() if state is not None else ""
            if "ACTIVE" in state_str:
                break
            if "FAILED" in state_str:
                raise VideoAnalysisError(
                    f"[Gemini] Video processing failed (state={state_str})"
                )
            if elapsed >= max_wait:
                raise VideoAnalysisError(
                    f"[Gemini] Timed out waiting for video processing after {elapsed}s"
                )
            time.sleep(poll_interval)
            elapsed += poll_interval
            # Refresh file metadata.
            video_file = client.files.get(name=video_file.name)
            logger.debug(
                "[Gemini] File state: %s (waited %ds)", state_str, elapsed
            )

        logger.info("[Gemini] Video processed. Generating analysis ...")

        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=[video_file, self.ANALYSIS_PROMPT],
        )

        text = response.text if hasattr(response, "text") else str(response)
        logger.info("[Gemini] Analysis complete (%d characters).", len(text))
        return text

    # ------------------------------------------------------------------
    # Grok (via xAI OpenAI-compatible endpoint)
    # ------------------------------------------------------------------

    def _run_grok(self, video: Path) -> str:
        """Wrapper that handles retries and error isolation for Grok."""
        if not self._xai_api_key:
            msg = "[Grok] Skipped -- no API key configured."
            logger.warning(msg)
            return msg

        backoff = self._INITIAL_BACKOFF_SECONDS
        last_error: Optional[Exception] = None

        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                return self._analyze_with_grok(video)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.error(
                    "[Grok] Attempt %d/%d failed: %s",
                    attempt,
                    self._MAX_RETRIES,
                    exc,
                )
                if attempt < self._MAX_RETRIES:
                    logger.info("[Grok] Retrying in %d seconds ...", backoff)
                    time.sleep(backoff)
                    backoff *= 2

        return f"[Grok] Analysis failed after {self._MAX_RETRIES} attempts: {last_error}"

    def _analyze_with_grok(self, video_path: Path) -> str:
        """Extract key frames from the video and send them to Grok for analysis.

        Since xAI's OpenAI-compatible API does not support direct video
        upload, we extract one frame every 30 seconds via FFmpeg, encode
        each frame as base64, and send them as image content parts.

        Args:
            video_path: Path to the video file.

        Returns:
            The model's text response.
        """
        import openai  # type: ignore[import-untyped]

        logger.info("[Grok] Extracting frames from %s ...", video_path)
        frames = self._extract_frames(video_path, interval=30)

        if not frames:
            raise VideoAnalysisError(
                "[Grok] No frames extracted from video -- is FFmpeg installed?"
            )

        logger.info("[Grok] Extracted %d frames. Building request ...", len(frames))

        try:
            # Build multimodal message content list.
            content_parts: List[Dict[str, Any]] = []

            # Add the text prompt first.
            content_parts.append({"type": "text", "text": self.ANALYSIS_PROMPT})

            # Add each frame as a base64-encoded image.
            for frame_path in frames:
                frame_data = Path(frame_path).read_bytes()
                b64 = base64.b64encode(frame_data).decode("utf-8")
                content_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{b64}",
                        },
                    }
                )

            client = openai.OpenAI(
                api_key=self._xai_api_key,
                base_url="https://api.x.ai/v1",
            )

            response = client.chat.completions.create(
                model="grok-4-1-fast-reasoning",
                messages=[
                    {
                        "role": "user",
                        "content": content_parts,
                    }
                ],
                max_tokens=16384,
            )

            text = response.choices[0].message.content or ""
            logger.info("[Grok] Analysis complete (%d characters).", len(text))
            return text

        finally:
            # Clean up extracted frame files.
            self._cleanup_frames(frames)

    # ------------------------------------------------------------------
    # Frame extraction helpers
    # ------------------------------------------------------------------

    def _extract_frames(
        self, video_path: Path, interval: int = 30
    ) -> List[str]:
        """Extract key frames from a video at a fixed interval using FFmpeg.

        Args:
            video_path: Path to the source video file.
            interval: Seconds between extracted frames (default 30).

        Returns:
            A sorted list of absolute paths to the extracted JPEG frames.
        """
        tmp_dir = tempfile.mkdtemp(prefix="screenrecord_frames_")
        output_pattern = str(Path(tmp_dir) / "frame_%04d.jpg")

        cmd = [
            "ffmpeg",
            "-i", str(video_path),
            "-vf", f"fps=1/{interval}",
            "-q:v", "2",
            output_pattern,
        ]

        logger.debug("[Frames] Running: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout
            )
            if result.returncode != 0:
                logger.error(
                    "[Frames] FFmpeg failed (rc=%d): %s",
                    result.returncode,
                    result.stderr[:500],
                )
                return []
        except FileNotFoundError:
            logger.error(
                "[Frames] FFmpeg not found. Please install FFmpeg to enable "
                "Grok video analysis."
            )
            return []
        except subprocess.TimeoutExpired:
            logger.error("[Frames] FFmpeg timed out after 300 seconds.")
            return []

        frames = sorted(Path(tmp_dir).glob("frame_*.jpg"))
        frame_paths = [str(f) for f in frames]
        logger.info("[Frames] Extracted %d frames to %s", len(frame_paths), tmp_dir)
        return frame_paths

    @staticmethod
    def _cleanup_frames(frame_paths: List[str]) -> None:
        """Remove extracted frame files and their parent temp directory."""
        if not frame_paths:
            return

        parent: Optional[Path] = None
        for fp in frame_paths:
            p = Path(fp)
            if parent is None:
                parent = p.parent
            try:
                p.unlink(missing_ok=True)
            except OSError:
                logger.debug("Failed to delete frame %s", fp)

        # Remove the temp directory itself.
        if parent is not None:
            try:
                parent.rmdir()
                logger.debug("[Frames] Cleaned up temp directory %s", parent)
            except OSError:
                logger.debug(
                    "[Frames] Could not remove temp directory %s (may not be empty)",
                    parent,
                )

    # ------------------------------------------------------------------
    # Output formatting
    # ------------------------------------------------------------------

    @staticmethod
    def _build_combined_text(
        gemini_result: str,
        grok_result: str,
        video_path: Path,
    ) -> str:
        """Merge results from both models into a single analysis document."""
        sections: List[str] = []

        header = (
            f"VIDEO ANALYSIS REPORT\n"
            f"{'=' * 60}\n"
            f"Source: {video_path.name}\n"
            f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"{'=' * 60}\n"
        )
        sections.append(header)

        # Check if both models actually failed.
        gemini_failed = gemini_result.startswith("[Gemini]")
        grok_failed = grok_result.startswith("[Grok]")

        if gemini_failed and grok_failed:
            sections.append(
                "ERROR: Both analysis models failed.\n\n"
                f"Gemini: {gemini_result}\n\n"
                f"Grok: {grok_result}\n"
            )
        else:
            if not gemini_failed:
                sections.append(
                    f"\n{'=' * 60}\n"
                    f"GEMINI ANALYSIS (gemini-3-flash-preview)\n"
                    f"{'=' * 60}\n\n"
                    f"{gemini_result}\n"
                )
            else:
                sections.append(
                    f"\n[Note: Gemini analysis unavailable -- {gemini_result}]\n"
                )

            if not grok_failed:
                sections.append(
                    f"\n{'=' * 60}\n"
                    f"GROK ANALYSIS (grok-4-1-fast-reasoning)\n"
                    f"{'=' * 60}\n\n"
                    f"{grok_result}\n"
                )
            else:
                sections.append(
                    f"\n[Note: Grok analysis unavailable -- {grok_result}]\n"
                )

        return "\n".join(sections)
