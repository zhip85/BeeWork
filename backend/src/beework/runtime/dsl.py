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


class LimitsConfig(BaseModel):
    max_tokens: int = Field(default=1024, ge=16, le=8192)
    timeout_seconds: int = Field(default=60, ge=5, le=300)
    max_input_chars: int = Field(default=4000, ge=1, le=20000)
    # Short-term memory: how many previous user/assistant messages are sent as context (T-056 refines).
    history_messages: int = Field(default=20, ge=0, le=100)


class AssistantDSLv1(BaseModel):
    dsl_version: Literal[1] = 1
    prompt: str = Field(max_length=8000)
    model_alias: str
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
    return AssistantDSLv1(
        prompt=prompt,
        model_alias=model_alias(UUID(str(model_id))),
        greeting=config.get("greeting") or "",
        knowledge=KnowledgeConfig(bases=_knowledge_refs(db, assistant.tenant_id, kb_ids) if kb_ids else []),
        oc=OCConfig.model_validate(config.get("oc") or {}),
    )


def _knowledge_refs(db: Session, tenant_id: UUID, kb_ids: list[UUID]) -> list[KnowledgeBaseRef]:
    from beework.knowledge_mounts import (
        resolve_knowledge_bases,  # imports this module; resolved lazily
    )

    return resolve_knowledge_bases(db, tenant_id, kb_ids)
