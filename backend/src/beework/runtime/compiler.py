"""Compile an AssistantDSLv1 into a LangGraph ``StateGraph`` of the fixed OC stage pipeline (PRD 5.5).

Stages run in ``STAGE_ORDER``. Each is a node from ``STAGE_FACTORIES``; a stage without a registered
implementation passes the state through, so later tasks (T-055 PII, T-056 memory, T-057 glossary /
rewrite, T-058 routing, T-059 components) only register their own stage. A DSL cannot add nodes or run
code. Every stage reports ``step`` events, a Langfuse span ``oc.<stage>`` and its duration.
"""

import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any, TypedDict
from uuid import UUID

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from beework.governance.glossary_runtime import retrieval_query
from beework.runtime.dsl import AssistantDSLv1, KnowledgeBaseRef
from beework.runtime.errors import RunCancelled, RuntimeFailure  # noqa: I001
from beework.runtime.retrieval import Retriever

STAGE_ORDER: tuple[str, ...] = (
    "ingress",
    "admission",
    "pii_input",
    "memory",
    "glossary_rewrite",
    "route",
    "execute",
    "components",
    "pii_output",
    "egress",
)


class RuntimeState(TypedDict, total=False):
    input: str
    history: list[dict[str, str]]
    # Long-term memory snippets injected into the system prompt (filled by the memory stage, T-056a).
    memory: list[dict[str, str]]
    target: str
    context: list[dict[str, Any]]
    retrieval_mode: str
    text: str
    usage: dict[str, int | None]
    # PII / safety stages (T-055): a blocked input skips everything up to the egress; hits never carry matched text.
    blocked: bool
    block_message: str
    pii_hits: list[dict[str, Any]]
    pii_failures: list[dict[str, str]]
    pii_last: dict[str, Any]
    # Glossary / rewrite (T-057): the query routing and retrieval use, its synonyms and the matched terms.
    query: str
    synonyms: list[str]
    glossary_hits: list[dict[str, Any]]
    stage_info: dict[str, Any]


class StageClock:
    """Wall time per stage, plus the part of it spent in model / retrieval calls."""

    def __init__(self) -> None:
        self.stage_ms: dict[str, int] = {}
        self.external_seconds = 0.0

    @contextmanager
    def external(self):
        started = time.monotonic()
        try:
            yield
        finally:
            self.external_seconds += time.monotonic() - started

    @property
    def pipeline_ms(self) -> int:
        """Pipeline overhead: all stage time except model and retrieval calls (NFR-001)."""
        return max(0, int(sum(self.stage_ms.values()) - self.external_seconds * 1000))


@dataclass
class RuntimeDeps:
    llm: BaseChatModel
    retriever: Retriever
    tenant_id: UUID
    # Turns the DSL's mounted knowledge base ids into the tenant's current, searchable knowledge bases; the
    # retrieval step never searches anything else.
    resolve_bases: Callable[[Sequence[UUID]], list[KnowledgeBaseRef]]
    # Called between model chunks; raises RunCancelled / RunTimedOut to stop the run.
    check_stop: Callable[[], None] = lambda: None
    callbacks: list[Any] = field(default_factory=list)
    # Opens a tracing span named ``oc.<stage>`` around a stage (no-op without Langfuse).
    span: Callable[[str], AbstractContextManager] = lambda name: nullcontext()
    # Long-term memory snippets for the current message (the memory stage calls it; T-056a wires it).
    memory: Callable[[str], list[dict[str, str]]] = lambda query: []
    # The PII / safety rules in force for this run, and whether the reply is held back for the output rules (T-055).
    pii_rules: list[Any] = field(default_factory=list)
    hold_output: bool = False
    # The assistant's glossary dictionary (DictEntry list) and the question rewriter ``(query, history) -> RewriteResult``.
    dictionary: list[Any] = field(default_factory=list)
    rewrite: Callable[[str, list[dict[str, str]]], Any] | None = None
    clock: StageClock = field(default_factory=StageClock)
    # The tools the agent mounted and ticked (a ``ToolRuntime``; T-061) and the backup model, built only if needed.
    tools: Any = None
    fallback_llm: Callable[[], BaseChatModel] | None = None


