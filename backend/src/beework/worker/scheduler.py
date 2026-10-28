from datetime import UTC, datetime

from sqlalchemy.orm import Session

from beework.audit_maintenance import prune_expired_audit_events
from beework.conversations import end_inactive_conversations, purge_expired_conversations
from beework.worker.queue import enqueue, job_handler


@job_handler("audit.retention")
def audit_retention(db: Session, _payload: dict) -> None:
    prune_expired_audit_events(db)


@job_handler("conversations.end_inactive")
def conversations_end_inactive(db: Session, _payload: dict) -> None:
    end_inactive_conversations(db)


@job_handler("conversations.retention")
def conversations_retention(db: Session, _payload: dict) -> None:
    purge_expired_conversations(db)


def enqueue_scheduled_jobs(db: Session, *, now: datetime | None = None) -> None:
    current_time = now or datetime.now(UTC)
    window = current_time.date().isoformat()
    enqueue(db, "audit.retention", {}, idempotency_key=f"audit.retention:{window}")
    enqueue(db, "conversations.retention", {}, idempotency_key=f"conversations.retention:{window}")
    enqueue(db, "memory.retention", {}, idempotency_key=f"memory.retention:{window}")
    five_minutes = f"{window}T{current_time.hour:02d}:{current_time.minute // 5 * 5:02d}"
    enqueue(db, "conversations.end_inactive", {}, idempotency_key=f"conversations.end_inactive:{five_minutes}")
