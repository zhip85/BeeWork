"""Long-term end-user memory (T-056a): write after a conversation ends, read into the ``memory`` stage.

Retrieval is behind ``MemoryRetriever``. v1 ranks PostgreSQL-filtered candidates by keyword overlap
(word + CJK character bigrams) times a time decay, in the application: PostgreSQL's built-in full-text
search cannot segment Chinese. It never uses a vector store and nothing is written to the knowledge engine.
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
from sqlalchemy import delete, false, func, select
from sqlalchemy.orm import Session

from beework.models import AuditEvent
from beework.models.conversations import Conversation, Message
from beework.models.memory import EndUserMemory
from beework.models.runtime import Run
from beework.pii import redact_text
from beework.runtime.oc import OCConfig

logger = logging.getLogger(__name__)

FILE_LABELS = {"profile": "UserProfile", "memory": "UserMemory", "tools": "Tools", "session": "Session"}
INJECTION_ORDER = ("profile", "memory", "tools", "session")  # PRD 3.6.4 priority order
MAX_TOOL_ENTRIES = 5
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


def memory_tier(db: Session, tenant_id: UUID) -> str:
    """What the tenant's plan allows (PLT-081): ``short`` (the conversation window only), ``short_long`` (also long-term memory), or
    ``all_cross_assistant`` (also a UserProfile shared by every assistant of the tenant)."""
    from beework.entitlements import entitlement_value

    value = entitlement_value(db, tenant_id, "memory")
    return value if value in ("short", "short_long", "all_cross_assistant") else "short"


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
        share_profile: bool = True,
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
        share_profile: bool = True,
    ) -> list[MemoryItem]:
        statement = select(EndUserMemory).where(
            EndUserMemory.tenant_id == tenant_id,
            EndUserMemory.end_user_id == end_user_id,
            EndUserMemory.file == file,
        )
        if file == "profile":
            # shared across assistants — but only while the plan allows sharing; an assistant's own profile entries are always its own
            statement = statement.where(
                (EndUserMemory.assistant_id == assistant_id) | (EndUserMemory.assistant_id.is_(None) if share_profile else false())
            )
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
    tier = memory_tier(db, tenant_id)
    if tier == "short":
        return []  # the standard plan has the conversation window only
    retriever = retriever or KeywordMemoryRetriever()
    current = now or datetime.now(UTC)
    files = {
        "profile": long_term.user_profile if long_term.user_profile.subscribe_enabled else None,
        "memory": long_term.user_memory,
        "tools": long_term.tools,
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
            **({"share_profile": tier == "all_cross_assistant"} if file == "profile" else {}),
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


RECONCILE_PROMPT = (
    "你是记忆整理器。下面有“已有记忆”（带编号）和“新抽取的候选”。逐条决定候选怎么处理，只输出一个 JSON 对象，不要输出其他文字：\n"
    '{"ops": [{"op": "add", "candidate": 1}, {"op": "update", "candidate": 2, "id": 3, "text": "合并后的完整表述"}, '
    '{"op": "none", "candidate": 3}, {"op": "delete", "id": 4}]}\n'
    "规则：\n"
    "- add：已有记忆里没有这件事，原样新增候选。\n"
    "- update：已有记忆说的是同一件事，但候选更新、更准确或更完整；给出合并后的完整文本，替换该条已有记忆。\n"
    "- none：已有记忆已经覆盖了候选，或候选不值得记。\n"
    "- delete：已有记忆被候选明确推翻且没有可合并的新说法时才删除（通常用 update 代替）。\n"
    "- 只在候选与已有记忆明确矛盾或重复时才 update / delete / none；已有记忆没被提到不代表过时，绝不要因此删除。\n"
    "- 每个候选必须恰有一条处理（add / update / none），delete 另外按需追加。\n"
    "- “意思相同”就算重复，措辞、语气词、标点不同不影响：已有“用户习惯晚上开会”，候选“用户一般在晚上开会” → none。\n"
    "- 同一件事有了新的状态：已有“用户住在北京”，候选“用户搬到上海了” → update，text 写“用户住在上海（原先在北京）”。"
)
MAX_EXISTING_FOR_RECONCILE = 20


def _existing_for_reconcile(
    db: Session, conversation: Conversation, file: str, candidate_texts: Sequence[str], *, assistant_id: UUID | None, now: datetime
) -> list[EndUserMemory]:
    """The user's current memories of one file that a new candidate could repeat or contradict: the most relevant ones plus the newest."""
    statement = select(EndUserMemory).where(
        EndUserMemory.tenant_id == conversation.tenant_id,
        EndUserMemory.end_user_id == conversation.end_user_id,
        EndUserMemory.file == file,
        EndUserMemory.assistant_id.is_(None) if assistant_id is None else EndUserMemory.assistant_id == assistant_id,
    )
    rows = [
        r for r in db.scalars(statement.order_by(EndUserMemory.created_at.desc()).limit(CANDIDATE_LIMIT))
        if r.expires_at is None or _aware(r.expires_at) > now
    ]
    wanted = _terms(" ".join(candidate_texts))
    rows.sort(key=lambda r: (len(wanted & _terms(r.content)), _aware(r.created_at)), reverse=True)
    return rows[:MAX_EXISTING_FOR_RECONCILE]


