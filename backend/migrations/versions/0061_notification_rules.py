"""Notification rules and the digest queue (T-088)

Revision ID: 0061_notification_rules
Revises: 0060_webhooks
Create Date: 2026-11-14 21:00:00.000000
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0061_notification_rules'
down_revision: str | None = '0060_webhooks'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_rules",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=12), nullable=False),
        sa.Column("recipients", sa.JSON(), nullable=False),
        sa.Column("frequency", sa.String(length=10), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
    )
    op.create_index("ix_notification_rules_tenant_id", "notification_rules", ["tenant_id"])
    op.create_index("ix_notification_rules_tenant_event", "notification_rules", ["tenant_id", "event"])
    op.create_table(
        "notification_queue",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rule_id", sa.Uuid(), sa.ForeignKey("notification_rules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("rule_event", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("rule_id", "event_id", name="uq_notification_queue_event"),
    )
    op.create_index("ix_notification_queue_tenant_id", "notification_queue", ["tenant_id"])
    op.create_index("ix_notification_queue_pending", "notification_queue", ["rule_id", "sent_at"])


def downgrade() -> None:
    op.drop_table("notification_queue")
    op.drop_table("notification_rules")
