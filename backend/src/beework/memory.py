"""Long-term end-user memory (T-056a): write after a conversation ends, read into the ``memory`` stage.

Retrieval is behind ``MemoryRetriever``. v1 ranks PostgreSQL-filtered candidates by keyword overlap
(word + CJK character bigrams) times a time decay, in the application: PostgreSQL's built-in full-text
search cannot segment Chinese. It never uses a vector store and nothing is written to RAGFlow.
Everything is keyed by tenant + end user, so memory never crosses tenants or users.
"""

import json
import logging
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from langchain_core.language_models import BaseChatModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from beework.models import AuditEvent
from beework.models.conversations import Conversation, Message
from beework.models.memory import EndUserMemory
from beework.models.runtime import Run
from beework.pii import redact_text
from beework.runtime.oc import OCConfig

logger = logging.getLogger(__name__)

FILE_LABELS = {"profile": "UserProfile", "memory": "UserMemory", "session": "Session"}
INJECTION_ORDER = ("profile", "memory", "session")  # PRD 3.6.4 priority order
CANDIDATE_LIMIT = 200
DECAY_HALF_LIFE_DAYS = 30.0
RECENCY_BASELINE = 0.2  # score of an entry that shares no word with the query: recency alone orders those
MAX_ENTRY_CHARS = 500
MAX_ENTRIES_PER_FILE = 10


@dataclass(frozen=True)
class MemoryItem:
    file: str
    text: str
    score: float


class MemoryRetriever(Protocol):
    def retrieve(
        self,
        db: Session,
        *,
        tenant_id: UUID,
        end_user_id: UUID,
        assistant_id: UUID,
        file: str,
        query: str,
        top_k: int,
        now: datetime,
    ) -> list[MemoryItem]: ...


_WORD = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[一-鿿]+")


def _terms(text: str) -> set[str]:
    lowered = text.lower()
    terms = set(_WORD.findall(lowered))
    for run in _CJK.findall(lowered):
        terms.update(run[i : i + 2] for i in range(len(run) - 1))
        if len(run) == 1:
            terms.add(run)
    return terms


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class KeywordMemoryRetriever:
    """Keyword-overlap relevance with exponential time decay; entries without overlap still rank by recency."""

    def retrieve(
        self,
        db: Session,
        *,
        tenant_id: UUID,
        end_user_id: UUID,
        assistant_id: UUID,
        file: str,
        query: str,
        top_k: int,
        now: datetime,
    ) -> list[MemoryItem]:
        statement = select(EndUserMemory).where(
            EndUserMemory.tenant_id == tenant_id,
            EndUserMemory.end_user_id == end_user_id,
            EndUserMemory.file == file,
        )
        if file == "profile":
            statement = statement.where(EndUserMemory.assistant_id.is_(None))  # shared across assistants
        else:
            statement = statement.where(EndUserMemory.assistant_id == assistant_id)  # assistant private
        rows = list(
            db.scalars(
                statement.order_by(EndUserMemory.created_at.desc()).limit(CANDIDATE_LIMIT)
            )
        )
        query_terms = _terms(query)
        items = []
        for row in rows:
            if row.expires_at is not None and _aware(row.expires_at) <= now:
                continue
            overlap = len(query_terms & _terms(row.content)) / (math.sqrt(len(query_terms)) or 1.0)
            age_days = max(0.0, (now - _aware(row.created_at)).total_seconds() / 86400)
            decay = 0.5 ** (age_days / DECAY_HALF_LIFE_DAYS)
            items.append(MemoryItem(file=file, text=row.content, score=(RECENCY_BASELINE + overlap) * decay))
        items.sort(key=lambda item: item.score, reverse=True)
        return items[:top_k]