def _parse_ops(raw: str) -> list[dict[str, Any]]:
    parsed = _parse_extraction(raw)
    ops = parsed.get("ops")
    if not isinstance(ops, list):
        raise ValueError("记忆整理结果缺少 ops")
    return [op for op in ops if isinstance(op, dict)]


def _write_with_reconcile(
    db: Session,
    conversation: Conversation,
    file: str,
    candidates: Sequence[tuple[str | None, str]],
    *,
    llm: BaseChatModel,
    assistant_id: UUID | None,
    retention_days: int,
    now: datetime,
) -> int:
    """Write the candidates of one file after comparing them with what the user already has (T-056): a repeat is dropped, a newer
    statement of the same thing replaces the old entry, a contradicted entry is removed. Re-running for the same conversation gives the
    same result: its own earlier additions are removed first, and an update or delete already applied finds nothing left to change.
    If the comparison itself fails, the candidates are written as they are (the T-056a behaviour)."""
    texts = [text for _, text in candidates[:MAX_ENTRIES_PER_FILE] if text]
    db.execute(
        delete(EndUserMemory).where(
            EndUserMemory.tenant_id == conversation.tenant_id, EndUserMemory.source_conversation_id == conversation.id, EndUserMemory.file == file,
        )
    )
    if not texts:
        return 0
    existing = _existing_for_reconcile(db, conversation, file, texts, assistant_id=assistant_id, now=now)
    if not existing:
        return _replace_entries(db, conversation, file, [c for c in candidates if c[1]], assistant_id=assistant_id, retention_days=retention_days, now=now)
    listing = "\n".join(f"{i}. {row.content}" for i, row in enumerate(existing, start=1))
    wanted = "\n".join(f"{i}. {text}" for i, text in enumerate(texts, start=1))
    try:
        answer = llm.invoke([("system", RECONCILE_PROMPT), ("human", f"已有记忆：\n{listing}\n\n新抽取的候选：\n{wanted}")])
        ops = _parse_ops(answer.content if isinstance(answer.content, str) else "")
    except Exception:  # noqa: BLE001 — an unusable answer must not lose what was extracted
        logger.warning("Memory reconcile failed; writing the candidates as they are", exc_info=True)
        return _replace_entries(db, conversation, file, [c for c in candidates if c[1]], assistant_id=assistant_id, retention_days=retention_days, now=now)

    handled: set[int] = set()
    written = 0
    for op in ops[: MAX_ENTRIES_PER_FILE * 3]:
        kind = op.get("op")
        number = op.get("candidate")
        target = op.get("id")
        row = existing[target - 1] if isinstance(target, int) and 1 <= target <= len(existing) else None
        if kind == "delete" and row is not None:
            db.delete(row)
        elif kind == "update" and row is not None and isinstance(number, int) and 1 <= number <= len(texts):
            text = _clean(op.get("text") or texts[number - 1])
            if text:
                row.content = text
                row.expires_at = now + timedelta(days=retention_days)  # said again: the retention clock restarts
                handled.add(number)
        elif kind == "add" and isinstance(number, int) and 1 <= number <= len(texts) and number not in handled:
            entry_type = candidates[number - 1][0] if number - 1 < len(candidates) else None
            written += _replace_entries_append(db, conversation, file, entry_type, texts[number - 1], assistant_id=assistant_id, retention_days=retention_days, now=now)
            handled.add(number)
        elif kind == "none" and isinstance(number, int):
            handled.add(number)
    for number, text in enumerate(texts, start=1):  # a candidate the model did not mention is kept rather than lost
        if number not in handled:
            entry_type = candidates[number - 1][0] if number - 1 < len(candidates) else None
            written += _replace_entries_append(db, conversation, file, entry_type, text, assistant_id=assistant_id, retention_days=retention_days, now=now)
    return written


