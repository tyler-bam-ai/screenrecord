"""
RAG (Retrieval-Augmented Generation) system for video analysis storage and synthesis.

Manages a ChromaDB database of all video analyses and uses Claude Opus 4.5
via OpenRouter to synthesize comprehensive employee profiles and an operations bible.
"""

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

try:
    import chromadb
except ImportError:
    chromadb = None
    logger.error(
        "chromadb is not installed. Install it with: pip install chromadb"
    )


class RAGSystem:
    """Manages a RAG database of video analyses and synthesizes employee profiles."""

    SYNTHESIS_PROMPT = """You are analyzing observation data from screen recordings of an employee at work. Based on all the following observation reports, create a COMPREHENSIVE profile of how this employee does their job.

Structure your analysis as follows:

### Role Summary
What this person's role appears to be and their primary responsibilities.

### Daily Workflow
The typical sequence of activities they perform, including how they start their day, transition between tasks, and patterns in their work.

### Tools & Applications
Every application, website, and tool they use, with details on HOW they use each one and for what purpose.

### Key Processes
Step-by-step descriptions of the main business processes they execute. Be specific about the order of operations, what they click, what information they reference, etc.

### Skills Demonstrated
Technical skills, soft skills, domain knowledge, and proficiencies observed.

### Efficiency Notes
Any observations about their work efficiency, common patterns, time allocation, and potential areas for process improvement.

Be extremely detailed and specific. This document should allow someone else to understand and replicate this person's job functions.

OBSERVATION DATA:
"""

    BIBLE_CROSS_TEAM_PROMPT = """You are analyzing observation profiles of multiple employees at a company. Based on the following employee profiles, identify cross-team patterns, dependencies between roles, shared processes, and overall organizational observations.

Focus on:
- How different roles interact and depend on each other
- Shared tools and processes across the organization
- Communication patterns between team members
- Potential bottlenecks or single points of failure
- Overall organizational workflow patterns

EMPLOYEE PROFILES:
"""

    def __init__(self, config: dict[str, Any]) -> None:
        if chromadb is None:
            raise ImportError(
                "chromadb is required for the RAG system. "
                "Install it with: pip install chromadb"
            )

        rag_config = config.get("rag", {})
        self.db_path = Path(rag_config.get("db_path", "data/rag_db"))
        self.synthesis_interval = rag_config.get("synthesis_interval", 3600)
        self.bible_path = Path(
            rag_config.get("bible_path", "data/operations_bible.md")
        )
        self.enabled = rag_config.get("enabled", True)
        self.api_key = config.get("analysis", {}).get("openrouter_api_key", "")

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.bible_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info("Initializing ChromaDB persistent client at %s", self.db_path)
        self._client = chromadb.PersistentClient(path=str(self.db_path))
        self._collection = self._client.get_or_create_collection(
            name="video_analyses"
        )
        logger.info(
            "ChromaDB collection 'video_analyses' ready with %d existing documents",
            self._collection.count(),
        )

        self._stop_event = threading.Event()
        self._synthesis_thread: Optional[threading.Thread] = None
        self.last_synthesis_time: Optional[float] = None

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def index_analysis(
        self,
        analysis_text: str,
        metadata: dict[str, str],
    ) -> None:
        """Index an analysis by splitting it into chunks and adding to ChromaDB.

        Args:
            analysis_text: The full analysis text to index.
            metadata: Dict with keys employee_name, computer_name,
                      video_date, video_filename.
        """
        video_filename = metadata.get("video_filename", "unknown")
        chunks = self._split_into_chunks(analysis_text)

        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[dict[str, str]] = []

        for idx, chunk in enumerate(chunks):
            chunk_id = f"{video_filename}_{idx}"
            ids.append(chunk_id)
            documents.append(chunk)
            metadatas.append(
                {
                    "employee_name": metadata.get("employee_name", "unknown"),
                    "computer_name": metadata.get("computer_name", "unknown"),
                    "video_date": metadata.get("video_date", "unknown"),
                    "video_filename": video_filename,
                    "chunk_index": str(idx),
                }
            )

        if ids:
            self._collection.upsert(
                ids=ids,
                documents=documents,
                metadatas=metadatas,
            )
            logger.info(
                "Indexed %d chunks for video '%s' (employee: %s)",
                len(ids),
                video_filename,
                metadata.get("employee_name", "unknown"),
            )

    @staticmethod
    def _split_into_chunks(text: str, target_size: int = 1000) -> list[str]:
        """Split text into chunks of approximately target_size characters.

        Splits on paragraph boundaries (double newlines) to keep logical
        groupings intact.
        """
        paragraphs = text.split("\n\n")
        chunks: list[str] = []
        current_chunk = ""

        for paragraph in paragraphs:
            paragraph = paragraph.strip()
            if not paragraph:
                continue

            if current_chunk and len(current_chunk) + len(paragraph) + 2 > target_size:
                chunks.append(current_chunk.strip())
                current_chunk = paragraph
            else:
                if current_chunk:
                    current_chunk += "\n\n" + paragraph
                else:
                    current_chunk = paragraph

        if current_chunk.strip():
            chunks.append(current_chunk.strip())

        # If the text had no paragraph breaks, fall back to a single chunk
        if not chunks and text.strip():
            chunks.append(text.strip())

        return chunks

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------

    def query(
        self,
        query_text: str,
        employee_name: Optional[str] = None,
        n_results: int = 10,
    ) -> dict[str, Any]:
        """Query the ChromaDB collection.

        Args:
            query_text: The query string for semantic search.
            employee_name: If provided, filter results to this employee.
            n_results: Maximum number of results to return.

        Returns:
            Dict with 'documents' and 'metadatas' keys from ChromaDB results.
        """
        query_kwargs: dict[str, Any] = {
            "query_texts": [query_text],
            "n_results": min(n_results, self._collection.count() or 1),
        }

        if employee_name:
            query_kwargs["where"] = {"employee_name": employee_name}

        if self._collection.count() == 0:
            logger.warning("Query on empty collection; returning empty results")
            return {"documents": [[]], "metadatas": [[]]}

        results = self._collection.query(**query_kwargs)
        logger.info(
            "Query '%s' returned %d results (employee filter: %s)",
            query_text[:60],
            len(results.get("documents", [[]])[0]),
            employee_name or "none",
        )
        return results

    def get_all_employees(self) -> list[str]:
        """Return a sorted list of all unique employee names in the collection."""
        if self._collection.count() == 0:
            return []

        all_data = self._collection.get(include=["metadatas"])
        employee_names: set[str] = set()
        for meta in all_data.get("metadatas", []):
            if meta and "employee_name" in meta:
                employee_names.add(meta["employee_name"])

        employees = sorted(employee_names)
        logger.info("Found %d unique employees in the collection", len(employees))
        return employees

    # ------------------------------------------------------------------
    # Synthesis via OpenRouter (Claude Opus 4.5)
    # ------------------------------------------------------------------

    def synthesize_employee_profile(self, employee_name: str) -> str:
        """Synthesize a comprehensive profile for an employee using Claude Opus 4.5.

        Queries all analyses for the employee, compiles them, and sends
        to Claude Opus 4.5 via OpenRouter for synthesis.

        Args:
            employee_name: The employee to profile.

        Returns:
            The synthesized profile text.
        """
        logger.info("Synthesizing profile for employee: %s", employee_name)

        results = self.query(
            query_text="complete work analysis",
            employee_name=employee_name,
            n_results=100,
        )

        documents = results.get("documents", [[]])[0]
        if not documents:
            logger.warning("No analysis data found for employee: %s", employee_name)
            return f"No observation data available for {employee_name}."

        compiled_text = "\n\n---\n\n".join(documents)
        prompt = self.SYNTHESIS_PROMPT + compiled_text

        profile = self._call_openrouter(prompt)
        logger.info(
            "Synthesized profile for %s (%d characters)", employee_name, len(profile)
        )
        return profile

    def _call_openrouter(self, prompt: str) -> str:
        """Make an API call to Claude Opus 4.5 via OpenRouter.

        Args:
            prompt: The full prompt to send.

        Returns:
            The model's response text.
        """
        if not self.api_key:
            error_msg = (
                "OpenRouter API key not configured. "
                "Set config['analysis']['openrouter_api_key']."
            )
            logger.error(error_msg)
            return error_msg

        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "anthropic/claude-opus-4-5-20250918",
            "messages": [
                {"role": "user", "content": prompt},
            ],
        }

        try:
            with httpx.Client(timeout=300.0) as client:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()

            data = response.json()
            content = data["choices"][0]["message"]["content"]
            logger.info("OpenRouter API call successful (%d chars returned)", len(content))
            return content

        except httpx.HTTPStatusError as exc:
            logger.error(
                "OpenRouter API HTTP error %s: %s",
                exc.response.status_code,
                exc.response.text[:500],
            )
            return f"Error calling OpenRouter API: HTTP {exc.response.status_code}"

        except httpx.RequestError as exc:
            logger.error("OpenRouter API request error: %s", exc)
            return f"Error calling OpenRouter API: {exc}"

        except (KeyError, IndexError) as exc:
            logger.error("Unexpected OpenRouter API response structure: %s", exc)
            return f"Error parsing OpenRouter API response: {exc}"

    # ------------------------------------------------------------------
    # Operations Bible Generation
    # ------------------------------------------------------------------

    def generate_operations_bible(self) -> Path:
        """Generate the full operations bible document.

        Synthesizes profiles for all employees, compiles them into a
        structured markdown document, and saves it to bible_path.

        Returns:
            Path to the saved operations bible.
        """
        logger.info("Starting operations bible generation")
        employees = self.get_all_employees()

        if not employees:
            logger.warning("No employees found; generating empty operations bible")
            self.bible_path.write_text(
                "# Company Operations Bible\n\n"
                f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                "No employee data available yet.\n",
                encoding="utf-8",
            )
            return self.bible_path

        # Synthesize profiles for each employee
        profiles: dict[str, dict[str, str]] = {}
        for employee_name in employees:
            logger.info("Generating profile for: %s", employee_name)
            profile_text = self.synthesize_employee_profile(employee_name)

            # Retrieve computer_name from the employee's metadata
            results = self.query(
                query_text="work",
                employee_name=employee_name,
                n_results=1,
            )
            metadatas = results.get("metadatas", [[]])[0]
            computer_name = "Unknown Computer"
            if metadatas:
                computer_name = metadatas[0].get("computer_name", "Unknown Computer")

            profiles[employee_name] = {
                "profile": profile_text,
                "computer_name": computer_name,
            }

        # Generate cross-team observations
        all_profiles_text = ""
        for name, data in profiles.items():
            all_profiles_text += f"\n\n## {name}\n{data['profile']}"

        cross_team_prompt = self.BIBLE_CROSS_TEAM_PROMPT + all_profiles_text
        cross_team_observations = self._call_openrouter(cross_team_prompt)

        # Build the markdown document
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sections: list[str] = []

        sections.append("# Company Operations Bible")
        sections.append(f"Generated: {now}")
        sections.append("")

        # Table of contents
        sections.append("## Table of Contents")
        for employee_name in employees:
            sections.append(f"- {employee_name}")
        sections.append("")
        sections.append("---")
        sections.append("")

        # Employee sections
        for employee_name in employees:
            data = profiles[employee_name]
            sections.append(
                f"## {employee_name} - {data['computer_name']}"
            )
            sections.append("")
            sections.append(data["profile"])
            sections.append("")
            sections.append("---")
            sections.append("")

        # Cross-team observations
        sections.append("## Cross-Team Observations")
        sections.append("")
        sections.append(cross_team_observations)
        sections.append("")

        bible_content = "\n".join(sections)
        self.bible_path.write_text(bible_content, encoding="utf-8")
        self.last_synthesis_time = time.time()

        logger.info(
            "Operations bible generated and saved to %s (%d characters)",
            self.bible_path,
            len(bible_content),
        )
        return self.bible_path

    # ------------------------------------------------------------------
    # Periodic Synthesis
    # ------------------------------------------------------------------

    def start_periodic_synthesis(self) -> None:
        """Start a background thread that periodically generates the operations bible."""
        if not self.enabled:
            logger.info("RAG system is disabled; skipping periodic synthesis")
            return

        if self._synthesis_thread and self._synthesis_thread.is_alive():
            logger.warning("Periodic synthesis thread is already running")
            return

        self._stop_event.clear()
        self._synthesis_thread = threading.Thread(
            target=self._periodic_synthesis_loop,
            name="rag-periodic-synthesis",
            daemon=True,
        )
        self._synthesis_thread.start()
        logger.info(
            "Started periodic synthesis thread (interval: %ds)",
            self.synthesis_interval,
        )

    def _periodic_synthesis_loop(self) -> None:
        """Background loop that runs generate_operations_bible() at regular intervals."""
        logger.info("Periodic synthesis loop started")

        while not self._stop_event.is_set():
            try:
                logger.info("Running scheduled operations bible generation")
                self.generate_operations_bible()
            except Exception:
                logger.exception("Error during periodic operations bible generation")

            # Wait for the synthesis interval, checking stop event regularly
            # Check every 10 seconds so we can respond to stop requests promptly
            elapsed = 0.0
            check_interval = 10.0
            while elapsed < self.synthesis_interval and not self._stop_event.is_set():
                remaining = min(check_interval, self.synthesis_interval - elapsed)
                self._stop_event.wait(timeout=remaining)
                elapsed += remaining

        logger.info("Periodic synthesis loop stopped")

    def stop(self) -> None:
        """Stop the periodic synthesis thread."""
        logger.info("Stopping RAG system periodic synthesis")
        self._stop_event.set()

        if self._synthesis_thread and self._synthesis_thread.is_alive():
            self._synthesis_thread.join(timeout=30)
            if self._synthesis_thread.is_alive():
                logger.warning("Periodic synthesis thread did not stop within timeout")
            else:
                logger.info("Periodic synthesis thread stopped")

        self._synthesis_thread = None
