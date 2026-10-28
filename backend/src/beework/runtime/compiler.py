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
from langchain_core.messages import AIMessageChunk, BaseMessage, HumanMessage, SystemMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from beework.runtime.dsl import AssistantDSLv1
from beework.runtime.errors import RunCancelled, RuntimeFailure
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
    # Dataset ids the tenant may query; the retrieval step never passes anything else on.
    allowed_dataset_ids: Callable[[Sequence[str]], list[str]]
    # Called between model chunks; raises RunCancelled / RunTimedOut to stop the run.
    check_stop: Callable[[], None] = lambda: None
    callbacks: list[Any] = field(default_factory=list)
    # Opens a tracing span named ``oc.<stage>`` around a stage (no-op without Langfuse).
    span: Callable[[str], AbstractContextManager] = lambda name: nullcontext()
    clock: StageClock = field(default_factory=StageClock)


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


def _system_prompt(dsl: AssistantDSLv1, state: RuntimeState) -> str:
    parts = [dsl.prompt]
    memory = state.get("memory") or []
    if memory:
        parts.append("# 记忆\n" + "\n".join(f"- [{m.get('file', 'memory')}] {m['text']}" for m in memory))
    context = state.get("context") or []
    if context:
        snippets = "\n\n".join(f"[{i}] {c['document_name']}\n{c['text']}" for i, c in enumerate(context, 1))
        parts.append(f"# 参考资料\n{snippets}")
    return "\n\n".join(parts)


@register_stage("execute")
def _execute(dsl: AssistantDSLv1, deps: RuntimeDeps) -> Node:
    """Run the routed target. For ``assistant``: knowledge retrieval, then the model."""

    def node(state: RuntimeState) -> RuntimeState:
        writer = get_stream_writer()
        update: RuntimeState = {}
        with deps.clock.external():
            result = deps.retriever.retrieve(
                tenant_id=deps.tenant_id,
                query=state["input"],
                dataset_ids=deps.allowed_dataset_ids(dsl.knowledge.dataset_ids),
                top_k=dsl.knowledge.top_k,
            )
        chunks = [chunk for chunk in result.chunks if chunk.score >= dsl.knowledge.min_score]
        update["retrieval_mode"] = result.mode
        update["context"] = [
            {"document_name": c.document_name, "chunk_id": c.chunk_id, "text": c.text, "score": c.score}
            for c in chunks
        ]

        messages: list[BaseMessage] = [SystemMessage(content=_system_prompt(dsl, {**state, **update}))]
        for turn in state.get("history", []):
            role = turn.get("role")
            if role == "user":
                messages.append(HumanMessage(content=turn["content"]))
            elif role == "assistant":
                messages.append(AIMessageChunk(content=turn["content"]))
        messages.append(HumanMessage(content=state["input"]))

        parts: list[str] = []
        usage: dict[str, int | None] = {"input": None, "output": None}
        with deps.clock.external():
            for chunk in deps.llm.stream(messages, config={"callbacks": deps.callbacks}):
                deps.check_stop()
                text = chunk.content if isinstance(chunk.content, str) else ""
                if text:
                    parts.append(text)
                    writer({"type": "token", "text": text})
                meta = getattr(chunk, "usage_metadata", None)
                if meta:
                    usage = {"input": meta.get("input_tokens"), "output": meta.get("output_tokens")}
        update["text"] = "".join(parts)
        update["usage"] = usage
        return update

    return node


@register_stage("egress")
def _egress(dsl: AssistantDSLv1, deps: RuntimeDeps) -> Node:
    return lambda state: {"text": (state.get("text") or "").strip()}


def _instrumented(name: str, node: Node, deps: RuntimeDeps) -> Node:
    """Wrap a stage with step events, a tracing span, timing and the failing stage's name."""

    def wrapped(state: RuntimeState) -> RuntimeState:
        writer = get_stream_writer()
        writer({"type": "step", "name": name, "status": "started"})
        started = time.monotonic()
        try:
            with deps.span(name):
                result = node(state)
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
