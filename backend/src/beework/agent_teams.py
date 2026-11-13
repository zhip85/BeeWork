"""Multi-agent collaboration: one level of leaf agents (T-066, 演进 PRD 6.7, 15.2).

A main agent with collaboration switched on may mount other agents. A mounted agent must be a *leaf*: published (a live
version exists), enabled, and neither collaborating itself nor mounting agents — so there are never two levels. At run time a
leaf is a tool in the main agent's tool-calling loop (T-061); it answers from its **frozen live version**, never its draft, with
its own knowledge and tools at call depth 2. Its input and output stay inside the main agent's pipeline: the task text the main
agent hands over and the answer it gets back pass the main agent's output rules like any tool result.
"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.assistant_versions import live_version
from beework.capabilities import CANDIDATE_LISTERS, MOUNT_RESOLVERS, MountItem
from beework.models.assistants import Assistant
from beework.tools.executor import PROVIDERS, ToolContext, ToolError, ToolOutput, ToolSpec

REASON_TEXT = {
    "not_published": "尚未发布",
    "disabled": "已停用",
    "not_leaf": "不是叶子智能体（它开启了多智能体协作或挂载了智能体）",
}

# (db, ctx, assistant, leaf dsl) -> chat model; tests replace it, production goes through the tenant's gateway key.
LeafModelFactory = Callable[[Session, ToolContext, Assistant, Any], Any]
LEAF_MODEL_FACTORY: list[LeafModelFactory] = []


def is_team(config: dict | None) -> bool:
    """Collaboration on, or agents mounted: either makes an agent a main agent, not a leaf."""
    config = config or {}
    return bool((config.get("multi_agent") or {}).get("enabled")) or bool((config.get("mounts") or {}).get("agent"))


def leaf_state(db: Session, assistant: Assistant | None) -> tuple[str, str | None]:
    """(state, reason): ``available`` when the agent can be called as a leaf, else ``disabled`` with why; ``deleted``
    for a missing or deleted one."""
    if assistant is None or assistant.deleted_at is not None:
        return "deleted", None
    if assistant.status == "disabled":
        return "disabled", "disabled"
    version = live_version(db, assistant)
    if version is None:
        return "disabled", "not_published"
    if is_team(assistant.draft_config) or is_team((version.snapshot or {}).get("config")):
        return "disabled", "not_leaf"
    return "available", None


def _agents(db: Session, tenant_id: UUID, ids: list[UUID]) -> dict[UUID, Assistant]:
    if not ids:
        return {}
    return {
        a.id: a for a in db.scalars(select(Assistant).where(Assistant.tenant_id == tenant_id, Assistant.id.in_(ids), Assistant.deleted_at.is_(None)))
    }


def resolve_agent_mounts(db: Session, tenant_id: UUID, ids: list[UUID]) -> dict[UUID, MountItem]:
    items: dict[UUID, MountItem] = {}
    for assistant_id, assistant in _agents(db, tenant_id, ids).items():
        state, reason = leaf_state(db, assistant)
        items[assistant_id] = MountItem(
            id=assistant_id, kind="agent", name=assistant.name, state=state, meta={"reason": reason, "description": assistant.description},  # type: ignore[arg-type]
        )
    return items


def list_agent_candidates(db: Session, membership: Any, q: str | None, limit: int) -> list[dict[str, Any]]:
    """Leaf agents the caller may see and that can be mounted now (RUL-010)."""
    from beework.api.assistants import (
        _visible_statement,  # lazy: the API package imports this module's neighbours
    )

    statement = _visible_statement(
        select(Assistant).where(Assistant.tenant_id == membership.tenant_id, Assistant.deleted_at.is_(None)), membership, db
    )
    if q and q.strip():
        statement = statement.where(Assistant.name.ilike(f"%{q.strip()}%"))
    items: list[dict[str, Any]] = []
    for assistant in db.scalars(statement.order_by(Assistant.name, Assistant.id)):
        if leaf_state(db, assistant)[0] != "available":
            continue
        items.append({"id": assistant.id, "kind": "agent", "name": assistant.name, "meta": {"description": assistant.description}})
        if len(items) >= limit:
            break
    return items


MOUNT_RESOLVERS["agent"] = resolve_agent_mounts
CANDIDATE_LISTERS["agent"] = list_agent_candidates


# ---------------------------------------------------------------------------
# The leaf as a tool.
# ---------------------------------------------------------------------------


def tool_name(assistant_id: UUID) -> str:
    return f"agent:{assistant_id}"


def _spec(assistant: Assistant) -> ToolSpec:
    return ToolSpec(
        name=tool_name(assistant.id), source="agent", capability_id=assistant.id, writes=False,
        parameters={
            "type": "object", "properties": {"task": {"type": "string", "description": "交给该智能体处理的任务，写清楚背景与期望"}},
            "required": ["task"], "additionalProperties": False,
        },
        description=f"委派给智能体「{assistant.name}」：{assistant.description}"[:300],
    )


def agent_tool_specs(db: Session, tenant_id: UUID, ids: list[UUID]) -> list[ToolSpec]:
    return [_spec(a) for a in _agents(db, tenant_id, ids).values() if leaf_state(db, a)[0] == "available"]


class AgentProvider:
    """``agent:{assistant id}`` — a leaf agent called by a main agent."""

    def lookup(self, db: Session, ctx: ToolContext, name: str) -> ToolSpec | None:
        try:
            assistant_id = UUID(name.split(":", 1)[1])
        except (IndexError, ValueError):
            return None
        assistant = _agents(db, ctx.tenant_id, [assistant_id]).get(assistant_id)
        if assistant is None or leaf_state(db, assistant)[0] != "available":
            return None
        return _spec(assistant)

    def execute(self, db: Session, ctx: ToolContext, spec: ToolSpec, args: dict[str, Any]) -> ToolOutput:
        from beework.runtime.compiler import RuntimeDeps, StageClock, run_agent
        from beework.runtime.dsl import AssistantDSLv1
        from beework.runtime.retrieval import EngineRetriever
        from beework.runtime.tool_runtime import ToolRuntime

        assistant = db.get(Assistant, spec.capability_id)
        version = live_version(db, assistant) if assistant is not None else None
        if assistant is None or version is None:
            raise ToolError("tool.agent_unavailable", "该智能体当前不可用")
        dsl = AssistantDSLv1.model_validate((version.snapshot or {})["dsl"])  # the frozen live version, never the draft
        task = str(args.get("task") or "").strip()
        if not task:
            raise ToolError("tool.invalid_arguments", "task 不能为空")
        if len(task) > dsl.limits.max_input_chars:
            raise ToolError("tool.invalid_arguments", f"task 过长（上限 {dsl.limits.max_input_chars} 字符）")
        model = (LEAF_MODEL_FACTORY[0] if LEAF_MODEL_FACTORY else _gateway_model)(db, ctx, assistant, dsl)
        from beework.knowledge_mounts import resolve_knowledge_bases

        tools = ToolRuntime(
            db.get_bind(), dsl, tenant_id=ctx.tenant_id, run_id=ctx.run_id, assistant_id=assistant.id, end_user=ctx.end_user,
            guard_output=ctx.guard_output, span=ctx.span, depth=ctx.depth + 1, allow_agents=False,  # a leaf never delegates further
            environment=ctx.environment, check_stop=ctx.check_stop, deadline=ctx.deadline,
        )
        deps = RuntimeDeps(
            llm=model, retriever=EngineRetriever(), tenant_id=ctx.tenant_id,
            resolve_bases=lambda ids: resolve_knowledge_bases(db, ctx.tenant_id, list(ids)),
            check_stop=ctx.check_stop, span=ctx.span, hold_output=True, clock=StageClock(), tools=tools if tools.offered else None,
        )
        with ctx.span(f"leaf.{assistant.name}"):
            result = run_agent(dsl, deps, {"input": task, "history": []}, lambda _event: None)
        text = (result.get("text") or "").strip()
        return ToolOutput(content=text or "（该智能体没有返回内容）", response_bytes=len(text.encode("utf-8")), request_bytes=len(task.encode("utf-8")))


def _gateway_model(db: Session, ctx: ToolContext, assistant: Assistant, dsl: Any):
    from beework.runtime.executor import gateway_llm_for

    metadata = {
        "tenant_id": str(ctx.tenant_id), "assistant_id": str(assistant.id), "source": "leaf", "environment": ctx.environment,
        "purpose": "leaf", **({"run_id": str(ctx.run_id)} if ctx.run_id else {}),
    }
    return gateway_llm_for(db, ctx.tenant_id, ctx.environment, dsl, metadata)


PROVIDERS["agent"] = AgentProvider()
