"""L2 intent judgement (T-161, 演进 PRD 23.1): who answers a message — a capability directly (path A) or the L3 agent (path B).

Everything here is a pure function of its inputs (the classifier and the argument extractor take a model), so the whole decision is
unit-testable. The order:

1. a conversation waiting on a Dify Chatflow sends the next message straight back to it (T-163 writes that state; this reads it);
2. with priority rules on, the first enabled rule — by priority, then creation time — whose target is still a callable capability wins;
3. the candidates (what the registry recalled inside the agent's scope) go to the **classifier**, which answers with confidences;
4. the top candidate must reach the threshold *and* lead the second by the margin, be callable directly (``direct_enabled``) and not be a
   write — otherwise L3 plans, with the candidates as hints. No candidates, a classifier that fails or times out, or an answer that
   names nothing it was given: also L3. **Nothing here ever raises into the run.**

The classifier is an interface (``IntentClassifier``): today an ordinary chat model answering in JSON (``LlmJsonClassifier``), later a
dedicated classification model behind the same interface — the model is a setting, never a constant.
"""

import json
import logging
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import regex
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from beework.runtime.oc import IntentConfig, IntentRule, keywords_of
from beework.tools.schema import validate

logger = logging.getLogger(__name__)

CLASSIFY_TIMEOUT_SECONDS = 8.0
EXTRACT_TIMEOUT_SECONDS = 8.0
REGEX_TIMEOUT_SECONDS = 0.05  # a rule's regular expression is the administrator's; a pathological one must not stall a message
MAX_MATCH_CHARS = 2000
MAX_SCORED = 3  # what the classifier may return


@dataclass(frozen=True)
class Candidate:
    """A registry entry as the decision sees it."""

    id: str
    kind: str  # mcp_tool | api_tool | skill | dify_workflow | dify_chatflow | agent | kb
    name: str
    description: str = ""
    intent_description: str = ""
    examples: tuple[str, ...] = ()
    direct: bool = False  # direct_enabled
    writes: bool = False
    input_schema: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)
    recall_score: float = 0.0
    source_id: str = ""  # the capability / connection / sub-agent / knowledge base the entry comes from
    tool: str = ""  # the executor's tool name (T-071) that calls it
    direct_output: bool = False  # path A returns its result as is, without the composing call


@dataclass
class Scored:
    id: str
    confidence: float
    reason: str = ""


@dataclass
class ClassifyResult:
    scored: list[Scored] = field(default_factory=list)  # best first
    failure: str | None = None  # timeout | error | invalid: the classifier could not decide
    ms: int = 0


class IntentClassifier(Protocol):
    def classify(self, query: str, history: list[dict[str, str]], candidates: list[Candidate]) -> ClassifyResult: ...


HandoffReason = Literal[
    "none", "no_candidates", "classifier_failed", "low_confidence", "ambiguous", "not_direct", "write", "slots_missing", "slots_invalid",
    "slots_failed", "capability_failed", "disabled",
]


@dataclass
class IntentDecision:
    path: Literal["A", "B"]
    source: Literal["pending", "rule", "classifier", "faq", "none"] = "none"
    target: Candidate | None = None  # path A: what is called; path B: the best hint (may be None)
    confidence: float | None = None
    second: float | None = None
    reason: str = ""  # the classifier's reason
    handoff: HandoffReason = "none"  # path B: why L3 and not a direct call
    failure: str | None = None
    rule_id: str | None = None
    rule_name: str | None = None
    scored: list[Scored] = field(default_factory=list)
    classifier_ms: int | None = None

    def to_b(self, reason: HandoffReason) -> "IntentDecision":
        self.path, self.handoff = "B", reason
        return self


# ---------------------------------------------------------------------------
# Priority rules (OC-061).
# ---------------------------------------------------------------------------


def rule_matches(rule: IntentRule, text: str) -> bool:
    """Case-insensitive; keywords match as substrings (any one), a regular expression with ``search``. One that times out is no match."""
    text = text[:MAX_MATCH_CHARS]
    if rule.match_type == "keyword":
        lowered = text.lower()
        return any(word in lowered for word in keywords_of(rule.pattern))
    try:
        return regex.search(rule.pattern, text, flags=regex.IGNORECASE, timeout=REGEX_TIMEOUT_SECONDS) is not None
    except (TimeoutError, regex.error):
        return False


def rule_order(rule: IntentRule) -> tuple[int, str, str]:
    return (rule.priority, rule.created_at or "", rule.id)


def matching_rule(rules: list[IntentRule], text: str, callable_by_id: dict[str, Candidate]) -> IntentRule | None:
    """The first enabled rule, in priority order, that matches and whose target is still a directly callable capability (a rule whose
    target is gone, unavailable or no longer direct is skipped — the editor shows it as “目标失效 / 待重选”)."""
    for rule in sorted(rules, key=rule_order):
        if rule.enabled and rule.target_entry_id in callable_by_id and rule_matches(rule, text):
            return rule
    return None


# ---------------------------------------------------------------------------
# The classifier (JEV).
# ---------------------------------------------------------------------------