Node = Callable[[RuntimeState], RuntimeState]
StageFactory = Callable[[AssistantDSLv1, RuntimeDeps], Node]
STAGE_FACTORIES: dict[str, StageFactory] = {}


def register_stage(name: str, *, replace: bool = False) -> Callable[[StageFactory], StageFactory]:
    """Register the implementation of an OC stage. Names outside ``STAGE_ORDER`` are rejected."""

    def decorator(factory: StageFactory) -> StageFactory:
        if name not in STAGE_ORDER:
            raise ValueError(f"Unknown OC stage: {name}")
        if name in STAGE_FACTORIES and not replace:
            raise RuntimeError(f"OC stage already registered: {name}")
        STAGE_FACTORIES[name] = factory
        return factory

    return decorator


def _passthrough(dsl: AssistantDSLv1, deps: RuntimeDeps) -> Node:
    return lambda state: {}


@register_stage("ingress")
def _ingress(dsl: AssistantDSLv1, deps: RuntimeDeps) -> Node:
    def node(state: RuntimeState) -> RuntimeState:
        text = (state.get("input") or "").strip()
        if not text:
            raise RuntimeFailure("runtime.input_rejected", "消息不能为空")
        if len(text) > dsl.limits.max_input_chars:
            raise RuntimeFailure("runtime.input_rejected", f"消息长度不能超过 {dsl.limits.max_input_chars} 个字符")
        return {"input": text}

    return node


@register_stage("route")
def _route(dsl: AssistantDSLv1, deps: RuntimeDeps) -> Node:
    # Until routing exists (T-058) the assistant answers with its own prompt and model.
    return lambda state: {"target": "assistant"}


STYLE_HINTS = {"concise": "回答尽量简洁，直奔重点，不展开无关内容。", "detailed": "回答尽量详细，必要时分步骤并说明理由。"}


def _system_prompt(dsl: AssistantDSLv1, state: RuntimeState, *, tools: bool = False) -> str:
    parts = [dsl.prompt]
    if dsl.role:
        parts.append(f"# 角色\n{dsl.role}")
    if dsl.answer_style in STYLE_HINTS:
        parts.append(f"# 回答风格\n{STYLE_HINTS[dsl.answer_style]}")
    if tools:
        parts.append("# 工具\n需要外部信息或操作时可以调用提供的工具；只依据工具返回的内容回答，不要编造工具结果。")
    memory = state.get("memory") or []
    if memory:
        parts.append("# 记忆\n" + "\n".join(f"- [{m.get('file', 'memory')}] {m['text']}" for m in memory))
    hits = state.get("glossary_hits") or []
    if hits:
        from beework.governance.glossary_runtime import DictEntry, prompt_section

        parts.append(prompt_section([DictEntry(h["term"], tuple(h.get("aliases", [])), h.get("definition", "")) for h in hits]))
    context = state.get("context") or []
    if context:
        snippets = "\n\n".join(
            f"[{i}] {'标准答案 · ' if c.get('kind') == 'faq' else ''}{c['document_name']}\n{c['text']}"
            for i, c in enumerate(context, 1)
        )
        intro = "以下是知识库的标准答案，请优先依据它回答。\n\n" if any(c.get("kind") == "faq" for c in context) else ""
        parts.append(f"# 参考资料\n{intro}{snippets}")
    return "\n\n".join(parts)


