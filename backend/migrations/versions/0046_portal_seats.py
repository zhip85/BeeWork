"""AI portal seat rules, assignments and policy (T-150)

Revision ID: 0046_portal_seats
Revises: 0045_portal_seat_grant
Create Date: 2026-11-11 18:00:00.000000

"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0046_portal_seats'
down_revision: str | None = '0045_portal_seat_grant'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portal_seat_rules",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "tenant_id"], ["organizations.id", "organizations.tenant_id"],
            ondelete="CASCADE", name="fk_portal_seat_rules_org_same_tenant",
        ),
        sa.UniqueConstraint("tenant_id", "organization_id", name="uq_portal_seat_rules_org"),
    )
    op.create_index("ix_portal_seat_rules_tenant_id", "portal_seat_rules", ["tenant_id"])
    op.create_table(
        "portal_seat_assignments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("source", sa.String(8)),
        sa.Column("excluded", sa.Boolean(), nullable=False),
        sa.Column("request_reason", sa.Text()),
        sa.Column("assigned_by", sa.String(64)),
        sa.Column("assigned_at", sa.DateTime(timezone=True)),
        sa.Column("decided_by", sa.String(64)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_portal_seat_assignments_user"),
    )
    op.create_index("ix_portal_seat_assignments_tenant_id", "portal_seat_assignments", ["tenant_id"])
    op.create_index("ix_portal_seat_assignments_user_id", "portal_seat_assignments", ["user_id"])
    op.create_table(
        "portal_seat_policy",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("auto_assign", sa.Boolean(), nullable=False),
        sa.Column("idle_days", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", name="uq_portal_seat_policy_tenant"),
    )
    op.create_index("ix_portal_seat_policy_tenant_id", "portal_seat_policy", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("portal_seat_policy")
    op.drop_table("portal_seat_assignments")
    op.drop_table("portal_seat_rules")
