"""Usage metering: gateway spend logs → points, monthly budget and alerts (T-011, RUL-020/022, SET-080–083).

Metering facts come from the AI gateway (LiteLLM); BeeWork never counts tokens itself. The Worker pulls the
per-request spend logs every few minutes, attributes each call to a tenant/environment through the virtual key's
alias (``beework:<tenant>:<environment>``, set by BeeWork and not forgeable by a caller) and to an assistant/source
through the ``spend_logs_metadata`` the runtime sends with every call, then converts tokens to points with the
platform model catalog and adds them to the daily ``usage_records``.
"""

import calendar
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from math import ceil
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from beework.errors import ApiError
from beework.integrations.litellm import GatewayError, LiteLLMClient, gateway_configured
from beework.models import AuditEvent, PlatformModel
from beework.models.conversations import Conversation
from beework.models.usage import (
    UNKNOWN_ID,
    TenantBudget,
    UsageAlert,
    UsageRecord,
    UsageSeen,
    UsageSyncState,
)
from beework.notifications import notify
from beework.worker.queue import enqueue, job_handler

logger = logging.getLogger(__name__)

SYNC_NAME = "litellm_spend"
# Spend logs are written to the gateway database in batches, so a row can show up after the cursor has passed its
# start time. Every pull therefore re-reads this much history and relies on ``usage_seen`` to skip what it has counted.
REREAD_WINDOW = timedelta(minutes=30)
PAGE_SIZE = 200
MAX_PAGES_PER_PULL = 50
THRESHOLD_CHOICES = (50, 80, 100)
DEFAULT_BUDGET_POINTS = 50_000
DEFAULT_THRESHOLDS = [80, 100]
# Conversations that count against the monthly conversation quota: real users', not console debugging / tests.
NON_BILLABLE_CONVERSATION_SOURCES = ("debug", "test")

_ALIAS = re.compile(r"^beework:([0-9a-fA-F-]{36}):(development|staging|production)$")


def month_key(moment: datetime | date) -> str:
    return f"{moment.year:04d}-{moment.month:02d}"


def month_start(moment: datetime | date) -> date:
    return date(moment.year, moment.month, 1)


