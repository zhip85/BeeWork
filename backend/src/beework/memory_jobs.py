"""Worker side of long-term memory: extraction when a conversation ends, and daily retention."""

import logging
from uuid import UUID

from langchain_core.language_models import BaseChatModel
from sqlalchemy.orm import Session

from beework import conversations
from beework.memory import extract_conversation_memory, purge_expired_memories
from beework.models.conversations import Conversation
from beework.models.runtime import Run
from beework.runtime.dsl import AssistantDSLv1
from beework.runtime.executor import gateway_llm_for
from beework.worker.queue import enqueue, job_handler

logger = logging.getLogger(__name__)


def enqueue_extraction(db: Session, conversation: Conversation) -> None:
    """Conversation-ended hook: queue extraction in the caller's transaction."""
    if conversation.end_user_id is None or conversation.turns < 1:
        return
    stamp = conversation.ended_at.isoformat() if conversation.ended_at else "open"
    enqueue(
        db,
        "memory.extract",
        {"conversation_id": str(conversation.id)},
        tenant_id=conversation.tenant_id,
        idempotency_key=f"memory.extract:{conversation.id}:{stamp}",
    )


if enqueue_extraction not in conversations.CONVERSATION_ENDED_HOOKS:
    conversations.CONVERSATION_ENDED_HOOKS.append(enqueue_extraction)


def gateway_extraction_llm(db: Session, conversation: Conversation, run: Run) -> BaseChatModel:
    """The conversation's own model, through the tenant's gateway key (same path as a normal run)."""
    dsl = AssistantDSLv1.model_validate(run.config)
    return gateway_llm_for(
        db, run.tenant_id, run.environment, dsl,
        {"tenant_id": str(run.tenant_id), "assistant_id": str(run.assistant_id), "source": "memory.extract",
         "conversation_id": str(conversation.id)},
    )


@job_handler("memory.extract")
def memory_extract(db: Session, payload: dict) -> None:
    # Failures (gateway down, unparsable output) raise and are retried with backoff; extraction is idempotent.
    extract_conversation_memory(db, UUID(payload["conversation_id"]), llm_factory=gateway_extraction_llm)


@job_handler("memory.retention")
def memory_retention(db: Session, _payload: dict) -> None:
    purge_expired_memories(db)
