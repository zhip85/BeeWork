"""Question rewriting (T-057, OC-040–051): a pure service around one structured model call.

``rewrite(query, history, config, llm)`` asks the model once for the result of every enabled step as JSON (coreference →
colloquial normalisation → decomposition → translation); alias expansion is not here, it is local and deterministic
(``governance.glossary_runtime``). Any failure — timeout (1.5 s), a model error, unusable output — returns the original
text: a rewrite only ever improves routing and retrieval, it must never get in the way of the answer.
The rewritten query is for routing and retrieval only; the conversation keeps the original (OC-051).
"""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from beework.runtime.oc import RewriteConfig

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 1.5
MAX_QUERY_CHARS = 2000
STEP_ORDER = ("coreference", "normalize", "decompose", "translate")


@dataclass
class RewriteResult:
    query: str  # what routing and retrieval use
    steps: list[dict[str, Any]] = field(default_factory=list)  # only the steps that changed something: {step, before, after}
    sub_questions: list[str] = field(default_factory=list)
    failure: str | None = None  # timeout | invalid | error — the original text was used
    used_original: bool = True


def active_steps(config: RewriteConfig) -> list[str]:
    steps = []
    if config.coreference.enabled:
        steps.append("coreference")
    if config.normalize:
        steps.append("normalize")
    if config.decompose.enabled:
        steps.append("decompose")
    if config.translate.enabled:
        steps.append("translate")
    return steps


def build_messages(query: str, history: list[dict[str, str]], config: RewriteConfig) -> list[BaseMessage]:
    steps = active_steps(config)
    lines = ["你是问题改写器。只输出一个 JSON 对象，不要输出其他内容。按顺序处理用户的最新问题，各字段含义："]
    schema: dict[str, str] = {}
    if "coreference" in steps:
        lines.append("1. coreference：把“它/这个/那个”等指代替换为上文中的具体对象；没有指代就原样返回。")
        schema["coreference"] = "string"
    if "normalize" in steps:
        lines.append("2. normalized：把口语改成规范表达（如“咋”→“如何”、“啥时候”→“什么时候”），不改变含义，不补充问题里没有的信息。")
        schema["normalized"] = "string"
    if "decompose" in steps:
        lines.append(f"3. sub_questions：如果是复合问题，拆成最多 {config.decompose.max_subquestions} 个独立子问题；不需要拆分就返回空数组。")
        schema["sub_questions"] = "string[]"
    if "translate" in steps:
        lines.append(f"4. translated：把最终问题翻译成 {config.translate.language}（有子问题时翻译拆分前的完整问题）。")
        schema["translated"] = "string"
    lines.append("输出格式：" + json.dumps(schema, ensure_ascii=False))
    system = "\n".join(lines)
    if config.custom_prompt.enabled and config.custom_prompt.text.strip():
        system += "\n\n补充要求：\n" + config.custom_prompt.text.strip()
    context = ""
    if "coreference" in steps and history:
        window = history[-config.coreference.rounds * 2 :]
        context = "上文：\n" + "\n".join(f"{'用户' if turn.get('role') == 'user' else '助手'}：{turn.get('content', '')[:400]}" for turn in window) + "\n\n"
    return [SystemMessage(content=system), HumanMessage(content=f"{context}最新问题：{query}")]


def _json_object(text: str) -> dict[str, Any] | None:
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


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", value).strip()[:MAX_QUERY_CHARS] if isinstance(value, str) else ""


def parse_result(query: str, data: dict[str, Any], config: RewriteConfig) -> RewriteResult:
    """Fold the model's JSON into steps. Only steps whose output differs from the text before them are reported."""
    steps: list[dict[str, Any]] = []
    current = query

    def record(step: str, after: str) -> None:
        nonlocal current
        if after and after != current:
            steps.append({"step": step, "before": current, "after": after})
            current = after

    if config.coreference.enabled:
        record("coreference", _clean(data.get("coreference")))
    if config.normalize:
        record("normalize", _clean(data.get("normalized")))
    subs: list[str] = []
    if config.decompose.enabled and isinstance(data.get("sub_questions"), list):
        subs = [_clean(item) for item in data["sub_questions"] if _clean(item)][: config.decompose.max_subquestions]
        if len(subs) >= 2:
            steps.append({"step": "decompose", "before": current, "after": "；".join(subs)})
        else:
            subs = []
    final = current
    if config.translate.enabled:
        translated = _clean(data.get("translated"))
        if translated and translated != current:
            steps.append({"step": "translate", "before": current, "after": translated})
            final = translated
            subs = []  # the translation covers the whole question: sub-questions in the original language are not used
    if subs:
        final = "；".join(subs)
    return RewriteResult(query=final, steps=steps, sub_questions=subs, failure=None, used_original=final == query)


def rewrite(
    query: str, history: list[dict[str, str]], config: RewriteConfig, llm: BaseChatModel, *, timeout: float = TIMEOUT_SECONDS,
    callbacks: list[Any] | None = None,
) -> RewriteResult:
    """One model call; the original query comes back when it times out, fails or answers with something unusable."""
    if not query.strip() or not active_steps(config):
        return RewriteResult(query=query)
    messages = build_messages(query, history, config)
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(lambda: llm.invoke(messages, config={"callbacks": callbacks or []}))
    try:
        reply = future.result(timeout=timeout)
    except FutureTimeout:
        logger.warning("Question rewrite timed out after %.1fs", timeout)
        return RewriteResult(query=query, failure="timeout")
    except Exception:
        logger.warning("Question rewrite failed", exc_info=True)
        return RewriteResult(query=query, failure="error")
    finally:
        executor.shutdown(wait=False)
    content = reply.content if isinstance(reply.content, str) else ""
    data = _json_object(content)
    if data is None:
        return RewriteResult(query=query, failure="invalid")
    return parse_result(query, data, config)