def next_month(day: date) -> date:
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def add_months(day: date, delta: int) -> date:
    index = day.year * 12 + day.month - 1 + delta
    return date(index // 12, index % 12 + 1, 1)


# ---------------------------------------------------------------------------
# Pulling spend logs.
# ---------------------------------------------------------------------------


@dataclass
class PullResult:
    rows: int = 0
    counted: int = 0
    duplicates: int = 0
    unattributed: int = 0
    unpriced: int = 0
    tenants: int = 0


def _uuid_or(value: object, default: UUID = UNKNOWN_ID) -> UUID:
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return default


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def points_for(model: PlatformModel | None, tokens_in: int, tokens_out: int) -> Decimal:
    """RUL-020: tokens_in/1000 × input points + tokens_out/1000 × output points (catalog prices)."""
    if model is None:
        return Decimal(0)
    return (
        Decimal(tokens_in) / 1000 * model.input_points_per_thousand
        + Decimal(tokens_out) / 1000 * model.output_points_per_thousand
    )


def _lock_usage_pull(db: Session) -> None:
    """Serialise concurrent pulls on PostgreSQL so two Workers cannot count the same request twice."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(select(func.pg_advisory_xact_lock(7011)))


def pull_usage(
    db: Session, client: LiteLLMClient, *, now: datetime | None = None
) -> PullResult:
    """Fetch spend logs since the cursor (minus the re-read window) and add the unseen ones to ``usage_records``.

    Idempotent: counting a request inserts its id into ``usage_seen`` in the same transaction, so pulling the same
    window again changes nothing.
    """
    now = now or datetime.now(UTC)
    _lock_usage_pull(db)
    state = db.get(UsageSyncState, SYNC_NAME)
    if state is None:
        state = UsageSyncState(name=SYNC_NAME)
        db.add(state)
        db.flush()
    cursor = state.cursor
    if cursor is not None and cursor.tzinfo is None:
        cursor = cursor.replace(tzinfo=UTC)
    start = (cursor - REREAD_WINDOW) if cursor else datetime.combine(month_start(now), datetime.min.time(), UTC)
    end = now + timedelta(hours=1)

    result = PullResult()
    models: dict[UUID, PlatformModel | None] = {}
    new_cursor = cursor
    touched: set[UUID] = set()
    page = 1
    while page <= MAX_PAGES_PER_PULL:
        rows, total_pages = client.spend_logs(start=start, end=end, page=page, page_size=PAGE_SIZE)
        _count_rows(db, rows, models, touched, result)
        for row in rows:
            started = _parse_time(row["startTime"]) if row.get("startTime") else None
            if started and (new_cursor is None or started > new_cursor):
                new_cursor = started
        if page >= total_pages or not rows:
            break
        page += 1
    state.cursor = new_cursor
    state.synced_at = now
    state.last_error = None
    result.tenants = len(touched)
    if new_cursor is not None:
        # A request the next pull can still return started after (cursor - window) and so was counted after it:
        # anything seen before that, minus an hour of clock skew between the gateway and this host, is safe to forget.
        db.execute(delete(UsageSeen).where(UsageSeen.seen_at < new_cursor - REREAD_WINDOW - timedelta(hours=1)))
    db.commit()
    for tenant_id in touched:
        evaluate_budget(db, tenant_id, now=now)
    return result


def _count_rows(
    db: Session,
    rows: list[dict],
    models: dict[UUID, PlatformModel | None],
    touched: set[UUID],
    result: PullResult,
) -> None:
    result.rows += len(rows)
    request_ids = [str(row["request_id"]) for row in rows if row.get("request_id")]
    already = set(db.scalars(select(UsageSeen.request_id).where(UsageSeen.request_id.in_(request_ids)))) if request_ids else set()
    buckets: dict[tuple, dict] = defaultdict(
        lambda: {"requests": 0, "tokens_in": 0, "tokens_out": 0, "points": Decimal(0), "spend": Decimal(0)}
    )
    for row in rows:
        request_id = str(row.get("request_id") or "")
        if not request_id:
            continue
        if request_id in already:
            result.duplicates += 1
            continue
        already.add(request_id)  # the same id twice within one pull
        tokens_in = int(row.get("prompt_tokens") or 0)
        tokens_out = int(row.get("completion_tokens") or 0)
        metadata = row.get("metadata") or {}
        match = _ALIAS.match(str(metadata.get("user_api_key_alias") or ""))
        db.add(UsageSeen(request_id=request_id))  # failed / empty calls are marked too, so they are not re-read forever
        if row.get("status") not in (None, "success") or (tokens_in + tokens_out) == 0:
            continue
        if match is None or not row.get("startTime"):
            result.unattributed += 1
            continue
        tenant_id, environment = UUID(match.group(1)), match.group(2)
        attribution = metadata.get("spend_logs_metadata") or {}
        model_id = _uuid_or(row.get("model_id"))
        if model_id not in models:
            models[model_id] = db.get(PlatformModel, model_id) if model_id != UNKNOWN_ID else None
        model = models[model_id]
        if model is None:
            result.unpriced += 1
        day = _parse_time(row["startTime"]).date()
        key = (
            tenant_id, day, environment, model_id, _uuid_or(attribution.get("assistant_id")),
            str(attribution.get("source") or "unknown")[:16],
        )
        bucket = buckets[key]
        bucket["requests"] += 1
        bucket["tokens_in"] += tokens_in
        bucket["tokens_out"] += tokens_out
        bucket["points"] += points_for(model, tokens_in, tokens_out)
        bucket["spend"] += Decimal(str(row.get("spend") or 0))
        result.counted += 1
        touched.add(tenant_id)
    db.flush()
    for (tenant_id, day, environment, model_id, assistant_id, source), bucket in buckets.items():
        record = db.scalar(
            select(UsageRecord).where(
                UsageRecord.tenant_id == tenant_id, UsageRecord.day == day, UsageRecord.environment == environment,
                UsageRecord.model_id == model_id, UsageRecord.assistant_id == assistant_id, UsageRecord.source == source,
            )
        )
        if record is None:
            record = UsageRecord(
                tenant_id=tenant_id, day=day, environment=environment, model_id=model_id,
                assistant_id=assistant_id, source=source, requests=0, tokens_in=0, tokens_out=0,
                points=Decimal(0), spend_usd=Decimal(0),
            )
            db.add(record)
        record.requests += bucket["requests"]
        record.tokens_in += bucket["tokens_in"]
        record.tokens_out += bucket["tokens_out"]
        record.points += bucket["points"]
        record.spend_usd += bucket["spend"]
        record.updated_at = datetime.now(UTC)
    db.flush()


def sync_usage(db: Session, *, now: datetime | None = None) -> PullResult | None:
    """Worker entry point: pull from the configured gateway; records the error on the state row and re-raises."""
    if not gateway_configured():
        return None
    try:
        with LiteLLMClient() as client:
            return pull_usage(db, client, now=now)
    except GatewayError as exc:
        db.rollback()
        state = db.get(UsageSyncState, SYNC_NAME)
        if state is None:
            state = UsageSyncState(name=SYNC_NAME)
            db.add(state)
        state.last_error = str(exc)[:500]
        db.commit()
        raise


@job_handler("usage.sync")
def usage_sync_job(db: Session, _payload: dict) -> None:
    result = sync_usage(db)
    if result is not None and result.counted:
        logger.info(
            "usage sync: %s rows, %s counted, %s duplicates, %s unattributed, %s unpriced",
            result.rows, result.counted, result.duplicates, result.unattributed, result.unpriced,
        )


# ---------------------------------------------------------------------------
# Reading usage.
# ---------------------------------------------------------------------------


def month_points_decimal(db: Session, tenant_id: UUID, month: date | None = None) -> Decimal:
    first = month_start(month or datetime.now(UTC))
    total = db.scalar(
        select(func.coalesce(func.sum(UsageRecord.points), 0)).where(
            UsageRecord.tenant_id == tenant_id, UsageRecord.day >= first, UsageRecord.day < next_month(first)
        )
    )
    return Decimal(total or 0)


def month_points(db: Session, tenant_id: UUID) -> int:
    """Points used this (UTC) month, rounded up: the unit quotas and budgets are compared in."""
    return ceil(month_points_decimal(db, tenant_id))


def month_conversations(db: Session, tenant_id: UUID) -> int:
    first = datetime.combine(month_start(datetime.now(UTC)), datetime.min.time(), UTC)
    return db.scalar(
        select(func.count()).select_from(Conversation).where(
            Conversation.tenant_id == tenant_id,
            Conversation.started_at >= first,
            Conversation.source.not_in(NON_BILLABLE_CONVERSATION_SOURCES),
        )
    ) or 0


def project_month_end(used: Decimal, now: datetime) -> int:
    """Linear extrapolation of this month's points from the elapsed part of the month (SET-080)."""
    first = datetime.combine(month_start(now), datetime.min.time(), UTC)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    elapsed_days = max((now - first).total_seconds() / 86400, 1.0)  # day one would otherwise extrapolate wildly
    return ceil(used / Decimal(str(elapsed_days)) * days_in_month)


# ---------------------------------------------------------------------------
# Budget, alerts and the non-production pause (RUL-022, SET-082/083).
# ---------------------------------------------------------------------------


def get_budget(db: Session, tenant_id: UUID) -> TenantBudget:
    budget = db.get(TenantBudget, tenant_id)
    if budget is None:
        try:
            with db.begin_nested():
                budget = TenantBudget(
                    tenant_id=tenant_id, monthly_points=DEFAULT_BUDGET_POINTS,
                    thresholds=list(DEFAULT_THRESHOLDS), recipients=[], pause_non_production=False,
                )
                db.add(budget)
                db.flush()
        except IntegrityError:
            budget = db.get(TenantBudget, tenant_id)
    return budget


def usage_paused(db: Session, tenant_id: UUID, *, now: datetime | None = None) -> bool:
    """Is non-production traffic paused because this month's budget reached 100%?"""
    budget = db.get(TenantBudget, tenant_id)
    return bool(
        budget is not None and budget.pause_non_production and budget.paused_month == month_key(now or datetime.now(UTC))
    )


def request_access_sync(db: Session, tenant_id: UUID) -> None:
    """Queue the gateway key block / unblock; retried like the lifecycle ones so a pause is never silently lost."""
    from beework.gateway_sync import ACCESS_JOB_ATTEMPTS

    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    enqueue(
        db, "gateway.sync_tenant_access", {"tenant_id": str(tenant_id)}, tenant_id=tenant_id,
        idempotency_key=f"gateway.access:{tenant_id}:usage:{stamp}", max_attempts=ACCESS_JOB_ATTEMPTS,
    )


def evaluate_budget(db: Session, tenant_id: UUID, *, now: datetime | None = None) -> list[int]:
    """Fire each configured threshold at most once per month and apply the 100% pause. Returns the thresholds fired."""
    now = now or datetime.now(UTC)
    budget = get_budget(db, tenant_id)  # a tenant that never opened the page still has the default budget and alerts
    if budget.monthly_points < 1:
        return []
    used = month_points_decimal(db, tenant_id, now.date())
    percent = used / budget.monthly_points * 100
    month = month_key(now)
    fired: list[int] = []
    for threshold in sorted(set(budget.thresholds or [])):
        if percent < threshold:
            continue
        try:
            with db.begin_nested():
                db.add(UsageAlert(tenant_id=tenant_id, month=month, threshold=threshold))
                db.flush()
        except IntegrityError:
            continue  # already notified this month
        fired.append(threshold)
        notify(
            db, tenant_id=tenant_id, type="usage.threshold",
            title=f"本月点数用量已达预算的 {threshold}%",
            body=f"已使用 {ceil(used):,} / {budget.monthly_points:,} 点。",
            link="/settings/usage",
            params={"threshold": threshold, "used": ceil(used), "budget": budget.monthly_points},
            role="tenant_admin",
        )
    if percent >= 100 and budget.pause_non_production and budget.paused_month != month:
        budget.paused_month = month
        db.add(
            AuditEvent(
                tenant_id=tenant_id, actor_user_id=None, action="usage.non_production_paused",
                details={"month": month, "used": str(ceil(used)), "budget": str(budget.monthly_points)},
            )
        )
        request_access_sync(db, tenant_id)
    db.commit()
    return fired


def apply_budget_change(db: Session, tenant_id: UUID, *, now: datetime | None = None) -> None:
    """After a budget edit: lift a pause that no longer applies, then re-evaluate thresholds."""
    now = now or datetime.now(UTC)
    budget = get_budget(db, tenant_id)
    if budget.paused_month is not None:
        percent = month_points_decimal(db, tenant_id, now.date()) / budget.monthly_points * 100
        if not budget.pause_non_production or percent < 100 or budget.paused_month != month_key(now):
            budget.paused_month = None
            request_access_sync(db, tenant_id)
    db.commit()
    evaluate_budget(db, tenant_id, now=now)


@job_handler("usage.month_rollover")
def month_rollover_job(db: Session, _payload: dict) -> None:
    """Lift last month's pauses on the first day of a month (the gateway key unblock rides the access sync)."""
    current = month_key(datetime.now(UTC))
    for budget in db.scalars(select(TenantBudget).where(TenantBudget.paused_month.is_not(None), TenantBudget.paused_month != current)):
        budget.paused_month = None
        request_access_sync(db, budget.tenant_id)
    db.commit()


# ---------------------------------------------------------------------------
# Enforcement at the points that start model work (RUL-021).
# ---------------------------------------------------------------------------


def ensure_environment_active(db: Session, tenant_id: UUID, environment: str) -> None:
    if environment != "production" and usage_paused(db, tenant_id):
        raise ApiError(
            403, "usage.environment_paused",
            "本月预算已用尽，开发与预发环境已暂停（生产环境不受影响）。可调高预算或关闭此开关后恢复",
        )
