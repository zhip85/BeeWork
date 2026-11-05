"""Tenant subscription usage page: points, trend, budget and CSV export (T-011, SET-080–082)."""

import csv
import io
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from math import ceil

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework.auth import require_csrf
from beework.db import get_db
from beework.entitlements import environments_for, get_subscription, seat_usage
from beework.errors import ApiError
from beework.models import AuditEvent, Membership, PlatformModel
from beework.models.assistants import Assistant
from beework.models.plans import Plan, PlanVersion
from beework.models.usage import UNKNOWN_ID, UsageRecord, UsageSyncState
from beework.rbac import require_tenant_permission
from beework.schemas.usage import (
    AssistantUsage,
    BudgetResponse,
    BudgetUpdate,
    MonthUsage,
    UsageSummary,
)
from beework.usage import (
    SYNC_NAME,
    add_months,
    apply_budget_change,
    get_budget,
    month_key,
    month_points_decimal,
    month_start,
    next_month,
    project_month_end,
    usage_paused,
)

router = APIRouter()

_EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
HISTORY_MONTHS = 6
TOP_ASSISTANTS = 3
MAX_EXPORT_DAYS = 366


def _budget_response(db: Session, tenant_id, now: datetime) -> BudgetResponse:
    budget = get_budget(db, tenant_id)
    used = month_points_decimal(db, tenant_id, now.date())
    return BudgetResponse(
        monthly_points=budget.monthly_points,
        thresholds=sorted(budget.thresholds or []),
        recipients=list(budget.recipients or []),
        pause_non_production=budget.pause_non_production,
        percent_used=round(float(used / budget.monthly_points * 100), 1),
        paused=usage_paused(db, tenant_id, now=now),
    )


@router.get("/tenant/usage", response_model=UsageSummary)
def get_usage(
    membership: Membership = Depends(require_tenant_permission("settings:view")),
    db: Session = Depends(get_db),
) -> UsageSummary:
    now = datetime.now(UTC)
    tenant_id = membership.tenant_id
    subscription = get_subscription(db, tenant_id)
    version = db.get(PlanVersion, subscription.plan_version_id)
    plan = db.get(Plan, version.plan_id)
    used = month_points_decimal(db, tenant_id, now.date())

    first = add_months(month_start(now), -(HISTORY_MONTHS - 1))
    per_month: dict[str, Decimal] = {}
    for day, points in db.execute(
        select(UsageRecord.day, UsageRecord.points).where(UsageRecord.tenant_id == tenant_id, UsageRecord.day >= first)
    ):
        per_month[month_key(day)] = per_month.get(month_key(day), Decimal(0)) + points
    history = []
    cursor = first
    for _ in range(HISTORY_MONTHS):
        history.append(MonthUsage(month=month_key(cursor), points=ceil(per_month.get(month_key(cursor), Decimal(0)))))
        cursor = next_month(cursor)

    this_month = month_start(now)
    rows = db.execute(
        select(UsageRecord.assistant_id, func.sum(UsageRecord.points))
        .where(UsageRecord.tenant_id == tenant_id, UsageRecord.day >= this_month, UsageRecord.day < next_month(this_month))
        .group_by(UsageRecord.assistant_id)
        .order_by(func.sum(UsageRecord.points).desc())
    ).all()
    total = sum((Decimal(points) for _, points in rows), Decimal(0))
    names = dict(db.execute(select(Assistant.id, Assistant.name).where(Assistant.tenant_id == tenant_id)).all())
    by_assistant = [
        AssistantUsage(
            assistant_id=None if assistant_id == UNKNOWN_ID else str(assistant_id),
            name=names.get(assistant_id),
            points=ceil(points),
            share=float(points / total) if total else 0.0,
        )
        for assistant_id, points in rows[:TOP_ASSISTANTS]
    ]
    rest = sum((Decimal(points) for _, points in rows[TOP_ASSISTANTS:]), Decimal(0))
    if rest:
        by_assistant.append(
            AssistantUsage(assistant_id=None, name=None, points=ceil(rest), share=float(rest / total), other=True)
        )

    state = db.get(UsageSyncState, SYNC_NAME)
    response = UsageSummary(
        month=month_key(now),
        points_used=ceil(used),
        points_quota=subscription.quotas.get("monthly_points"),
        projected_points=project_month_end(used, now),
        seats_used=seat_usage(db, tenant_id),
        seats_limit=subscription.quotas.get("seats"),
        environment_count=len(environments_for(db, tenant_id)),
        plan_name=plan.name,
        billing_cycle=subscription.billing_cycle,
        renews_at=subscription.renews_at,
        budget=_budget_response(db, tenant_id, now),
        history=history,
        by_assistant=by_assistant,
        synced_at=state.synced_at if state else None,
        sync_error=state.last_error if state else None,
    )
    db.commit()  # persists a lazily created subscription snapshot / default budget
    return response


