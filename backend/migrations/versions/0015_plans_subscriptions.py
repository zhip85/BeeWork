"""Add plans, plan versions and tenant subscription snapshots (T-010a).

Existing tenants get a subscription snapshot from their ``tenants.plan`` and ``tenants.seat_limit``.
``tenants.plan`` and ``tenants.seat_limit`` stay as compatibility columns; seat limits are read from
the subscription from now on.

Revision ID: 0015_plans_subscriptions
Revises: 0014_user_locale
Create Date: 2026-10-28
"""
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "0015_plans_subscriptions"
down_revision: str | None = "0014_user_locale"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OVERAGE = {
    key: "hard"
    for key in ("assistants", "seats", "monthly_points", "storage_gb", "monthly_conversations", "api_qps")
}
PLANS = {
    "standard": {
        "name": "标准版",
        "description": "助理、知识库、FAQ、组件；网页挂件与 API 渠道。",
        "price_text": "$499 / 月",
        "entitlements": {
            "external_flows": False, "mcp": False, "skills": False,
            "memory": "short", "channels": "widget_api", "handoff": False,
            "evaluations": False, "multi_agent": False, "sso": False,
            "approval_levels": 1, "environments": "production_only",
            "custom_domain": False, "byok": False, "log_retention_days": 30,
        },
        "quotas": {
            "assistants": 5, "seats": 10, "monthly_points": 200000, "storage_gb": 5,
            "monthly_conversations": 20000, "api_qps": 10,
        },
    },
    "professional": {
        "name": "专业版",
        "description": "+ 外部流程、MCP、技能、IM 渠道、人工转接、评测、SSO。",
        "price_text": "$2,400 / 月",
        "entitlements": {
            "external_flows": True, "mcp": True, "skills": True,
            "memory": "short_long", "channels": "im", "handoff": True,
            "evaluations": True, "multi_agent": "delegate_handoff_parallel", "sso": "sso",
            "approval_levels": 1, "environments": "production_only",
            "custom_domain": "custom_domain", "byok": "model_keys", "log_retention_days": 90,
        },
        "quotas": {
            "assistants": 30, "seats": 50, "monthly_points": 1500000, "storage_gb": 50,
            "monthly_conversations": 200000, "api_qps": 50,
        },
    },
    "enterprise": {
        "name": "企业版",
        "description": "+ 全部多智能体模式、多环境、白标、SCIM、自带密钥。",
        "price_text": "定制合同",
        "entitlements": {
            "external_flows": True, "mcp": True, "skills": True,
            "memory": "all_cross_assistant", "channels": "all", "handoff": True,
            "evaluations": True, "multi_agent": "all_long_tasks", "sso": "sso_scim",
            "approval_levels": "multi", "environments": "all",
            "custom_domain": "white_label", "byok": "model_and_encryption_keys",
            "log_retention_days": "custom",
        },
        "quotas": {
            "assistants": None, "seats": 500, "monthly_points": 10000000, "storage_gb": 1024,
            "monthly_conversations": 2000000, "api_qps": 300,
        },
    },
}


def upgrade() -> None:
    plans = op.create_table(
        "plans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("code", sa.String(length=24), nullable=False, unique=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("price_text", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    versions = op.create_table(
        "plan_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("plan_id", sa.Uuid(), sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("entitlements", sa.JSON(), nullable=False),
        sa.Column("quotas", sa.JSON(), nullable=False),
        sa.Column("overage_policy", sa.JSON(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("plan_id", "version", name="uq_plan_versions_plan_version"),
    )
    op.create_index("ix_plan_versions_plan_id", "plan_versions", ["plan_id"])
    subscriptions = op.create_table(
        "tenant_subscriptions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("plan_version_id", sa.Uuid(), sa.ForeignKey("plan_versions.id"), nullable=False),
        sa.Column("entitlements", sa.JSON(), nullable=False),
        sa.Column("quotas", sa.JSON(), nullable=False),
        sa.Column("overage_policy", sa.JSON(), nullable=False),
        sa.Column("billing_cycle", sa.String(length=16), nullable=False),
        sa.Column("renews_at", sa.DateTime(timezone=True)),
        sa.Column("addons", sa.JSON(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tenant_subscriptions_tenant_id", "tenant_subscriptions", ["tenant_id"])
    op.create_index(
        "ix_tenant_subscriptions_plan_version_id", "tenant_subscriptions", ["plan_version_id"]
    )
    op.create_index(
        "uq_tenant_subscriptions_current",
        "tenant_subscriptions",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
        sqlite_where=sa.text("is_current"),
    )

    now = datetime.now(UTC)
    version_ids: dict[str, object] = {}
    for code, definition in PLANS.items():
        plan_id = uuid4()
        version_id = uuid4()
        version_ids[code] = version_id
        op.bulk_insert(
            plans,
            [{
                "id": plan_id, "code": code, "name": definition["name"],
                "description": definition["description"], "price_text": definition["price_text"],
                "created_at": now,
            }],
        )
        op.bulk_insert(
            versions,
            [{
                "id": version_id, "plan_id": plan_id, "version": 1,
                "entitlements": definition["entitlements"], "quotas": definition["quotas"],
                "overage_policy": OVERAGE, "published_at": now,
            }],
        )

    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, plan, seat_limit FROM tenants")).fetchall()
    snapshots = []
    for tenant_id, plan, seat_limit in rows:
        code = plan if plan in PLANS else "standard"
        quotas = dict(PLANS[code]["quotas"])
        quotas["seats"] = seat_limit
        snapshots.append({
            "id": uuid4(), "tenant_id": tenant_id, "plan_version_id": version_ids[code],
            "entitlements": PLANS[code]["entitlements"], "quotas": quotas,
            "overage_policy": OVERAGE, "billing_cycle": "monthly", "addons": {},
            "is_current": True, "created_at": now,
        })
    if snapshots:
        op.bulk_insert(subscriptions, snapshots)


def downgrade() -> None:
    op.drop_index("uq_tenant_subscriptions_current", table_name="tenant_subscriptions")
    op.drop_table("tenant_subscriptions")
    op.drop_table("plan_versions")
    op.drop_table("plans")
