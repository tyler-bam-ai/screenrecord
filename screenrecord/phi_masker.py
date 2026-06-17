"""HIPAA PHI masking via an LLM (Vertex AI Gemini under a Google Cloud BAA).

Takes captured keystroke text and replaces any of the 18 HIPAA identifiers with
CONSISTENT pseudonyms — the same patient becomes the same alias (e.g.
"Patient_7A") everywhere, so a reviewer can still follow someone across screens
without seeing the real PHI. The masked text is what the dashboard shows; the
original text stays encrypted, and the alias map (alias -> original) is the
re-identification key, also stored encrypted and held only by authorized staff.

Honest scope:
  * This reduces exposure; it is NOT a certified de-identification. LLMs can miss
    things, so the data legally remains PHI under the BAA, and the masking LLM
    MUST be the BAA-covered endpoint (Vertex), never a general API.
  * Production uses Vertex AI Gemini (``build_vertex_caller``). The masking logic
    here is LLM-agnostic (inject any ``call_llm(prompt)->str``) so it can be
    unit-tested with synthetic (fake) data.
  * Visual PHI in screenshots is a separate, harder problem (vision + redaction)
    and is not handled here yet.
"""

import json
import logging
import re
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

LlmCaller = Callable[[str], str]

_PROMPT = """You are a strict HIPAA de-identification engine. You will receive a JSON array
of short text snippets a user typed, plus an existing alias map to keep aliases
consistent.

Find every instance of the 18 HIPAA identifiers — patient/person names, dates
(birth, admission, etc.), ages over 89, phone/fax, email, SSN, medical record
numbers, account numbers, addresses/geographic detail smaller than a state,
health-plan/beneficiary numbers, license/certificate numbers, vehicle/device
identifiers, URLs, IP addresses, biometric IDs, and any other uniquely
identifying number or code.

Replace each identifier with a consistent alias token. REUSE an alias from the
provided map if the same real value already has one; otherwise mint a new short,
stable alias like Patient_001, DOB_001, MRN_001, Phone_001, Addr_001. The SAME
real value must always map to the SAME alias.

Return ONLY minified JSON, no prose:
{"masked": ["...","..."], "new_aliases": {"<real value>": "<alias>"}}

EXISTING_ALIASES: %s
SNIPPETS: %s"""


def build_vertex_caller(project: str, location: str, model: str) -> LlmCaller:
    """Return a call_llm(prompt)->text backed by Vertex AI Gemini (BAA path).

    Requires ``google-genai`` and a Vertex-enabled project with the service
    account granted "Vertex AI User". Imported lazily so the recorder runs
    without these when masking is disabled.
    """
    from google import genai  # google-genai

    client = genai.Client(vertexai=True, project=project, location=location)

    def _call(prompt: str) -> str:
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
            config={"response_mime_type": "application/json", "temperature": 0},
        )
        return resp.text or ""

    return _call


class PhiMasker:
    """Masks PHI in text snippets, keeping a persistent alias map."""

    def __init__(self, call_llm: LlmCaller, alias_map: Optional[Dict[str, str]] = None) -> None:
        self._call = call_llm
        # original (real value) -> alias. This is the re-identification key.
        self.alias_map: Dict[str, str] = dict(alias_map or {})

    def mask_texts(self, texts: List[str]) -> Tuple[List[str], Dict[str, str]]:
        """Return (masked_texts, newly_added_aliases). Updates self.alias_map.

        On any failure, fails CLOSED: returns the snippets fully redacted
        ("[REDACTED]") rather than leaking raw PHI.
        """
        if not texts:
            return [], {}
        prompt = _PROMPT % (json.dumps(self.alias_map), json.dumps(texts))
        try:
            raw = self._call(prompt)
            data = _parse_json(raw)
            masked = data.get("masked")
            new_aliases = data.get("new_aliases", {}) or {}
            if not isinstance(masked, list) or len(masked) != len(texts):
                raise ValueError("LLM returned malformed/mismatched 'masked' array")
        except Exception:
            logger.exception("PHI masking failed; failing closed (redacting).")
            return ["[REDACTED]" for _ in texts], {}

        # Record new aliases (real -> alias) for the re-identification key.
        added: Dict[str, str] = {}
        for real, alias in new_aliases.items():
            if real not in self.alias_map:
                self.alias_map[real] = alias
                added[real] = alias

        # Defense in depth: if any known real value slipped through unmasked,
        # substitute its alias locally so raw PHI never reaches the masked output.
        masked = [self._apply_known_aliases(str(m)) for m in masked]
        return masked, added

    def _apply_known_aliases(self, text: str) -> str:
        for real, alias in self.alias_map.items():
            if real and real in text:
                text = re.sub(re.escape(real), alias, text)
        return text


def reconstruct_typed_text(events: List[dict], max_gap_sec: float = 3.0) -> List[str]:
    """Reconstruct typed-text bursts from an input-event log.

    PHI lives in sequences of keystrokes, not single keys. Concatenate
    consecutive printable ``key_press`` characters into strings, breaking on a
    non-typing event (e.g. a mouse click) or a pause longer than *max_gap_sec*.
    Returns the non-empty typed bursts in order.
    """
    bursts: List[str] = []
    cur: List[str] = []
    last_off: Optional[float] = None
    for ev in events:
        if ev.get("event_type") != "key_press":
            if cur:
                bursts.append("".join(cur)); cur = []
            last_off = None
            continue
        key = (ev.get("details") or {}).get("key", "")
        off = ev.get("video_offset_sec")
        gap = (off - last_off) if (last_off is not None and off is not None) else 0
        if gap > max_gap_sec and cur:
            bursts.append("".join(cur)); cur = []
        if isinstance(key, str) and len(key) == 1 and key.isprintable():
            cur.append(key)
        elif key == "Key.space":
            cur.append(" ")
        else:  # Enter, Backspace, modifiers, etc. end the burst
            if cur:
                bursts.append("".join(cur)); cur = []
        last_off = off
    if cur:
        bursts.append("".join(cur))
    return [b for b in (s.strip() for s in bursts) if b]


def _parse_json(raw: str) -> dict:
    raw = raw.strip()
    # tolerate code fences / stray prose around the JSON object
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("{"):]
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in LLM response")
    return json.loads(raw[start:end + 1])
