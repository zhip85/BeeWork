"""Apply PII / safety rules to a text (OC-013, PLT-144). Pure apart from the detectors it is given.

Several rules may hit at once; the strictest action wins: block > mask > flag. A hit records the rule, its layer, the
action and the stage — never the matched text (NFR-009). A detector that is unavailable is recorded as a failure and
skipped (fail open); the run is never stopped by a broken external detector.
"""

from dataclasses import dataclass, field

from beework.governance.detectors import REGISTRY, Detector, DetectorUnavailable, Match

DEFAULT_BLOCK_MESSAGE = "您的消息包含受限内容，已被拦截。"
JUDGE_PLACEHOLDER = "[内容已按安全策略处理]"
STAGES = ("input", "output")


@dataclass(frozen=True)
class PolicyRule:
    id: str
    name: str
    layer: str  # platform | tenant | assistant | baseline
    source: str  # pii | baseline
    category: str  # pii | prompt_attack | content_safety
    match_type: str
    pattern: str
    stage: str  # both | input | output
    action: str  # mask | block | flag
    block_message: str = ""
    enforced: bool = False


@dataclass(frozen=True)
class Hit:
    rule_id: str
    name: str
    layer: str
    source: str
    category: str
    action: str
    stage: str
    count: int

    def as_event(self) -> dict[str, str | int]:
        """What may be shown in events, traces and logs: never the matched text."""
        return {
            "rule_id": self.rule_id, "name": self.name, "layer": self.layer, "source": self.source, "category": self.category,
            "action": self.action, "stage": self.stage, "count": self.count,
        }


@dataclass
class ScanResult:
    text: str  # the text after masking (unchanged when blocked)
    blocked: bool = False
    message: str = ""  # the block message when blocked
    hits: list[Hit] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)  # {"rule_id", "name", "reason"}


def mask_text(value: str) -> str:
    """Keep the first and last characters and replace the middle with ``*`` (OC-013); short values keep less."""
    size = len(value)
    if size <= 2:
        return "*" * size
    head, tail = (3, 4) if size >= 11 else (2, 2) if size >= 8 else (1, 1)
    return value[:head] + "*" * (size - head - tail) + value[size - tail :]


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def detector_configured(match_type: str) -> bool:
    """Whether the detector for ``match_type`` can answer at all (the in-process ones always can)."""
    from beework.config import settings

    if match_type == "classifier":
        return bool(settings.presidio_analyzer_url)
    if match_type == "llm_judge":
        return bool(settings.safety_judge_model and settings.litellm_base_url and settings.litellm_master_key)
    return True


def applies(rule: PolicyRule, stage: str) -> bool:
    return rule.stage in ("both", stage)


def scan(text: str, rules: list[PolicyRule], stage: str, *, detectors: dict[str, Detector] | None = None) -> ScanResult:
    """Run every rule of ``stage`` over ``text``; the strictest action decides what happens to it."""
    registry = detectors if detectors is not None else REGISTRY
    result = ScanResult(text=text)
    if not text:
        return result
    spans: list[tuple[int, int]] = []
    judged_whole = False
    for rule in rules:
        if not applies(rule, stage):
            continue
        detector = registry.get(rule.match_type)
        if detector is None:
            result.failures.append({"rule_id": rule.id, "name": rule.name, "reason": "unsupported"})
            continue
        try:
            matches: list[Match] = detector.find(text, rule.match_type, rule.pattern)
        except DetectorUnavailable as exc:
            result.failures.append({"rule_id": rule.id, "name": rule.name, "reason": exc.reason})
            continue
        if not matches:
            continue
        result.hits.append(
            Hit(rule.id, rule.name, rule.layer, rule.source, rule.category, rule.action, stage, len(matches))
        )
        if rule.action == "block" and not result.blocked:
            result.blocked = True
            result.message = rule.block_message or DEFAULT_BLOCK_MESSAGE
        elif rule.action == "block" and not result.message:
            result.message = rule.block_message
        elif rule.action == "mask":
            if rule.match_type == "llm_judge":
                judged_whole = True
            spans.extend((m.start, m.end) for m in matches)
    if result.blocked:
        result.message = result.message or DEFAULT_BLOCK_MESSAGE
        return result
    if judged_whole:
        result.text = JUDGE_PLACEHOLDER
    elif spans:
        pieces: list[str] = []
        cursor = 0
        for start, end in _merge(spans):
            pieces.append(text[cursor:start])
            pieces.append(mask_text(text[start:end]))
            cursor = end
        pieces.append(text[cursor:])
        result.text = "".join(pieces)
    return result


def redact_for_logs(text: str, rules: list[PolicyRule]) -> str:
    """Mask what the rules would mask or block, for stored conversation text (SET-070). Only the in-process detectors run."""
    local = [r for r in rules if r.match_type in ("regex", "keyword") and r.action in ("mask", "block")]
    if not text or not local:
        return text
    spans: list[tuple[int, int]] = []
    for rule in local:
        try:
            spans.extend((m.start, m.end) for m in REGISTRY[rule.match_type].find(text, rule.match_type, rule.pattern))
        except DetectorUnavailable:
            continue
    if not spans:
        return text
    pieces: list[str] = []
    cursor = 0
    for start, end in _merge(spans):
        pieces.append(text[cursor:start])
        pieces.append(mask_text(text[start:end]))
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)
