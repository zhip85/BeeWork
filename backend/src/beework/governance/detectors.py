"""Detectors that find the spans a PII / safety rule matches in a text (T-055).

``regex`` and ``keyword`` run inside BeeWork. The ``regex`` detector is bounded: patterns are compiled with the ``regex``
module and every scan has a time limit, so a catastrophic pattern cannot stall a run. ``classifier`` calls a Presidio
Analyzer over REST and ``llm_judge`` asks a model through the AI gateway; both are optional services. A detector that
is unavailable, slow or failing raises ``DetectorUnavailable`` and the caller fails open (the run goes on, the failure is
recorded) — an outage of an external detector must not stop all traffic.
"""

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

import httpx
import regex

from beework.config import settings

logger = logging.getLogger(__name__)

REGEX_TIMEOUT_SECONDS = 0.05  # per pattern and window; a legitimate rule matches in microseconds
MAX_PATTERN_CHARS = 1000
WINDOW_CHARS = 20_000  # longer texts are scanned window by window so nothing is left unchecked
WINDOW_OVERLAP = 200
MAX_MATCHES = 200  # per rule and text; beyond this the rest is not recorded


class DetectorUnavailable(Exception):
    """The detector could not give an answer: not configured, timed out or failed. ``reason`` is a stable short code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Match:
    start: int
    end: int


class Detector(Protocol):
    def find(self, text: str, match_type: str, pattern: str) -> list[Match]: ...


def _windows(text: str):
    if len(text) <= WINDOW_CHARS:
        yield 0, text
        return
    step = WINDOW_CHARS - WINDOW_OVERLAP
    for start in range(0, len(text), step):
        yield start, text[start : start + WINDOW_CHARS]
        if start + WINDOW_CHARS >= len(text):
            break


@lru_cache(maxsize=512)
def _compile(pattern: str):
    return regex.compile(pattern)


def _dedupe(matches: list[Match]) -> list[Match]:
    seen: set[tuple[int, int]] = set()
    out = []
    for match in sorted(matches, key=lambda m: (m.start, m.end)):
        if (match.start, match.end) not in seen and match.end > match.start:
            seen.add((match.start, match.end))
            out.append(match)
    return out[:MAX_MATCHES]


class RegexDetector:
    def find(self, text: str, match_type: str, pattern: str) -> list[Match]:
        if not pattern or len(pattern) > MAX_PATTERN_CHARS:
            raise DetectorUnavailable("invalid_pattern")
        try:
            compiled = _compile(pattern)
        except regex.error as exc:
            raise DetectorUnavailable("invalid_pattern") from exc
        found: list[Match] = []
        try:
            for offset, window in _windows(text):
                found.extend(Match(offset + m.start(), offset + m.end()) for m in compiled.finditer(window, timeout=REGEX_TIMEOUT_SECONDS))
        except TimeoutError as exc:
            logger.warning("PII regex timed out (pattern length %s)", len(pattern))
            raise DetectorUnavailable("timeout") from exc
        return _dedupe(found)


class KeywordDetector:
    """Case-insensitive occurrences of any keyword (comma separated, as the rule stores them)."""

    def find(self, text: str, match_type: str, pattern: str) -> list[Match]:
        folded = text.casefold()
        found: list[Match] = []
        for word in (item.strip().casefold() for item in pattern.split(",")):
            if not word:
                continue
            start = folded.find(word)
            while start != -1 and len(found) < MAX_MATCHES * 4:
                found.append(Match(start, start + len(word)))
                start = folded.find(word, start + len(word))
        return _dedupe(found)


class PresidioDetector:
    """The Presidio Analyzer REST API (``POST /analyze``). The rule's pattern lists the entity types to look for."""

    def __init__(self, base_url: str | None = None, transport: httpx.BaseTransport | None = None, timeout: float = 1.0) -> None:
        self._base_url = base_url
        self._transport = transport
        self._timeout = timeout

    def find(self, text: str, match_type: str, pattern: str) -> list[Match]:
        base = self._base_url if self._base_url is not None else settings.presidio_analyzer_url
        if not base:
            raise DetectorUnavailable("not_configured")
        entities = [item.strip().upper() for item in pattern.replace("\n", ",").split(",") if item.strip()]
        body: dict = {"text": text, "language": settings.presidio_language}
        if entities:
            body["entities"] = entities
        try:
            with httpx.Client(base_url=base.rstrip("/"), timeout=self._timeout, transport=self._transport) as client:
                response = client.post("/analyze", json=body)
            response.raise_for_status()
            return _dedupe([Match(int(item["start"]), int(item["end"])) for item in response.json()])
        except httpx.TimeoutException as exc:
            raise DetectorUnavailable("timeout") from exc
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise DetectorUnavailable("failed") from exc


class LlmJudgeDetector:
    """Asks the configured safety-judge model (through LiteLLM) whether the text meets the rule's description."""

    SYSTEM = "你是内容安全判定器。根据“判定标准”判断“文本”是否符合该标准。只回答 YES 或 NO，不要解释。"

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    def find(self, text: str, match_type: str, pattern: str) -> list[Match]:
        if not (settings.safety_judge_model and settings.litellm_base_url and settings.litellm_master_key):
            raise DetectorUnavailable("not_configured")
        body = {
            "model": settings.safety_judge_model, "max_tokens": 3, "temperature": 0,
            "messages": [{"role": "system", "content": self.SYSTEM}, {"role": "user", "content": f"判定标准：{pattern}\n\n文本：{text[:4000]}"}],
        }
        try:
            with httpx.Client(
                base_url=settings.litellm_base_url.rstrip("/"), timeout=settings.safety_judge_timeout_seconds, transport=self._transport
            ) as client:
                response = client.post("/chat/completions", json=body, headers={"Authorization": f"Bearer {settings.litellm_master_key}"})
            response.raise_for_status()
            answer = str(response.json()["choices"][0]["message"]["content"]).strip().upper()
        except httpx.TimeoutException as exc:
            raise DetectorUnavailable("timeout") from exc
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise DetectorUnavailable("failed") from exc
        return [Match(0, len(text))] if answer.startswith("YES") and text else []


REGISTRY: dict[str, Detector] = {
    "regex": RegexDetector(),
    "keyword": KeywordDetector(),
    "classifier": PresidioDetector(),
    "llm_judge": LlmJudgeDetector(),
}
