import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from beework.models.jobs import Job

logger = logging.getLogger(__name__)

JobHandler = Callable[[Session, dict[str, Any]], None]
HANDLERS: dict[str, JobHandler] = {}
DEFAULT_LOCK_TIMEOUT = timedelta(minutes=15)


def job_handler(kind: str) -> Callable[[JobHandler], JobHandler]:
    def register(handler: JobHandler) -> JobHandler:
        if kind in HANDLERS:
            raise RuntimeError(f"Job handler already registered: {kind}")
        HANDLERS[kind] = handler
        return handler

    return register


def enqueue(
    db: Session,
    kind: str,
    payload: dict[str, Any],
    *,
    tenant_id: UUID | None = None,
    idempotency_key: str | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 5,
) -> Job:
    """Add a job to the caller's transaction without committing it.

    A job with an ``idempotency_key`` is inserted inside a savepoint: when another
    transaction wins the race for the same key, the existing job is returned
    instead of failing the caller's whole transaction.
    """
    if idempotency_key:
        existing = _job_by_key(db, idempotency_key)
        if existing is not None:
            return existing
    job = Job(
        kind=kind,
        payload=payload,
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        run_after=run_after or datetime.now(UTC),
        max_attempts=max_attempts,
    )
    if not idempotency_key:
        db.add(job)
        return job
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError:
        existing = _job_by_key(db, idempotency_key)
        if existing is None:
            raise
        return existing
    return job


def _job_by_key(db: Session, idempotency_key: str) -> Job | None:
    return db.scalar(select(Job).where(Job.idempotency_key == idempotency_key))


def reclaim_expired_jobs(
    db: Session,
    *,
    now: datetime | None = None,
    lock_timeout: timedelta = DEFAULT_LOCK_TIMEOUT,
) -> int:
    current_time = now or datetime.now(UTC)
    stale_before = current_time - lock_timeout
    stale_jobs = list(
        db.scalars(
            select(Job)
            .where(Job.status == "running", Job.locked_at.is_not(None), Job.locked_at < stale_before)
            .with_for_update(skip_locked=True)
        )
    )
    for job in stale_jobs:
        job.status = "queued"
        job.locked_by = None
        job.locked_at = None
        job.run_after = current_time
        job.last_error = "Worker lock timed out; job returned to queue"
    return len(stale_jobs)


def claim_jobs(
    db: Session,
    worker_id: str,
    *,
    limit: int = 10,
    now: datetime | None = None,
) -> list[Job]:
    current_time = now or datetime.now(UTC)
    statement: Select[tuple[Job]] = (
        select(Job)
        .where(Job.status == "queued", Job.run_after <= current_time)
        .order_by(Job.run_after, Job.created_at, Job.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    jobs = list(db.scalars(statement))
    for job in jobs:
        job.status = "running"
        job.locked_by = worker_id
        job.locked_at = current_time
        job.attempts += 1
    db.flush()
    return jobs


def retry_delay(attempts: int) -> timedelta:
    return timedelta(seconds=min(30 * (2 ** max(attempts - 1, 0)), 3600))


def execute_job(db: Session, job_id: UUID, *, now: datetime | None = None) -> Job | None:
    current_time = now or datetime.now(UTC)
    job = db.get(Job, job_id)
    if job is None or job.status != "running":
        return job
    handler = HANDLERS.get(job.kind)
    if handler is None:
        error = f"No handler registered for {job.kind}"
        _record_failure(db, job_id, error, current_time)
        return db.get(Job, job_id)
    try:
        handler(db, job.payload)
        job.status = "succeeded"
        job.finished_at = current_time
        job.locked_by = None
        job.locked_at = None
        job.last_error = None
        db.commit()
        return job
    except Exception as exc:
        logger.exception("Job %s (%s) failed", job.id, job.kind)
        db.rollback()
        _record_failure(db, job_id, str(exc), current_time)
        return db.get(Job, job_id)


def _record_failure(db: Session, job_id: UUID, error: str, current_time: datetime) -> None:
    job = db.get(Job, job_id)
    if job is None:
        return
    job.last_error = error[:2000]
    job.locked_by = None
    job.locked_at = None
    if job.attempts >= job.max_attempts:
        job.status = "dead"
        job.finished_at = current_time
    else:
        job.run_after = current_time + retry_delay(job.attempts)
        job.status = "queued"
    db.commit()


def retry_job(db: Session, job: Job, *, now: datetime | None = None) -> Job:
    if job.status not in {"failed", "dead", "succeeded"}:
        raise ValueError("Only completed or failed jobs can be retried")
    job.status = "queued"
    job.attempts = 0
    job.run_after = now or datetime.now(UTC)
    job.locked_by = None
    job.locked_at = None
    job.finished_at = None
    job.last_error = None
    return job


@job_handler("noop.echo")
def noop_echo(_db: Session, _payload: dict[str, Any]) -> None:
    return None
