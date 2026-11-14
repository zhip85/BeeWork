from datetime import UTC, datetime

from sqlalchemy.orm import Session

from beework.audit_maintenance import prune_expired_audit_events
from beework.conversations import end_inactive_conversations, purge_expired_conversations
from beework.login_security import release_expired_locks
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


@job_handler("traces.retention")
def traces_retention(db: Session, _payload: dict) -> None:
    from beework.traces import purge_expired_traces

    purge_expired_traces(db)


@job_handler("capabilities.mcp_health")
def capabilities_mcp_health(db: Session, payload: dict) -> None:
    from beework.capability_service import mcp_health_job

    mcp_health_job(db, payload)


@job_handler("channels.health")
def channels_health(db: Session, payload: dict) -> None:
    from beework.channel_health import channel_health_job

    channel_health_job(db, payload)


@job_handler("portal.reclaim_idle_seats")
def portal_reclaim_idle_seats(db: Session, _payload: dict) -> None:
    from beework.portal_seat_service import reclaim_idle_for_all_tenants

    reclaim_idle_for_all_tenants(db)


@job_handler("auth.release_locks")
def auth_release_locks(db: Session, _payload: dict) -> None:
    release_expired_locks(db)


def enqueue_scheduled_jobs(db: Session, *, now: datetime | None = None) -> None:
    current_time = now or datetime.now(UTC)
    window = current_time.date().isoformat()
    enqueue(db, "audit.retention", {}, idempotency_key=f"audit.retention:{window}")
    enqueue(db, "traces.retention", {}, idempotency_key=f"traces.retention:{window}")
    enqueue(db, "conversations.retention", {}, idempotency_key=f"conversations.retention:{window}")
    enqueue(db, "memory.retention", {}, idempotency_key=f"memory.retention:{window}")
    enqueue(db, "portal.reclaim_idle_seats", {}, idempotency_key=f"portal.reclaim_idle_seats:{window}")
    hour = f"{window}T{current_time.hour:02d}"
    enqueue(db, "tenants.auto_lifecycle", {}, idempotency_key=f"tenants.auto_lifecycle:{hour}")
    enqueue(db, "gateway.reconcile_access", {}, idempotency_key=f"gateway.reconcile_access:{hour}")
    enqueue(db, "knowledge.reconcile", {}, idempotency_key=f"knowledge.reconcile:{hour}")
    enqueue(db, "knowledge.expire_faqs", {}, idempotency_key=f"knowledge.expire_faqs:{hour}")
    five_minutes = f"{window}T{current_time.hour:02d}:{current_time.minute // 5 * 5:02d}"
    enqueue(db, "conversations.end_inactive", {}, idempotency_key=f"conversations.end_inactive:{five_minutes}")
    enqueue(db, "approvals.sweep", {}, idempotency_key=f"approvals.sweep:{five_minutes}")
    enqueue(db, "auth.release_locks", {}, idempotency_key=f"auth.release_locks:{five_minutes}")
    enqueue(db, "knowledge.sweep_documents", {}, idempotency_key=f"knowledge.sweep_documents:{five_minutes}")
    enqueue(db, "usage.sync", {}, idempotency_key=f"usage.sync:{five_minutes}")
    enqueue(db, "capabilities.mcp_health", {}, idempotency_key=f"capabilities.mcp_health:{five_minutes}")
    enqueue(db, "channels.health", {}, idempotency_key=f"channels.health:{five_minutes}")
    enqueue(db, "usage.month_rollover", {}, idempotency_key=f"usage.month_rollover:{hour}")