def _replace_entries_append(
    db: Session, conversation: Conversation, file: str, entry_type: str | None, text: str, *, assistant_id: UUID | None, retention_days: int, now: datetime
) -> int:
    db.add(
        EndUserMemory(
            tenant_id=conversation.tenant_id, end_user_id=conversation.end_user_id, assistant_id=assistant_id, file=file, type=entry_type,
            content=text, source_conversation_id=conversation.id, expires_at=now + timedelta(days=retention_days), created_at=now,
        )
    )
    return 1


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
    from beework.portal_profile_service import memory_allowed_for_end_user

    if not memory_allowed_for_end_user(db, conversation.tenant_id, conversation.end_user_id):
        return {}  # the administrator or the employee switched personal memory off: nothing is written (T-145)
    tier = memory_tier(db, conversation.tenant_id)
    if tier == "short":
        return {}  # long-term memory is not part of the tenant's plan
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
        written["memory"] = _write_with_reconcile(
            db, conversation, "memory", entries, llm=llm,
            assistant_id=conversation.assistant_id, retention_days=long_term.user_memory.retention_days, now=current,
        )
    if long_term.user_profile.enabled and long_term.user_profile.write_enabled:
        entries = [
            (None, _clean(item.get("text", "")))
            for item in parsed.get("profile") or []
            if isinstance(item, dict)
        ]
        written["profile"] = _write_with_reconcile(
            db, conversation, "profile", entries, llm=llm,
            # one profile for every assistant of the tenant only where the plan allows it; otherwise it belongs to this assistant
            assistant_id=None if tier == "all_cross_assistant" else conversation.assistant_id,
            retention_days=long_term.user_profile.retention_days, now=current,
        )
    if long_term.tools.enabled:
        written["tools"] = _write_tool_memory(db, conversation, assistant_id=conversation.assistant_id, retention_days=long_term.tools.retention_days, now=current)
    if long_term.session.enabled:
        summary = _clean(parsed.get("session_summary") or "")
        written["session"] = _replace_entries(
            db, conversation, "session", [(None, summary)] if summary else [],
            assistant_id=conversation.assistant_id, retention_days=long_term.session.retention_days, now=current,
        )
    db.commit()
    return written


def _write_tool_memory(db: Session, conversation: Conversation, *, assistant_id: UUID, retention_days: int, now: datetime) -> int:
    """The Tools file (OC-021): which tools the user's conversations called, counted from the call records (metadata only — no arguments
    or results). One entry per tool, refreshed when the tool is used again."""
    from beework.models.tools import ToolInvocation
    from beework.tools import policy as tool_policy

    run_ids = [r for r in db.scalars(select(Run.id).where(Run.conversation_id == conversation.id))]
    if not run_ids:
        return 0
    rows = db.execute(
        select(ToolInvocation.tool_name, func.count()).where(ToolInvocation.run_id.in_(run_ids), ToolInvocation.status == "ok")
        .group_by(ToolInvocation.tool_name).order_by(func.count().desc()).limit(MAX_TOOL_ENTRIES)
    ).all()
    written = 0
    for name, count in rows:
        entry = tool_policy.entry_for_tool(db, conversation.tenant_id, name)
        label = _clean(entry.name if entry is not None else name)[:80]
        text = f"用过工具“{label}”（最近一次会话调用 {count} 次）"
        existing = db.scalar(
            select(EndUserMemory).where(
                EndUserMemory.tenant_id == conversation.tenant_id, EndUserMemory.end_user_id == conversation.end_user_id,
                EndUserMemory.assistant_id == assistant_id, EndUserMemory.file == "tools", EndUserMemory.content.like(f"用过工具“{label}”%"),
            )
        )
        if existing is not None:
            existing.content, existing.expires_at, existing.source_conversation_id = text, now + timedelta(days=retention_days), conversation.id
        else:
            written += _replace_entries_append(db, conversation, "tools", None, text, assistant_id=assistant_id, retention_days=retention_days, now=now)
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
