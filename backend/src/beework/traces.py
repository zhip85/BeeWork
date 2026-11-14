"""Call traces (T-084, OPS-040–042, PER-011).

The list is built from BeeWork's own ``runs`` table; the waterfall of one run is read from Langfuse (v2 observations) on demand.
When Langfuse cannot be reached the detail still shows the run's own event timeline. Retention removes the trace in Langfuse and
clears ``runs.trace_url``; the run itself leaves with its conversation (``conversations.retention``).
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.compliance import get_policy
from beework.config import settings
from beework.entitlements import get_subscription
from beework.integrations.langfuse import (
    TracingError,
    delete_traces,
    fetch_observations,
    langfuse_configured,
)
from beework.models import AuditEvent, Tenant
from beework.models.jobs import RunEvent
from beework.models.runtime import Run
from beework.pii import redact_text
from beework.schemas.traces import SpanResponse, TimelineEvent

logger = logging.getLogger(__name__)

# 8.3#13: standard 7 / professional 30 / enterprise 90 days, until the product fixes the plan field.
DEFAULT_TRACE_RETENTION = {"standard": 7, "professional": 30, "enterprise": 90}
MAX_TEXT = 4000
ORIGINS = ("online", "debug", "trial", "eval")


def origin_of(source: str) -> str:
    """Where a run came from: a debug run, a published-version test run (试运行), an evaluation, or live traffic."""
    return {"debug": "debug", "test": "trial", "eval": "eval"}.get(source, "online")


def sources_of(origin: str) -> tuple[list[str], list[str]]:
    """``(in, not_in)`` run sources for an origin filter."""
    fixed = {"debug": ["debug"], "trial": ["test"], "eval": ["eval"]}
    if origin == "online":
        return [], [s for values in fixed.values() for s in values]
    return fixed[origin], []


def trace_id_of(run: Run) -> str:
    """The Langfuse trace id: kept at the end of ``trace_url``; derived from the run id (as the tracer does) when not stored."""
    if run.trace_url:
        tail = run.trace_url.rstrip("/").rsplit("/", 1)[-1]
        if len(tail) == 32:
            return tail
    from langfuse import Langfuse

    return Langfuse.create_trace_id(seed=str(run.id))


def status_of(run: Run) -> str:
    if run.status in ("queued", "running"):
        return "running"
    if run.status != "succeeded":
        return "error"
    return "slow" if (run.latency_ms or 0) >= settings.trace_slow_ms else "success"


def _text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return redact_text(text)[:MAX_TEXT]


def _ms(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def build_spans(observations: list[dict]) -> list[SpanResponse]:
    """The waterfall: spans in start order with offsets from the first one. Texts are redacted again on the way out."""
    starts = [t for t in (_ms(o.get("startTime")) for o in observations) if t is not None]
    if not starts:
        return []
    origin = min(starts)
    spans: list[SpanResponse] = []
    for item in sorted(observations, key=lambda o: (_ms(o.get("startTime")) or origin, o.get("id") or "")):
        start, end = _ms(item.get("startTime")) or origin, _ms(item.get("endTime"))
        usage = item.get("usageDetails") or {}
        cost = item.get("totalCost")
        spans.append(
            SpanResponse(
                id=str(item.get("id")), parent_id=item.get("parentObservationId"), name=str(item.get("name") or ""),
                kind=str(item.get("type") or "SPAN").lower(),
                start_ms=int((start - origin).total_seconds() * 1000),
                duration_ms=int((end - start).total_seconds() * 1000) if end else None,
                status="error" if str(item.get("level") or "").upper() == "ERROR" else "ok",
                message=_text(item.get("statusMessage")), model=item.get("model") or None,
                tokens_in=usage.get("input"), tokens_out=usage.get("output"),
                cost=float(cost) if cost else None, input=_text(item.get("input")), output=_text(item.get("output")),
            )
        )
    return spans


def timeline_of(db: Session, run: Run) -> list[TimelineEvent]:
    events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq)))
    base = run.created_at
    if base is not None and base.tzinfo is None:
        base = base.replace(tzinfo=UTC)

    def offset(event: RunEvent) -> int:
        at = event.created_at.replace(tzinfo=UTC) if event.created_at.tzinfo is None else event.created_at
        return max(0, int((at - base).total_seconds() * 1000)) if base is not None else 0

    return [TimelineEvent(seq=e.seq, type=e.type, offset_ms=offset(e)) for e in events]


def load_spans(run: Run) -> tuple[list[SpanResponse], str, str | None]:
    """``(spans, source, notice)``: Langfuse's view of the run, or why it is missing."""
    if not run.trace_url:
        return [], "unavailable", "该调用的追踪已超过保留期或未记录追踪，仅显示运行事件时间线"
    if not langfuse_configured():
        return [], "unavailable", "追踪服务未配置，仅显示运行事件时间线"
    try:
        return build_spans(fetch_observations(trace_id_of(run))), "langfuse", None
    except TracingError as exc:
        logger.warning("Trace %s could not be read: %s", run.id, exc)
        return [], "unavailable", "追踪服务暂时不可用，仅显示运行事件时间线"


# ---------------------------------------------------------------------------
# Retention (OPS-042).
# ---------------------------------------------------------------------------


def trace_retention_days(db: Session, tenant: Tenant) -> int:
    """The shorter of the plan's trace retention and the tenant's conversation retention."""
    plan = get_subscription(db, tenant.id).entitlements.get("trace_retention_days")
    days = plan if isinstance(plan, int) and plan > 0 else DEFAULT_TRACE_RETENTION.get(tenant.plan, 7)
    return min(days, get_policy(db, tenant.id).retention_days)


def purge_expired_traces(db: Session, now: datetime | None = None, *, batch: int = 500) -> dict[UUID, int]:
    """Delete the Langfuse traces of runs older than each tenant's retention and clear their ``trace_url``.

    Only BeeWork's side is cleared once Langfuse confirmed the deletion; when it fails the runs keep their link and the next daily run
    tries again. Without a Langfuse configuration there is nothing to delete remotely, so only the links are cleared."""
    current = now or datetime.now(UTC)
    purged: dict[UUID, int] = {}
    for tenant in list(db.scalars(select(Tenant))):
        days = trace_retention_days(db, tenant)
        runs = list(
            db.scalars(
                select(Run)
                .where(Run.tenant_id == tenant.id, Run.trace_url.is_not(None), Run.created_at < current - timedelta(days=days))
                .order_by(Run.created_at)
                .limit(batch)
            )
        )
        if not runs:
            continue
        if langfuse_configured():
            try:
                delete_traces([trace_id_of(run) for run in runs])
            except TracingError as exc:
                logger.warning("Trace retention for tenant %s postponed: %s", tenant.id, exc)
                continue
        for run in runs:
            run.trace_url = None
        db.add(AuditEvent(tenant_id=tenant.id, action="trace.retention_purged", details={"traces": str(len(runs)), "retention_days": str(days)}))
        purged[tenant.id] = len(runs)
        db.commit()
    return purged
