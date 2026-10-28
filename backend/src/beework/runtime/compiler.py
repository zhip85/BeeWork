"""Compile an AssistantDSLv1 into a LangGraph ``StateGraph``.

The v1 graph is linear: ``input_guard -> retrieve -> generate -> output``. Nodes come only from
``NODE_FACTORIES``; a DSL cannot add nodes or run code.
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TypedDict
from uuid import UUID

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, BaseMessage, HumanMessage, SystemMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from beework.runtime.dsl import AssistantDSLv1
from beework.runtime.errors import RuntimeFailure
from beework.runtime.retrieval import Retriever

PIPELINE: tuple[str, ...] = ("input_guard", "retrieve", "generate", "output")


class RuntimeState(TypedDict, total=False):
    input: str
    history: list[dict[str, str]]
    context: list[dict[str, Any]]
    retrieval_mode: str
    text: str
    usage: dict[str, int | None]


@dataclass
class RuntimeDeps:
    llm: BaseChatModel
    retriever: Retriever
    tenant_id: UUID
    # Dataset ids the tenant may query; the retrieve node never passes anything else on.
    allowed_dataset_ids: Callable[[Sequence[str]], list[str]]
    # Called between model chunks; raises RunCancelled / RunTimedOut to stop the run.
    check_stop: Callable[[], None] = lambda: None
    callbacks: list[Any] = field(default_factory=list)


Node = Callable[[RuntimeState], RuntimeState]


def _input_guard(dsl: AssistantDSLv1, deps: RuntimeDeps) -> Node:
    def node(state: RuntimeState) -> RuntimeState:
        text = (state.get("input") or "").strip()
        if not text:
            raise RuntimeFailure("runtime.input_rejected", "消息不能为空")
        if len(text) > dsl.limits.max_input_chars:
            raise RuntimeFailure("runtime.input_rejected", f"消息长度不能超过 {dsl.limits.max_input_chars} 个字符")
        return {"input": text}

    return node


def _retrieve(dsl: AssistantDSLv1, deps: RuntimeDeps) -> Node:
    def node(state: RuntimeState) -> RuntimeState:
        dataset_ids = deps.allowed_dataset_ids(dsl.knowledge.dataset_ids)
        result = deps.retriever.retrieve(
            tenant_id=deps.tenant_id,
            query=state["input"],
            dataset_ids=dataset_ids,
            top_k=dsl.knowledge.top_k,
        )
        chunks = [chunk for chunk in result.chunks if chunk.score >= dsl.knowledge.min_score]
        return {
            "retrieval_mode": result.mode,
            "context": [
                {"document_name": c.document_name, "chunk_id": c.chunk_id, "text": c.text, "score": c.score}
                for c in chunks
            ],
        }

    return node


def _system_prompt(dsl: AssistantDSLv1, context: list[dict[str, Any]]) -> str:
    if not context:
        return dsl.prompt
    snippets = "\n\n".join(f"[{i}] {c['document_name']}\n{c['text']}" for i, c in enumerate(context, 1))
    return f"{dsl.prompt}\n\n# 参考资料\n{snippets}"


def _generate(dsl: AssistantDSLv1, deps: RuntimeDeps) -> Node:
    def node(state: RuntimeState) -> RuntimeState:
        writer = get_stream_writer()
        messages: list[BaseMessage] = [SystemMessage(content=_system_prompt(dsl, state.get("context", [])))]
        for turn in state.get("history", []):
            role = turn.get("role")
            if role == "user":
                messages.append(HumanMessage(content=turn["content"]))
            elif role == "assistant":
                messages.append(AIMessageChunk(content=turn["content"]))
        messages.append(HumanMessage(content=state["input"]))

        parts: list[str] = []
        usage: dict[str, int | None] = {"input": None, "output": None}
        for chunk in deps.llm.stream(messages, config={"callbacks": deps.callbacks}):
            deps.check_stop()
            text = chunk.content if isinstance(chunk.content, str) else ""
            if text:
                parts.append(text)
                writer({"type": "token", "text": text})
            meta = getattr(chunk, "usage_metadata", None)
            if meta:
                usage = {"input": meta.get("input_tokens"), "output": meta.get("output_tokens")}
        return {"text": "".join(parts), "usage": usage}

    return node


def _output(dsl: AssistantDSLv1, deps: RuntimeDeps) -> Node:
    def node(state: RuntimeState) -> RuntimeState:
        return {"text": (state.get("text") or "").strip()}

    return node


NODE_FACTORIES: dict[str, Callable[[AssistantDSLv1, RuntimeDeps], Node]] = {
    "input_guard": _input_guard,
    "retrieve": _retrieve,
    "generate": _generate,
    "output": _output,
}


def _with_step_events(name: str, node: Node) -> Node:
    def wrapped(state: RuntimeState) -> RuntimeState:
        writer = get_stream_writer()
        writer({"type": "step", "name": name, "status": "started"})
        started = time.monotonic()
        result = node(state)
        data: dict[str, Any] = {
            "type": "step", "name": name, "status": "completed",
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        if name == "retrieve":
            data["retrieval_mode"] = result.get("retrieval_mode")
        writer(data)
        return result

    return wrapped


def compile_assistant(dsl: AssistantDSLv1, deps: RuntimeDeps):
    """Return the compiled graph for ``dsl``; every node is looked up in ``NODE_FACTORIES``."""
    builder = StateGraph(RuntimeState)
    previous = START
    for name in PIPELINE:
        builder.add_node(name, _with_step_events(name, NODE_FACTORIES[name](dsl, deps)))
        builder.add_edge(previous, name)
        previous = name
    builder.add_edge(previous, END)
    return builder.compile()