def build_classifier_messages(query: str, history: list[dict[str, str]], candidates: list[Candidate], config: IntentConfig) -> list[BaseMessage]:
    lines = [
        "你是意图分类器：根据用户的问题，从候选能力里判断用户想用哪一个。只输出一个 JSON 对象，不要输出其他内容：",
        '{"candidates": [{"id": "候选的 id", "confidence": 0 到 1 之间的小数, "reason": "一句话理由"}]}',
        f"按 confidence 从高到低最多给 {MAX_SCORED} 个；一个都不合适就给空数组。confidence 表示你有多确定这个能力就是用户要的：问题明确属于它的用途时给高分；含糊、缺少信息或同时适合多个时给低分。",
        "只能使用下面列出的 id，不要编造。",
        "候选能力：",
    ]
    for item in candidates:
        extras = f"；用途：{item.intent_description}" if item.intent_description else ""
        examples = f"；示例问法：{' / '.join(item.examples[:3])}" if item.examples else ""
        lines.append(f"- id: {item.id}；类型：{item.kind}；名称：{item.name}；说明：{item.description or '（无）'}{extras}{examples}")
    system = "\n".join(lines)
    if config.custom_prompt.enabled and config.custom_prompt.text.strip():
        system += "\n\n补充要求：\n" + config.custom_prompt.text.strip()
    context = ""
    if history:
        context = "上文：\n" + "\n".join(f"{'用户' if turn.get('role') == 'user' else '助手'}：{str(turn.get('content', ''))[:300]}" for turn in history[-4:]) + "\n\n"
    return [SystemMessage(content=system), HumanMessage(content=f"{context}用户的问题：{query[:MAX_MATCH_CHARS]}")]


def json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    elif not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        text = text[start : end + 1]
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def parse_classification(content: str, candidates: list[Candidate]) -> ClassifyResult:
    """An id that is not a candidate (an invented one) is dropped, and so is its confidence; confidences are clamped to 0–1."""
    data = json_object(content)
    if data is None or not isinstance(data.get("candidates"), list):
        return ClassifyResult(failure="invalid")
    known = {c.id for c in candidates}
    scored: list[Scored] = []
    seen: set[str] = set()
    for item in data["candidates"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] not in known or item["id"] in seen:
            continue
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.0
        seen.add(item["id"])
        scored.append(Scored(item["id"], min(1.0, max(0.0, confidence)), str(item.get("reason") or "")[:200]))
    scored.sort(key=lambda s: -s.confidence)
    return ClassifyResult(scored=scored[:MAX_SCORED])


class LlmJsonClassifier:
    """The first ``IntentClassifier``: one structured call of an ordinary chat model. A failure, a timeout or an unusable answer is a
    result (``failure``), never an exception."""

    def __init__(self, llm: BaseChatModel, config: IntentConfig, *, timeout: float = CLASSIFY_TIMEOUT_SECONDS, callbacks: list[Any] | None = None) -> None:
        self.llm, self.config, self.timeout, self.callbacks = llm, config, timeout, callbacks or []

    def classify(self, query: str, history: list[dict[str, str]], candidates: list[Candidate]) -> ClassifyResult:
        started = time.monotonic()
        messages = build_classifier_messages(query, history, candidates, self.config)
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(lambda: self.llm.invoke(messages, config={"callbacks": self.callbacks}))
        try:
            reply = future.result(timeout=self.timeout)
        except FutureTimeout:
            logger.warning("Intent classification timed out after %.1fs", self.timeout)
            return ClassifyResult(failure="timeout", ms=int((time.monotonic() - started) * 1000))
        except Exception:
            logger.warning("Intent classification failed", exc_info=True)
            return ClassifyResult(failure="error", ms=int((time.monotonic() - started) * 1000))
        finally:
            executor.shutdown(wait=False)
        result = parse_classification(reply.content if isinstance(reply.content, str) else "", candidates)
        result.ms = int((time.monotonic() - started) * 1000)
        return result


# ---------------------------------------------------------------------------
# The decision.
# ---------------------------------------------------------------------------

Classify = Callable[[str], ClassifyResult]