@register_stage("execute")
def _execute(dsl: AssistantDSLv1, deps: RuntimeDeps) -> Node:
    """Run the routed target. For ``assistant``: knowledge retrieval, then the model."""

    def node(state: RuntimeState) -> RuntimeState:
        if state.get("blocked"):
            return {}  # the input rules answered already: no retrieval, no model call
        writer = get_stream_writer()
        update: RuntimeState = {}
        bases = deps.resolve_bases([base.kb_id for base in dsl.knowledge.bases]) if dsl.knowledge.bases else []
        with deps.clock.external(), deps.span("retrieval"):
            result = deps.retriever.retrieve(
                tenant_id=deps.tenant_id, query=retrieval_query(state.get("query") or state["input"], state.get("synonyms", [])),
                bases=bases, top_k=dsl.knowledge.top_k,
            )
        update["retrieval_mode"] = result.mode
        update["context"] = [
            {
                "document_name": c.document_name, "chunk_id": c.chunk_id, "text": c.text, "score": c.score,
                "kind": c.kind, "kb_name": c.kb_name, "document_id": c.document_id,
            }
            for c in result.chunks
        ]

        offered = deps.tools.schemas() if deps.tools is not None else []
        messages: list[BaseMessage] = [SystemMessage(content=_system_prompt(dsl, {**state, **update}, tools=bool(offered)))]
        for turn in state.get("history", []):
            role = turn.get("role")
            if role == "user":
                messages.append(HumanMessage(content=turn["content"]))
            elif role == "assistant":
                messages.append(AIMessageChunk(content=turn["content"]))
        messages.append(HumanMessage(content=state["input"]))

        usage: dict[str, int | None] = {"input": None, "output": None}
        model = _ModelCaller(deps, writer)
        text = ""
        for _round in range(dsl.limits.max_iterations if offered else 1):
            reply, round_usage, text = model.call(messages, offered)
            usage = _add_usage(usage, round_usage)
            if not reply.tool_calls:
                break
            messages.append(reply)
            for call in reply.tool_calls:
                messages.append(_run_tool(deps, writer, call))
        else:
            if offered:  # still asking for tools after the limit: answer from what was gathered, tools off
                messages.append(SystemMessage(content="已达到工具调用次数上限，不能再调用工具。请根据已有信息直接回答。"))
                reply, round_usage, text = model.call(messages, [])
                usage = _add_usage(usage, round_usage)
        update["text"] = text
        update["usage"] = usage
        return update

    return node


def _add_usage(total: dict[str, int | None], new: dict[str, int | None]) -> dict[str, int | None]:
    return {key: (total.get(key) or 0) + (new.get(key) or 0) if (total.get(key) is not None or new.get(key) is not None) else None for key in ("input", "output")}


def _run_tool(deps: RuntimeDeps, writer: Callable[[Any], None], call: dict[str, Any]) -> ToolMessage:
    """One tool call of the model, through the controlled executor; the outcome is also a step event for the console."""
    alias, args = str(call.get("name") or ""), call.get("args") if isinstance(call.get("args"), dict) else {}
    deps.check_stop()
    result, duration_ms = deps.tools.call(alias, args)
    writer({"type": "step", "name": "tool", "tool": alias, "status": "completed" if result.status == "ok" else "failed",
            "duration_ms": duration_ms, "code": result.code})
    content = result.content if result.status == "ok" else f"[工具调用失败：{result.content}]"
    return ToolMessage(content=content, tool_call_id=str(call.get("id") or alias), name=alias)


