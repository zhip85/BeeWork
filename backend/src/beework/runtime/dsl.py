"""Assistant DSL v1: the versioned configuration the compiler turns into a graph.

It is built either from an assistant draft (debug runs) or, once T-024 exists, from a published
version snapshot. It holds data only; the compiler never executes anything stored in it.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from beework.integrations.litellm import model_alias
from beework.models.assistants import Assistant
from beework.runtime.errors import RuntimeFailure


class KnowledgeConfig(BaseModel):
    # RAGFlow dataset ids mounted on the assistant (T-033); empty until knowledge bases exist.
    dataset_ids: list[str] = Field(default_factory=list, max_length=20)
    top_k: int = Field(default=5, ge=1, le=20)
    min_score: float = Field(default=0.3, ge=0, le=1)


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


def dsl_from_draft(assistant: Assistant) -> AssistantDSLv1:
    """Build the DSL from an assistant's draft configuration."""
    config = assistant.draft_config or {}
    model_id = config.get("model_id")
    if not model_id:
        raise RuntimeFailure("runtime.model_unavailable", "助理尚未选择模型")
    prompt = (config.get("prompt") or "").strip()
    if not prompt:
        raise RuntimeFailure("runtime.prompt_missing", "助理尚未填写提示词")
    return AssistantDSLv1(
        prompt=prompt,
        model_alias=model_alias(UUID(str(model_id))),
        greeting=config.get("greeting") or "",
    )
