"""Assistant DSL v1: the versioned configuration the compiler turns into a graph.

It is built either from an assistant draft (debug runs) or, once T-024 exists, from a published
version snapshot. It holds data only; the compiler never executes anything stored in it.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from beework.integrations.litellm import model_alias
from beework.models.assistants import Assistant
from beework.runtime.errors import RuntimeFailure
from beework.runtime.oc import OCConfig


class KnowledgeBaseRef(BaseModel):
    """A mounted knowledge base as it was when the DSL was built (a publish records the engine dataset ids then).

    At run time only ``kb_id`` is trusted: the tenant's current row decides which datasets are searched.
    """

    kb_id: UUID
    name: str = ""
    kb_type: Literal["standard", "faq"] = "standard"
    engine: str = "dify"
    dataset_id: str | None = None
    faq_dataset_id: str | None = None
    threshold: float = Field(default=0.62, ge=0, le=1)


class KnowledgeConfig(BaseModel):
    bases: list[KnowledgeBaseRef] = Field(default_factory=list, max_length=20)
    top_k: int = Field(default=5, ge=1, le=20)


class ToolMount(BaseModel):
    """A mounted capability whose tools the agent may call (演进 PRD 7.6). ``tools`` are the tool names ticked for this
    agent (empty: every tool the capability exposes); ``delegate`` says when to use it. At run time the tenant's
    current rows decide what is available — a capability deleted since is simply not offered."""

    kind: Literal["mcp", "skill", "agent", "chatflow", "flow", "api"]
    capability_id: UUID
    name: str = ""
    tools: list[str] = Field(default_factory=list, max_length=100)
    delegate: str = Field(default="", max_length=500)


class ToolsConfig(BaseModel):
    mounts: list[ToolMount] = Field(default_factory=list, max_length=50)


class LimitsConfig(BaseModel):
    max_tokens: int = Field(default=1024, ge=16, le=8192)
    max_iterations: int = Field(default=8, ge=1, le=20)  # tool-calling rounds per message
    timeout_seconds: int = Field(default=60, ge=5, le=300)
    max_input_chars: int = Field(default=4000, ge=1, le=20000)
    # Short-term memory: how many previous user/assistant messages are sent as context (T-056 refines).
    history_messages: int = Field(default=20, ge=0, le=100)


class AssistantDSLv1(BaseModel):
    dsl_version: Literal[1] = 1
    prompt: str = Field(max_length=8000)
    model_alias: str
    role: str = ""
    answer_style: Literal["concise", "balanced", "detailed"] = "balanced"
    temperature: float = Field(default=0.7, ge=0, le=2)
    fallback_model_alias: str | None = None
    fallback_model_id: str | None = None
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    greeting: str = ""
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    oc: OCConfig = Field(default_factory=OCConfig)


def dsl_from_draft(assistant: Assistant, db: Session | None = None) -> AssistantDSLv1:
    """Build the DSL from an assistant's draft configuration.

    ``db`` is needed to record mounted knowledge bases; callers that can mount any pass it.
    """
    config = assistant.draft_config or {}
    kb_ids = [UUID(str(value)) for value in (config.get("mounts") or {}).get("kb") or []]
    if kb_ids and db is None:
        raise ValueError("dsl_from_draft needs a database session to resolve mounted knowledge bases")
    model_id = config.get("model_id")
    if not model_id:
        raise RuntimeFailure("runtime.model_unavailable", "智能体尚未选择模型")
    prompt = (config.get("prompt") or "").strip()
    if not prompt:
        raise RuntimeFailure("runtime.prompt_missing", "智能体尚未填写提示词")
    fallback_id = config.get("fallback_model_id")
    max_tokens = config.get("max_output_tokens")
    return AssistantDSLv1(
        prompt=prompt,
        model_alias=model_alias(UUID(str(model_id))),
        role=(config.get("role") or "").strip(),
        answer_style=config.get("answer_style") or "balanced",
        temperature=config.get("temperature") if config.get("temperature") is not None else 0.7,
        fallback_model_alias=model_alias(UUID(str(fallback_id))) if fallback_id else None,
        fallback_model_id=str(fallback_id) if fallback_id else None,
        tools=ToolsConfig(mounts=_tool_mounts(db, assistant.tenant_id, config)),
        greeting=config.get("greeting") or "",
        knowledge=KnowledgeConfig(bases=_knowledge_refs(db, assistant.tenant_id, kb_ids) if kb_ids else []),
        limits=LimitsConfig(max_tokens=int(max_tokens)) if max_tokens else LimitsConfig(),
        oc=OCConfig.model_validate(config.get("oc") or {}),
    )


def _tool_mounts(db: Session | None, tenant_id: UUID, config: dict) -> list[ToolMount]:
    """The agent's tool-bearing mounts (MCP, Skill) with their ticked tools and delegation text (演进 PRD 7.6)."""
    mounts, settings = config.get("mounts") or {}, config.get("mount_settings") or {}
    names: dict[UUID, str] = {}
    if db is not None:
        from beework.capabilities import resolve_mounts

        names = {item.id: item.name for item in resolve_mounts(db, tenant_id, {k: mounts.get(k) or [] for k in ("mcp", "skill", "agent", "chatflow", "flow", "api")})}
    result: list[ToolMount] = []
    team = bool((config.get("multi_agent") or {}).get("enabled"))
    for kind in ("mcp", "skill", "agent", "chatflow", "flow", "api"):
        if kind == "agent" and not team:
            continue  # single-agent mode: nothing is delegated (T-066)
        for raw in mounts.get(kind) or []:
            capability_id = UUID(str(raw))
            entry = settings.get(str(capability_id)) or {}
            result.append(
                ToolMount(
                    kind=kind, capability_id=capability_id, name=names.get(capability_id, ""),
                    tools=list(entry.get("tools") or []), delegate=str(entry.get("delegate") or ""),
                )
            )
    return result


def _knowledge_refs(db: Session, tenant_id: UUID, kb_ids: list[UUID]) -> list[KnowledgeBaseRef]:
    from beework.knowledge_mounts import (
        resolve_knowledge_bases,  # imports this module; resolved lazily
    )

    return resolve_knowledge_bases(db, tenant_id, kb_ids)
