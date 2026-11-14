"""Tenant default experience per channel type (T-042)

Revision ID: 0059_tenant_channel_styles
Revises: 0058_widget_components
Create Date: 2026-11-14 17:00:00.000000
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0059_tenant_channel_styles'
down_revision: str | None = '0058_widget_components'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenant_channel_styles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel_type", sa.String(length=16), nullable=False),
        sa.Column("overrides", sa.JSON(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "channel_type", name="uq_tenant_channel_styles_type"),
    )
    op.create_index("ix_tenant_channel_styles_tenant_id", "tenant_channel_styles", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_tenant_channel_styles_tenant_id", table_name="tenant_channel_styles")
    op.drop_table("tenant_channel_styles")