@router.put("/tenant/usage/budget", response_model=BudgetResponse)
def update_budget(
    payload: BudgetUpdate,
    membership: Membership = Depends(require_tenant_permission("settings:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> BudgetResponse:
    recipients = []
    for raw in payload.recipients:
        email = raw.strip().lower()
        if not _EMAIL.match(email) or len(email) > 254:
            raise ApiError(422, "usage.invalid_recipient", f"告警接收人邮箱格式不正确：{raw}")
        if email not in recipients:
            recipients.append(email)
    budget = get_budget(db, membership.tenant_id)
    budget.monthly_points = payload.monthly_points
    budget.thresholds = sorted(set(payload.thresholds))
    budget.recipients = recipients
    budget.pause_non_production = payload.pause_non_production
    budget.updated_by = membership.user_id
    budget.updated_at = datetime.now(UTC)
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id, actor_user_id=membership.user_id, action="usage.budget_updated",
            details={
                "monthly_points": str(budget.monthly_points),
                "thresholds": ",".join(map(str, budget.thresholds)),
                "pause_non_production": str(budget.pause_non_production).lower(),
            },
        )
    )
    apply_budget_change(db, membership.tenant_id)  # commits
    return _budget_response(db, membership.tenant_id, datetime.now(UTC))


def _csv_cell(value: object) -> str:
    """Neutralise spreadsheet formulas: names are tenant-typed and the file is opened in Excel."""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@router.get("/tenant/usage/export")
def export_usage(
    start: date | None = Query(default=None, description="含，默认本月 1 日"),
    end: date | None = Query(default=None, description="含，默认今天"),
    membership: Membership = Depends(require_tenant_permission("settings:view")),
    db: Session = Depends(get_db),
) -> Response:
    today = datetime.now(UTC).date()
    start = start or month_start(today)
    end = end or today
    if end < start:
        raise ApiError(422, "usage.invalid_range", "结束日期不能早于开始日期")
    if (end - start) > timedelta(days=MAX_EXPORT_DAYS):
        raise ApiError(422, "usage.range_too_long", f"导出范围不能超过 {MAX_EXPORT_DAYS} 天")
    names = dict(db.execute(select(Assistant.id, Assistant.name).where(Assistant.tenant_id == membership.tenant_id)).all())
    models = dict(db.execute(select(PlatformModel.id, PlatformModel.display_name)).all())
    records = db.scalars(
        select(UsageRecord)
        .where(UsageRecord.tenant_id == membership.tenant_id, UsageRecord.day >= start, UsageRecord.day <= end)
        .order_by(UsageRecord.day, UsageRecord.assistant_id, UsageRecord.model_id, UsageRecord.environment)
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["date", "assistant", "model", "environment", "source", "requests", "tokens_in", "tokens_out", "points"])
    for record in records:
        writer.writerow([
            record.day.isoformat(),
            _csv_cell(names.get(record.assistant_id, "" if record.assistant_id == UNKNOWN_ID else "(deleted)")),
            _csv_cell(models.get(record.model_id, "" if record.model_id == UNKNOWN_ID else "(removed)")),
            record.environment, record.source, record.requests, record.tokens_in, record.tokens_out,
            f"{record.points:.2f}",
        ])
    return Response(
        content="﻿" + buffer.getvalue(),  # BOM so Excel reads UTF-8
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="usage-{start.isoformat()}_{end.isoformat()}.csv"'},
    )
