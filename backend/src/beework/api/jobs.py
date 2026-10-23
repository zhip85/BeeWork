import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.api.conventions import (
    ApiError,
    Page,
    Pagination,
    get_pagination,
    paginate,
    require_platform_admin,
)
from beework.auth import get_tenant_membership, require_csrf
from beework.db import get_db
from beework.models import AuditEvent, Membership, User
from beework.models.jobs import Job
from beework.schemas.jobs import JobResponse
from beework.worker.events import run_event_tenant_id, stream_run_events
from beework.worker.queue import retry_job

router = APIRouter(tags=["jobs"])


@router.get("/platform/jobs", response_model=Page[JobResponse])
def list_jobs(
    status: str | None = Query(default=None, max_length=16),
    kind: str | None = Query(default=None, max_length=120),
    pagination: Pagination = Depends(get_pagination),
    _user: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> Page[JobResponse]:
    statement = select(Job)
    if status:
        statement = statement.where(Job.status == status)
    if kind:
        statement = statement.where(Job.kind == kind)
    items, total = paginate(db, statement.order_by(Job.created_at.desc(), Job.id), pagination)
    return Page[JobResponse](items=items, total=total, limit=pagination.limit, offset=pagination.offset)


@router.post("/platform/jobs/{job_id}/retry", response_model=JobResponse)
def retry_platform_job(
    job_id: UUID,
    _csrf: None = Depends(require_csrf),
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> JobResponse:
    job = db.get(Job, job_id)
    if job is None:
        raise ApiError(404, "not_found", "未找到指定任务")
    try:
        retry_job(db, job)
    except ValueError as exc:
        raise ApiError(409, "job.not_retryable", str(exc)) from exc
    db.add(AuditEvent(actor_user_id=user.id, action="job.retried", details={"job_id": str(job.id)}))
    db.commit()
    return JobResponse.model_validate(job)


@router.get("/runs/{run_id}/events")
def get_run_events(
    run_id: UUID,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    membership: Membership = Depends(get_tenant_membership),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """SSE replay + live stream of a run's events, scoped to the caller's tenant.

    A run whose events belong to another tenant (or that has none yet) is a 404,
    so run IDs cannot be probed across tenants. Producers must publish a first
    event (``queued``) in the same transaction that creates the run.
    """
    owner = run_event_tenant_id(db, run_id)
    if owner is None or owner != membership.tenant_id:
        raise ApiError(404, "not_found", "未找到指定运行")
    bind = db.get_bind()

    def sse():
        # The request-scoped session may be closed before streaming starts or while
        # it is still running, so the stream owns its session.
        with Session(bind=bind) as stream_db:
            for event in stream_run_events(stream_db, run_id, after_seq, heartbeat=True):
                if event is None:
                    yield ": keepalive\n\n"
                    continue
                yield f"id: {event['seq']}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
