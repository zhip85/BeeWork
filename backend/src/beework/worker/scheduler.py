from datetime import UTC, datetime

from sqlalchemy.orm import Session

from beework.audit_maintenance import prune_expired_audit_events
from beework.worker.queue import enqueue, job_handler


@job_handler("audit.retention")
def audit_retention(db: Session, _payload: dict) -> None:
    prune_expired_audit_events(db)


def enqueue_scheduled_jobs(db: Session, *, now: datetime | None = None) -> None:
    current_time = now or datetime.now(UTC)
    window = current_time.date().isoformat()
    enqueue(db, "audit.retention", {}, idempotency_key=f"audit.retention:{window}")