def decide(
    *, text: str, candidates: list[Candidate], config: IntentConfig, classify: Classify | None, pending: Candidate | None = None,
    callable_by_id: dict[str, Candidate] | None = None,
) -> IntentDecision:
    """``text`` is what the rules look at (the user's message); ``classify`` judges the query the stage built (which may carry earlier
    turns). ``callable_by_id`` = the scope's available, directly callable, non-write capabilities (what a rule may target)."""
    callable_by_id = callable_by_id if callable_by_id is not None else {c.id: c for c in candidates if c.direct and not c.writes}
    if pending is not None:
        return IntentDecision(path="A", source="pending", target=pending, confidence=1.0, reason="等待用户补充的流程")
    if config.priority_rules_enabled:
        rule = matching_rule(config.rules, text, callable_by_id)
        if rule is not None:
            return IntentDecision(path="A", source="rule", target=callable_by_id[rule.target_entry_id or ""], confidence=1.0, rule_id=rule.id, rule_name=rule.name)
    if not candidates:
        return IntentDecision(path="B", handoff="no_candidates")
    if classify is None:
        return IntentDecision(path="B", handoff="classifier_failed", failure="error")
    result = classify(text)
    by_id = {c.id: c for c in candidates}
    if result.failure:
        return IntentDecision(path="B", handoff="classifier_failed", failure=result.failure, classifier_ms=result.ms)
    decision = IntentDecision(path="B", scored=result.scored, classifier_ms=result.ms)
    if not result.scored:
        return decision.to_b("low_confidence")
    top = result.scored[0]
    second = result.scored[1].confidence if len(result.scored) > 1 else None
    decision.target, decision.confidence, decision.second, decision.reason = by_id[top.id], top.confidence, second, top.reason
    if top.confidence < config.threshold:
        return decision.to_b("low_confidence")
    if second is not None and top.confidence - second < config.margin:
        return decision.to_b("ambiguous")
    target = decision.target
    if target.writes:
        return decision.to_b("write")  # a write needs the end user's confirmation: only the planner can ask for it
    if target.kind == "kb":
        if not config.faq_direct:
            return decision.to_b("not_direct")  # a knowledge base is only ever answered directly from an FAQ, and only when that is on
    elif not target.direct:
        return decision.to_b("not_direct")
    decision.path, decision.source = "A", "classifier"
    return decision


# ---------------------------------------------------------------------------
# Arguments of a directly called capability (slot filling).
# ---------------------------------------------------------------------------


@dataclass
class Slots:
    status: Literal["ok", "missing", "invalid", "failed"]
    args: dict[str, Any] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)  # required arguments the message did not provide
    problems: list[str] = field(default_factory=list)


def trivial_arguments(kind: str, schema: dict[str, Any], query: str) -> dict[str, Any] | None:
    """What needs no model: a sub-agent gets the message as its task, a Dify app as its query, a capability without parameters nothing.
    ``None`` = a model has to read the arguments out of the message."""
    properties = (schema or {}).get("properties") or {}
    if kind == "agent":
        return {"task": query}
    if kind == "skill":
        return {}
    if kind == "dify_chatflow" and set(properties) <= {"query"}:
        return {"query": query}
    if kind == "dify_workflow":
        if "inputs" in properties:
            return {"inputs": {"query": query}}
        if len(properties) == 1:
            return {next(iter(properties)): query}
    if not properties:
        return {}
    return None


def build_extraction_messages(query: str, history: list[dict[str, str]], candidate: Candidate, schema: dict[str, Any]) -> list[BaseMessage]:
    system = (
        f"你要从用户的话里为能力「{candidate.name}」（{candidate.description or candidate.intent_description}）抽取调用参数。只输出一个 JSON 对象，不要输出其他内容。\n"
        "只能使用下面参数定义里的键；用户没有说清楚的参数不要编造，直接省略；类型要符合定义。\n"
        f"参数定义（JSON Schema）：{json.dumps(schema, ensure_ascii=False)[:3000]}"
    )
    context = ""
    if history:
        context = "上文：\n" + "\n".join(f"{'用户' if t.get('role') == 'user' else '助手'}：{str(t.get('content', ''))[:300]}" for t in history[-4:]) + "\n\n"
    return [SystemMessage(content=system), HumanMessage(content=f"{context}用户的话：{query[:MAX_MATCH_CHARS]}")]


def extract_arguments(
    llm: BaseChatModel, candidate: Candidate, query: str, history: list[dict[str, str]], *, timeout: float = EXTRACT_TIMEOUT_SECONDS,
    callbacks: list[Any] | None = None,
) -> Slots:
    """Read the arguments out of the message by the capability's input schema and check them against it. A missing required argument
    or one that does not validate is not an error — it hands the message to L3, which can ask the user."""
    schema = candidate.input_schema or {}
    trivial = trivial_arguments(candidate.kind, schema, query)
    if trivial is None:
        executor = ThreadPoolExecutor(max_workers=1)
        messages = build_extraction_messages(query, history, candidate, schema)
        future = executor.submit(lambda: llm.invoke(messages, config={"callbacks": callbacks or []}))
        try:
            reply = future.result(timeout=timeout)
        except FutureTimeout:
            return Slots("failed", problems=["timeout"])
        except Exception:
            logger.warning("Argument extraction failed", exc_info=True)
            return Slots("failed", problems=["error"])
        finally:
            executor.shutdown(wait=False)
        data = json_object(reply.content if isinstance(reply.content, str) else "")
        if data is None:
            return Slots("failed", problems=["invalid"])
        allowed = set(schema.get("properties") or {})
        args: dict[str, Any] = {key: value for key, value in data.items() if key in allowed and value not in (None, "")}
    else:
        args = dict(trivial)
    if candidate.kind == "dify_chatflow" and "query" in ((schema.get("properties")) or {}):
        args["query"] = query  # a chatflow always gets the message itself
    required = [str(name) for name in (schema.get("required") or [])]
    missing = [name for name in required if name not in args]
    if missing:
        return Slots("missing", args=args, missing=missing)
    problems = validate(args, schema)
    if problems:
        return Slots("invalid", args=args, problems=problems[:5])
    return Slots("ok", args=args)