class _ModelCaller:
    """Calls the model, once switching to the backup model if the primary fails before it produced anything (T-061)."""

    def __init__(self, deps: RuntimeDeps, writer: Callable[[Any], None]) -> None:
        self.deps, self.writer = deps, writer
        self.llm = deps.llm
        self.switched = False

    def call(self, messages: list[BaseMessage], tools: list[dict[str, Any]]) -> tuple[AIMessageChunk, dict[str, int | None], str]:
        try:
            return self._stream(messages, tools)
        except (RunCancelled, RuntimeFailure):
            raise
        except Exception as exc:
            if self.switched or self.deps.fallback_llm is None or getattr(exc, "beework_streamed", False):
                raise
            self.switched = True
            self.llm = self.deps.fallback_llm()
            code = type(exc).__name__
            with self.deps.span("model.fallback"):
                self.writer({"type": "step", "name": "model_fallback", "status": "completed", "reason": code})
            return self._stream(messages, tools)

    def _stream(self, messages: list[BaseMessage], tools: list[dict[str, Any]]) -> tuple[AIMessageChunk, dict[str, int | None], str]:
        runnable = self.llm.bind_tools(tools) if tools else self.llm
        aggregate: AIMessageChunk | None = None
        parts: list[str] = []
        usage: dict[str, int | None] = {"input": None, "output": None}
        try:
            with self.deps.clock.external():
                for chunk in runnable.stream(messages, config={"callbacks": self.deps.callbacks}):
                    self.deps.check_stop()
                    text = chunk.content if isinstance(chunk.content, str) else ""
                    if text:
                        parts.append(text)
                        if not self.deps.hold_output:  # output rules may rewrite the reply: it is sent once they have run
                            self.writer({"type": "token", "text": text})
                    meta = getattr(chunk, "usage_metadata", None)
                    if meta:
                        usage = {"input": meta.get("input_tokens"), "output": meta.get("output_tokens")}
                    aggregate = chunk if aggregate is None else aggregate + chunk
        except Exception as exc:
            if parts:
                exc.beework_streamed = True  # something reached the user already: switching models would repeat it
            raise
        return aggregate if aggregate is not None else AIMessageChunk(content=""), usage, "".join(parts)


@register_stage("egress")
def _egress(dsl: AssistantDSLv1, deps: RuntimeDeps) -> Node:
    return lambda state: {"text": (state.get("text") or "").strip()}


def _annotate(span: Any, name: str, state: RuntimeState, result: RuntimeState) -> None:
    """Record the original and the rewritten query on the stage's trace span (OC-051); best effort."""
    if span is None or name != "glossary_rewrite" or not result.get("stage_info"):
        return
    try:
        span.update(input={"original": state.get("input")}, output=result["stage_info"])
    except Exception:
        pass


def _instrumented(name: str, node: Node, deps: RuntimeDeps) -> Node:
    """Wrap a stage with step events, a tracing span, timing and the failing stage's name."""

    def wrapped(state: RuntimeState) -> RuntimeState:
        writer = get_stream_writer()
        writer({"type": "step", "name": name, "status": "started"})
        started = time.monotonic()
        try:
            with deps.span(name) as span:
                result = node(state)
                _annotate(span, name, state, result)
        except BaseException as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            deps.clock.stage_ms[name] = elapsed
            if not hasattr(exc, "beework_stage"):
                exc.beework_stage = name
            if not isinstance(exc, RunCancelled):
                writer({"type": "step", "name": name, "status": "failed", "duration_ms": elapsed})
            raise
        elapsed = int((time.monotonic() - started) * 1000)
        deps.clock.stage_ms[name] = elapsed
        data: dict[str, Any] = {"type": "step", "name": name, "status": "completed", "duration_ms": elapsed}
        if name == "execute":
            data["retrieval_mode"] = result.get("retrieval_mode")
        if name in ("pii_input", "pii_output") and result.get("pii_last") is not None:
            data.update({key: value for key, value in result["pii_last"].items() if value})
        if name == "glossary_rewrite" and result.get("stage_info"):
            data.update(result["stage_info"])
        writer(data)
        return result

    return wrapped


def compile_assistant(dsl: AssistantDSLv1, deps: RuntimeDeps):
    """Return the compiled graph for ``dsl``: every stage in ``STAGE_ORDER``, in that order."""
    builder = StateGraph(RuntimeState)
    previous = START
    for name in STAGE_ORDER:
        factory = STAGE_FACTORIES.get(name, _passthrough)
        builder.add_node(name, _instrumented(name, factory(dsl, deps), deps))
        builder.add_edge(previous, name)
        previous = name
    builder.add_edge(previous, END)
    return builder.compile()
