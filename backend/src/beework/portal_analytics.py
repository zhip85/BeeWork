"""AI portal usage analytics (T-147, 演进 PRD 10.5). Read-only, tenant-scoped, computed from the real records.

Definitions (also shown to administrators on the page):
- A *portal call* is a run with ``source = portal``; the *employee* of a call is the portal end user of its conversation.
- *Success rate* = succeeded / (succeeded + failed). Runs still queued or running and runs the employee cancelled are left out.
- *Satisfaction* = thumbs-up / (thumbs-up + thumbs-down) over the assistant answers of portal conversations that
  were given a rating in the window (answers by their own time); no ratings → no score (never a made-up number).
- A window of ``N`` days is the ``N`` days ending now; the comparison window is the ``N`` days before it. The change is
  relative (current / previous − 1) and absent when the previous value is 0.
- *Monthly active employees* / *monthly calls* are the current calendar month (UTC), whatever the window.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.models.conversations import Conversation, Message
from beework.models.portal import PortalApp
from beework.models.runtime import Run

WINDOWS = (7, 30, 90)
TOP_APPS = 10


@dataclass
class Metric:
    value: float | int | None
    previous: float | int | None
    change: float | None  # relative; None when the previous value is 0 / missing


@dataclass
class AppRow:
    app_id: UUID | None
    name: str
    calls: int
    success_rate: float | None
    employees: int


@dataclass
class DayPoint:
    day: str
    calls: int
    employees: int


@dataclass
class Analytics:
    days: int
    active_employees: Metric
    calls: Metric
    success_rate: Metric
    satisfaction: Metric
    ratings: dict[str, int]  # {"up": n, "down": n} in the window
    top_apps: list[AppRow] = field(default_factory=list)
    trend: list[DayPoint] = field(default_factory=list)
    monthly_active_employees: int = 0
    monthly_calls: int = 0


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _change(current: float | int | None, previous: float | int | None) -> float | None:
    if current is None or not previous:
        return None
    return round(current / previous - 1, 4)


def _rate(ok: int, failed: int) -> float | None:
    return round(ok / (ok + failed), 4) if ok + failed else None


def _runs(db: Session, tenant_id: UUID, start: datetime, end: datetime) -> list[tuple[datetime, str, UUID | None, UUID | None]]:
    """(created_at, status, employee end user, app) of the portal runs in ``[start, end)``."""
    rows = db.execute(
        select(Run.created_at, Run.status, Conversation.end_user_id, Conversation.portal_app_id)
        .join(Conversation, Conversation.id == Run.conversation_id)
        .where(Run.tenant_id == tenant_id, Run.source == "portal", Run.created_at >= start, Run.created_at < end)
    )
    return [(_aware(created), status, employee, app) for created, status, employee, app in rows]


def _ratings(db: Session, tenant_id: UUID, start: datetime, end: datetime) -> dict[str, int]:
    counts = {"up": 0, "down": 0}
    rows = db.execute(
        select(Message.feedback)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.tenant_id == tenant_id, Conversation.source == "portal", Message.role == "assistant",
            Message.feedback.in_(("up", "down")), Message.created_at >= start, Message.created_at < end,
        )
    )
    for (value,) in rows:
        counts[value] += 1
    return counts


def _summarise(runs: list[tuple[datetime, str, UUID | None, UUID | None]]) -> tuple[int, int, float | None]:
    ok = sum(1 for _c, status, _e, _a in runs if status == "succeeded")
    failed = sum(1 for _c, status, _e, _a in runs if status == "failed")
    return len({e for _c, _s, e, _a in runs if e is not None}), len(runs), _rate(ok, failed)


def _satisfaction(counts: dict[str, int]) -> float | None:
    total = counts["up"] + counts["down"]
    return round(counts["up"] / total, 4) if total else None


def monthly_figures(db: Session, tenant_id: UUID, *, now: datetime | None = None) -> tuple[int, int]:
    """(active employees, calls) of the current calendar month (UTC) so far."""
    end = _aware(now) if now else datetime.now(UTC)
    month_start = end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    employees, calls, _rate_ = _summarise(_runs(db, tenant_id, month_start, end + timedelta(microseconds=1)))
    return employees, calls


def compute(db: Session, tenant_id: UUID, days: int, *, now: datetime | None = None) -> Analytics:
    if days not in WINDOWS:
        raise ValueError(f"days must be one of {WINDOWS}")
    end = _aware(now) if now else datetime.now(UTC)
    start, previous_start = end - timedelta(days=days), end - timedelta(days=2 * days)
    current, before = _runs(db, tenant_id, start, end), _runs(db, tenant_id, previous_start, start)
    employees, calls, success = _summarise(current)
    p_employees, p_calls, p_success = _summarise(before)
    ratings, p_ratings = _ratings(db, tenant_id, start, end), _ratings(db, tenant_id, previous_start, start)
    satisfaction, p_satisfaction = _satisfaction(ratings), _satisfaction(p_ratings)

    names = {a.id: a.name for a in db.scalars(select(PortalApp).where(PortalApp.tenant_id == tenant_id))}
    by_app: dict[UUID | None, list[tuple[datetime, str, UUID | None, UUID | None]]] = {}
    for run in current:
        by_app.setdefault(run[3], []).append(run)
    top = sorted(by_app.items(), key=lambda item: (-len(item[1]), str(item[0])))[:TOP_APPS]
    top_apps = [
        AppRow(app_id=app_id, name=names.get(app_id, "—") if app_id else "—", calls=len(rows),
               success_rate=_summarise(rows)[2], employees=_summarise(rows)[0])
        for app_id, rows in top
    ]

    per_day: dict[str, list] = {}
    for created, _status, employee, _app in current:
        per_day.setdefault(created.date().isoformat(), []).append(employee)
    trend = []
    for offset in range(days):
        day = (end - timedelta(days=days - 1 - offset)).date()  # the window's last ``days`` calendar days, oldest first
        entries = per_day.get(day.isoformat(), [])
        trend.append(DayPoint(day=day.isoformat(), calls=len(entries), employees=len({e for e in entries if e is not None})))

    month_employees, month_calls = monthly_figures(db, tenant_id, now=end)
    return Analytics(
        days=days,
        active_employees=Metric(employees, p_employees, _change(employees, p_employees)),
        calls=Metric(calls, p_calls, _change(calls, p_calls)),
        success_rate=Metric(success, p_success, _change(success, p_success)),
        satisfaction=Metric(satisfaction, p_satisfaction, _change(satisfaction, p_satisfaction)),
        ratings=ratings, top_apps=top_apps, trend=trend,
        monthly_active_employees=month_employees, monthly_calls=month_calls,
    )
