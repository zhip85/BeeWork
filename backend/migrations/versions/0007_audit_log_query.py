"""Add queryable audit event fields.

Revision ID: 0007_audit_log_query
Revises: 0006_member_lifecycle
Create Date: 2026-10-22
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0007_audit_log_query"
down_revision: str | None = "0006_member_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audit_events",
        sa.Column("category", sa.String(length=32), nullable=False, server_default="security"),
    )
    op.add_column(
        "audit_events",
        sa.Column("result", sa.String(length=24), nullable=False, server_default="success"),
    )
    op.add_column(
        "audit_events",
        sa.Column("description", sa.String(length=500), nullable=False, server_default=""),
    )
    op.add_column(
        "audit_events",
        sa.Column("source_ip", sa.String(length=64), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE audit_events
            SET category = CASE
                WHEN action LIKE 'membership.%' OR action LIKE 'role.%'
                    OR action LIKE 'organization.%' THEN 'members_permissions'
                WHEN action LIKE 'tenant.%' THEN 'security'
                ELSE 'security'
            END,
            description = action
            """
        )
    )
    op.create_index(
        "ix_audit_events_tenant_created_at",
        "audit_events",
        ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_events_tenant_created_at", table_name="audit_events")
    op.drop_column("audit_events", "source_ip")
    op.drop_column("audit_events", "description")
    op.drop_column("audit_events", "result")
    op.drop_column("audit_events", "category")
