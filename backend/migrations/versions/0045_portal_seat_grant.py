"""AI portal seat grant in plans and subscription snapshots (T-149)

Revision ID: 0045_portal_seat_grant
Revises: 0044_skills
Create Date: 2026-11-11 15:00:00.000000

"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0045_portal_seat_grant'
down_revision: str | None = '0044_skills'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Plan defaults (standard: named · 200 · hard; professional: named · 1,000 · grace 5%; enterprise: unlimited).
DEFAULTS = {
    "standard": ("named", 200, "hard"),
    "professional": ("named", 1000, "grace_5"),
    "enterprise": ("unlimited", None, None),
}
FALLBACK = DEFAULTS["standard"]
KEYS = ("portal_mode", "portal_overage")


def _grant(plan_code: str | None) -> tuple[str, int | None, str | None]:
    return DEFAULTS.get(plan_code or "", FALLBACK)


def _fill(entitlements: dict, quotas: dict, overage_policy: dict, plan_code: str | None) -> tuple[dict, dict, dict]:
    mode, seats, overage = _grant(plan_code)
    entitlements = {**entitlements, "portal_mode": mode, "portal_overage": overage}
    quotas = {**quotas, "portal_seats": seats}
    overage_policy = {**overage_policy, "portal_seats": "soft" if (overage or "").startswith("grace") else "hard"}
    return entitlements, quotas, overage_policy


def upgrade() -> None:
    connection = op.get_bind()
    plans = sa.table("plans", sa.column("id", sa.Uuid()), sa.column("code", sa.String()))
    versions = sa.table(
        "plan_versions", sa.column("id", sa.Uuid()), sa.column("plan_id", sa.Uuid()),
        sa.column("entitlements", sa.JSON()), sa.column("quotas", sa.JSON()), sa.column("overage_policy", sa.JSON()),
    )
    subscriptions = sa.table(
        "tenant_subscriptions", sa.column("id", sa.Uuid()), sa.column("plan_version_id", sa.Uuid()),
        sa.column("entitlements", sa.JSON()), sa.column("quotas", sa.JSON()), sa.column("overage_policy", sa.JSON()),
    )
    codes = {row.id: row.code for row in connection.execute(sa.select(plans.c.id, plans.c.code))}
    version_plan: dict = {}
    for row in connection.execute(sa.select(versions)).fetchall():
        version_plan[row.id] = codes.get(row.plan_id)
        if "portal_mode" in (row.entitlements or {}):
            continue
        entitlements, quotas, overage = _fill(row.entitlements or {}, row.quotas or {}, row.overage_policy or {}, codes.get(row.plan_id))
        connection.execute(versions.update().where(versions.c.id == row.id).values(entitlements=entitlements, quotas=quotas, overage_policy=overage))
    for row in connection.execute(sa.select(subscriptions)).fetchall():
        if "portal_mode" in (row.entitlements or {}):
            continue
        entitlements, quotas, overage = _fill(row.entitlements or {}, row.quotas or {}, row.overage_policy or {}, version_plan.get(row.plan_version_id))
        connection.execute(subscriptions.update().where(subscriptions.c.id == row.id).values(entitlements=entitlements, quotas=quotas, overage_policy=overage))


def downgrade() -> None:
    connection = op.get_bind()
    for name in ("plan_versions", "tenant_subscriptions"):
        table = sa.table(name, sa.column("id", sa.Uuid()), sa.column("entitlements", sa.JSON()), sa.column("quotas", sa.JSON()), sa.column("overage_policy", sa.JSON()))
        for row in connection.execute(sa.select(table)).fetchall():
            entitlements = {k: v for k, v in (row.entitlements or {}).items() if k not in KEYS}
            quotas = {k: v for k, v in (row.quotas or {}).items() if k != "portal_seats"}
            overage = {k: v for k, v in (row.overage_policy or {}).items() if k != "portal_seats"}
            connection.execute(table.update().where(table.c.id == row.id).values(entitlements=entitlements, quotas=quotas, overage_policy=overage))