def load_memory_context(
    db: Session,
    *,
    tenant_id: UUID,
    end_user_id: UUID | None,
    assistant_id: UUID,
    oc: OCConfig,
    query: str,
    retriever: MemoryRetriever | None = None,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    """Memory snippets to inject, in priority order, honouring the assistant's OC memory settings."""
    long_term = oc.memory.long_term
    if end_user_id is None or not long_term.enabled:
        return []
    retriever = retriever or KeywordMemoryRetriever()
    current = now or datetime.now(UTC)
    files = {
        "profile": long_term.user_profile if long_term.user_profile.subscribe_enabled else None,
        "memory": long_term.user_memory,
        "session": long_term.session,
    }
    context: list[dict[str, str]] = []
    for file in INJECTION_ORDER:
        settings = files[file]
        if settings is None or not settings.enabled:
            continue
        for item in retriever.retrieve(
            db,
            tenant_id=tenant_id,
            end_user_id=end_user_id,
            assistant_id=assistant_id,
            file=file,
            query=query,
            top_k=settings.top_k,
            now=current,
        ):
            context.append({"file": FILE_LABELS[file], "text": item.text})
    return context


# ---------------------------------------------------------------------------
# Writing.
# ---------------------------------------------------------------------------


def _clean(text: Any) -> str:
    return redact_text(str(text).strip())[:MAX_ENTRY_CHARS]  # memory is always redacted before it is stored


def _replace_entries(
    db: Session,
    conversation: Conversation,
    file: str,
    entries: Sequence[tuple[str | None, str]],
    *,
    assistant_id: UUID | None,
    retention_days: int,
    now: datetime,
) -> int:
    """Replace what this conversation previously wrote to ``file`` (idempotent re-extraction)."""
    db.execute(
        delete(EndUserMemory).where(
            EndUserMemory.tenant_id == conversation.tenant_id,
            EndUserMemory.source_conversation_id == conversation.id,
            EndUserMemory.file == file,
        )
    )
    written = 0
    for entry_type, text in entries[:MAX_ENTRIES_PER_FILE]:
        if not text:
            continue
        db.add(
            EndUserMemory(
                tenant_id=conversation.tenant_id,
                end_user_id=conversation.end_user_id,
                assistant_id=assistant_id,
                file=file,
                type=entry_type,
                content=text,
                source_conversation_id=conversation.id,
                expires_at=now + timedelta(days=retention_days),
                created_at=now,
            )
        )
        written += 1
    return written


EXTRACTION_PROMPT = (
    "你是对话记忆抽取器。阅读下面的对话，只输出一个 JSON 对象，不要输出其他文字：\n"
    '{"user_memory": [{"type": "preference|history|fact", "text": "..."}],\n'
    ' "profile": [{"text": "..."}],\n'
    ' "session_summary": "..."}\n'
    "user_memory：关于用户的稳定偏好(preference)、场景行为历史(history)或临时事实(fact)；"
    "profile：用户的身份画像（称呼、角色、所在行业等，不要写入联系方式）；"
    "session_summary：本次会话的一两句摘要。没有内容的字段给空数组或空字符串。"
)


def _parse_extraction(raw: str) -> dict[str, Any]:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("记忆抽取结果不是 JSON")
    parsed = json.loads(raw[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("记忆抽取结果不是 JSON 对象")
    return parsed


def _transcript(db: Session, conversation: Conversation) -> str:
    messages = list(
        db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id, Message.role.in_(("user", "assistant", "human_agent")))
            .order_by(Message.seq.desc())
            .limit(40)
        )
    )
    return "\n".join(
        f"{'用户' if m.role == 'user' else '助理'}：{m.content[:500]}" for m in reversed(messages)
    )


def extract_conversation_memory(
    db: Session,
    conversation_id: UUID,
    *,
    llm_factory: Callable[[Session, Conversation, Run], BaseChatModel],
    now: datetime | None = None,
) -> dict[str, int]:
    """Extract memories from an ended conversation. Safe to run repeatedly: it replaces its own output."""
    current = now or datetime.now(UTC)
    conversation = db.get(Conversation, conversation_id)
    if conversation is None or conversation.end_user_id is None or conversation.turns < 1:
        return {}
    run = db.scalar(
        select(Run).where(Run.conversation_id == conversation.id).order_by(Run.created_at.desc()).limit(1)
    )
    if run is None:
        return {}
    oc = OCConfig.model_validate((run.config or {}).get("oc") or {})
    long_term, short_term = oc.memory.long_term, oc.memory.short_term
    if not long_term.enabled:
        return {}
    transcript = _transcript(db, conversation)
    if not transcript.strip():
        return {}

    llm = llm_factory(db, conversation, run)
    answer = llm.invoke([("system", EXTRACTION_PROMPT), ("human", transcript)])
    content = answer.content if isinstance(answer.content, str) else ""
    parsed = _parse_extraction(content)

    written: dict[str, int] = {}
    if short_term.enabled and long_term.user_memory.enabled:
        entries = [
            (str(item.get("type")), _clean(item.get("text", "")))
            for item in parsed.get("user_memory") or []
            if isinstance(item, dict) and item.get("type") in short_term.type_whitelist
        ]
        written["memory"] = _replace_entries(
            db, conversation, "memory", entries,
            assistant_id=conversation.assistant_id, retention_days=long_term.user_memory.retention_days, now=current,
        )
    if long_term.user_profile.enabled and long_term.user_profile.write_enabled:
        entries = [
            (None, _clean(item.get("text", "")))
            for item in parsed.get("profile") or []
            if isinstance(item, dict)
        ]
        written["profile"] = _replace_entries(
            db, conversation, "profile", entries,
            assistant_id=None, retention_days=long_term.user_profile.retention_days, now=current,
        )
    if long_term.session.enabled:
        summary = _clean(parsed.get("session_summary") or "")
        written["session"] = _replace_entries(
            db, conversation, "session", [(None, summary)] if summary else [],
            assistant_id=conversation.assistant_id, retention_days=long_term.session.retention_days, now=current,
        )
    db.commit()
    return written


# ---------------------------------------------------------------------------
# Deletion and retention.
# ---------------------------------------------------------------------------


def delete_end_user_memories(db: Session, tenant_id: UUID, end_user_id: UUID, *, actor_user_id: UUID | None = None) -> int:
    """Delete every memory of one end user (right to erasure, PIPL / GDPR). Returns the number removed."""
    result = db.execute(
        delete(EndUserMemory).where(EndUserMemory.tenant_id == tenant_id, EndUserMemory.end_user_id == end_user_id)
    )
    removed = result.rowcount or 0
    db.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="memory.user_deleted",
            details={"end_user_id": str(end_user_id), "entries": str(removed)},
        )
    )
    db.commit()
    return removed


def purge_expired_memories(db: Session, now: datetime | None = None) -> int:
    current = now or datetime.now(UTC)
    result = db.execute(
        delete(EndUserMemory).where(EndUserMemory.expires_at.is_not(None), EndUserMemory.expires_at <= current)
    )
    removed = result.rowcount or 0
    if removed:
        db.add(AuditEvent(action="memory.retention_purged", details={"entries": str(removed)}))
    db.commit()
    return removed
