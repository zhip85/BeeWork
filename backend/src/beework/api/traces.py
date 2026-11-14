"""Call traces (T-084, OPS-040/041): a list of runs and the waterfall of one run. Tenant-scoped; platform administrators get no route here (PER-011)."""

import re
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from beework import traces
from beework.api.assistants import _visible_statement
from beework.api.conventions import ApiError, Page, Pagination, get_pagination
from beework.config import settings
from beework.db import get_db
from beework.models import Membership
from beework.models.assistants import Assistant
from beework.models.conversations import Conversation
from beework.models.runtime import Run
from beework.rbac import require_tenant_permission
from beework.schemas.traces import TraceDetailResponse, TraceResponse

router = APIRouter()
_HEX32 = re.compile(r"^[0-9a-f]{32}$")


def _base(membership: Membership, db: Session):
    """Runs the caller may see: their tenant, assistants within their scope, not an employee's private portal conversation."""
    statement = (
        select(Run, Assistant, Conversation)
        .join(Assistant, (Assistant.id == Run.assistant_id) & (Assistant.tenant_id == Run.tenant_id))
        .outerjoin(Conversation, Conversation.id == Run.conversation_id)
        .where(Run.tenant_id == membership.tenant_id, Assistant.deleted_at.is_(None))
        .where(or_(Conversation.id.is_(None), Conversation.source != "portal"))
    )
    return _visible_statement(statement, membership, db)


def _response(run: Run, assistant: Assistant, conversation: Conversation | None) -> dict:
    return {
        "run_id": run.id, "trace_id": traces.trace_id_of(run), "assistant_id": run.assistant_id, "assistant_name": assistant.name,
        "conversation_id": run.conversation_id, "origin": traces.origin_of(run.source), "source": run.source,
        "channel": conversation.source if conversation is not None else None, "environment": run.environment,
        "status": traces.status_of(run), "latency_ms": run.latency_ms, "tokens_in": run.tokens_in, "tokens_out": run.tokens_out,
        "error_code": run.error_code, "trace_available": bool(run.trace_url), "created_at": run.created_at,
    }


def _uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


@router.get("/traces", response_model=Page[TraceResponse])
def list_traces(
    assistant_id: UUID | None = None,
    channel: str | None = Query(default=None, max_length=24),
    status: str | None = Query(default=None, pattern="^(success|error|slow)$"),
    origin: str | None = Query(default=None, pattern="^(online|debug|trial|eval)$"),
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    min_latency_ms: int | None = Query(default=None, ge=0),
    q: str | None = Query(default=None, max_length=64, description="Exact trace id, run id or conversation id"),
    pagination: Pagination = Depends(get_pagination),
    membership: Membership = Depends(require_tenant_permission("traces:view")),
    db: Session = Depends(get_db),
) -> Page[TraceResponse]:
    statement = _base(membership, db)
    if assistant_id:
        statement = statement.where(Run.assistant_id == assistant_id)
    if channel:
        statement = statement.where(Conversation.source == channel)
    if status == "error":
        statement = statement.where(Run.status.in_(("failed", "cancelled")))
    elif status == "slow":
        statement = statement.where(Run.status == "succeeded", Run.latency_ms >= settings.trace_slow_ms)
    elif status == "success":
        statement = statement.where(Run.status == "succeeded", or_(Run.latency_ms.is_(None), Run.latency_ms < settings.trace_slow_ms))
    if origin:
        include, exclude = traces.sources_of(origin)
        statement = statement.where(Run.source.in_(include) if include else Run.source.not_in(exclude))
    if created_from:
        statement = statement.where(Run.created_at >= created_from)
    if created_to:
        statement = statement.where(Run.created_at <= created_to)
    if min_latency_ms is not None:
        statement = statement.where(Run.latency_ms >= min_latency_ms)
    if q:
        key = q.strip().lower()
        ident = None if _HEX32.match(key) else _uuid(key)  # 32 hex digits are a trace id, even though they would parse as a UUID
        if ident is not None:
            statement = statement.where(or_(Run.id == ident, Run.conversation_id == ident))
        elif _HEX32.match(key):
            statement = statement.where(Run.trace_url.is_not(None), cast(Run.trace_url, String).like(f"%/{key}"))
        else:
            statement = statement.where(False)
    page = pagination
    total = db.scalar(select(func.count()).select_from(statement.with_only_columns(Run.id).subquery())) or 0
    rows = db.execute(statement.order_by(Run.created_at.desc(), Run.id).offset(page.offset).limit(page.limit)).all()
    return Page[TraceResponse](
        items=[TraceResponse(**_response(run, assistant, conversation)) for run, assistant, conversation in rows],
        total=total, limit=page.limit, offset=page.offset,
    )


@router.get("/traces/{run_id}", response_model=TraceDetailResponse)
def get_trace(
    run_id: UUID,
    membership: Membership = Depends(require_tenant_permission("traces:view")),
    db: Session = Depends(get_db),
) -> TraceDetailResponse:
    row = db.execute(_base(membership, db).where(Run.id == run_id)).first()
    if row is None:
        raise ApiError(404, "not_found", "未找到指定调用")
    run, assistant, conversation = row
    spans, source, notice = traces.load_spans(run)
    return TraceDetailResponse(
        **_response(run, assistant, conversation), spans=spans, spans_source=source, notice=notice,
        timeline=traces.timeline_of(db, run),
    )
