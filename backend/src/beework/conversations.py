"""Conversation and message storage shared by the runtime, the API and the Worker (T-026)."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from beework.compliance import get_policy
from beework.models import AuditEvent, Tenant, User
from beework.models.assistants import Assistant
from beework.models.conversations import Conversation, EndUser, Message
from beework.models.jobs import RunEvent
from beework.models.runtime import Run
from beework.pii import redact_text

logger = logging.getLogger(__name__)

IDLE_AFTER = timedelta(minutes=30)  # PRD 8.3#18 (provisional): no message for 30 minutes ends a conversation
CONSOLE_CHANNEL = "console"

# Called when a conversation ends (e.g. T-056 queues memory extraction).
ConversationEndedHook = Callable[[Session, Conversation], None]
CONVERSATION_ENDED_HOOKS: list[ConversationEndedHook] = []


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def console_end_user(db: Session, tenant_id: UUID, user: User) -> EndUser:
    """The person testing in the console is an end user of channel ``console``."""
    existing = db.scalar(
        select(EndUser).where(
            EndUser.tenant_id == tenant_id,
            EndUser.channel_type == CONSOLE_CHANNEL,
            EndUser.external_id == str(user.id),
        )
    )
    if existing is not None:
        return existing
    end_user = EndUser(
        tenant_id=tenant_id, channel_type=CONSOLE_CHANNEL, external_id=str(user.id), name=user.display_name
    )
    db.add(end_user)
    db.flush()
    return end_user


def start_conversation(
    db: Session, *, assistant: Assistant, end_user: EndUser, source: str, environment: str, created_by: UUID | None
) -> Conversation:
    now = datetime.now(UTC)
    conversation = Conversation(
        tenant_id=assistant.tenant_id,
        assistant_id=assistant.id,
        assistant_version=f"draft:{assistant.draft_version}",
        environment=environment,
        source=source,
        end_user_id=end_user.id,
        end_user_name=end_user.name,
        created_by=created_by,
        started_at=now,
        last_active_at=now,
    )
    db.add(conversation)
    db.flush()
    return conversation


def store_text(db: Session, tenant_id: UUID, text: str) -> str:
    """Text as it must be persisted under the tenant's PII policy."""
    return redact_text(text) if get_policy(db, tenant_id).redact_pii else text


def append_message(
    db: Session,
    conversation: Conversation,
    role: str,
    content: str,
    *,
    run_id: UUID | None = None,
    trace_id: str | None = None,
) -> Message:
    """Add a message with the next ``seq``. The caller holds a lock on the conversation row."""
    seq = (db.scalar(select(func.max(Message.seq)).where(Message.conversation_id == conversation.id)) or 0) + 1
    message = Message(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        seq=seq,
        role=role,
        content=store_text(db, conversation.tenant_id, content),
        run_id=run_id,
        trace_id=trace_id,
    )
    db.add(message)
    conversation.last_active_at = datetime.now(UTC)
    if role == "user":
        conversation.turns += 1
    # A new message reopens a conversation that was ended for inactivity.
    conversation.status = "in_progress"
    conversation.ended_at = None
    db.flush()
    return message


def history_for_run(db: Session, run: Run, limit: int) -> list[dict[str, str]]:
    """The most recent ``limit`` user/assistant messages before this run's own user message."""
    if run.conversation_id is None or limit <= 0:
        return []
    own = db.scalar(
        select(Message.seq).where(
            Message.conversation_id == run.conversation_id, Message.run_id == run.id, Message.role == "user"
        )
    )
    statement = select(Message).where(
        Message.conversation_id == run.conversation_id,
        Message.role.in_(("user", "assistant", "human_agent")),
    )
    if own is not None:
        statement = statement.where(Message.seq < own)
    rows = list(db.scalars(statement.order_by(Message.seq.desc()).limit(limit)))
    return [
        {"role": "user" if message.role == "user" else "assistant", "content": message.content}
        for message in reversed(rows)
    ]


def end_conversation(db: Session, conversation: Conversation, now: datetime) -> None:
    """Mark ended. Answered conversations count as resolved, an unanswered last user message as abandoned."""
    last_role = db.scalar(
        select(Message.role).where(Message.conversation_id == conversation.id).order_by(Message.seq.desc()).limit(1)
    )
    conversation.status = "resolved" if last_role in {"assistant", "human_agent"} else "abandoned"
    conversation.ended_at = now
    for hook in CONVERSATION_ENDED_HOOKS:
        try:
            hook(db, conversation)
        except Exception:
            logger.exception("Conversation-ended hook failed for %s", conversation.id)


def end_inactive_conversations(db: Session, now: datetime | None = None, *, batch: int = 500) -> int:
    current = now or datetime.now(UTC)
    stale = list(
        db.scalars(
            select(Conversation)
            .where(Conversation.status == "in_progress", Conversation.last_active_at <= current - IDLE_AFTER)
            .order_by(Conversation.last_active_at)
            .limit(batch)
        )
    )
    # A conversation with a run still in flight is not idle, whatever its last message time says.
    busy = set(
        db.scalars(
            select(Run.conversation_id).where(
                Run.conversation_id.in_([c.id for c in stale]), Run.status.in_(("queued", "running"))
            )
        )
    ) if stale else set()
    ended = 0
    for conversation in stale:
        if conversation.id in busy:
            continue
        end_conversation(db, conversation, current)
        ended += 1
    db.commit()
    return ended


def purge_expired_conversations(db: Session, now: datetime | None = None, *, batch: int = 1000) -> dict[UUID, int]:
    """Delete conversations (messages, runs, run events) older than each tenant's retention; one audit row per tenant."""
    current = now or datetime.now(UTC)
    purged: dict[UUID, int] = {}
    for tenant_id in db.scalars(select(Tenant.id)):
        retention = get_policy(db, tenant_id).retention_days
        expired = list(
            db.scalars(
                select(Conversation.id)
                .where(Conversation.tenant_id == tenant_id, Conversation.last_active_at < current - timedelta(days=retention))
                .limit(batch)
            )
        )
        if not expired:
            continue
        run_ids = list(db.scalars(select(Run.id).where(Run.conversation_id.in_(expired))))
        if run_ids:
            db.execute(delete(RunEvent).where(RunEvent.run_id.in_(run_ids)))
            db.execute(delete(Run).where(Run.id.in_(run_ids)))
        db.execute(delete(Message).where(Message.conversation_id.in_(expired)))
        db.execute(delete(Conversation).where(Conversation.id.in_(expired)))
        db.add(
            AuditEvent(
                tenant_id=tenant_id,
                action="conversation.retention_purged",
                details={"conversations": str(len(expired)), "retention_days": str(retention)},
            )
        )
        purged[tenant_id] = len(expired)
    db.commit()
    return purged
