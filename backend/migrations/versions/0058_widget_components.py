"""Widget templates, tenant components and a channel's component selection (T-043)

Revision ID: 0058_widget_components
Revises: 0057_channel_framework
Create Date: 2026-11-14 15:00:00.000000
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0058_widget_components'
down_revision: str | None = '0057_channel_framework'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "widget_templates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name_zh", sa.String(length=80), nullable=False),
        sa.Column("name_en", sa.String(length=80), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=False),
        sa.Column("trigger_tag", sa.String(length=40), nullable=False),
        sa.Column("schema", sa.Text(), nullable=False),
        sa.Column("sample_data", sa.Text(), nullable=False),
        sa.Column("compatibility", sa.JSON(), nullable=False),
        sa.Column("version", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("trigger_tag", name="uq_widget_templates_tag"),
    )
    op.create_table(
        "tenant_components",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name_zh", sa.String(length=80), nullable=False),
        sa.Column("name_en", sa.String(length=80), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=False),
        sa.Column("trigger_tag", sa.String(length=40), nullable=False),
        sa.Column("schema", sa.Text(), nullable=False),
        sa.Column("sample_data", sa.Text(), nullable=False),
        sa.Column("compatibility", sa.JSON(), nullable=False),
        sa.Column("version", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("template_id", sa.Uuid(), sa.ForeignKey("widget_templates.id", ondelete="SET NULL"), nullable=True),
        sa.Column("template_version", sa.String(length=16), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "trigger_tag", name="uq_tenant_components_tag"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_tenant_components_id_tenant_id"),
    )
    op.create_index("ix_tenant_components_tenant_id", "tenant_components", ["tenant_id"])
    op.create_index("ix_tenant_components_tenant_status", "tenant_components", ["tenant_id", "status"])
    op.add_column("channel_instances", sa.Column("component_ids", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    op.drop_column("channel_instances", "component_ids")
    op.drop_index("ix_tenant_components_tenant_status", table_name="tenant_components")
    op.drop_index("ix_tenant_components_tenant_id", table_name="tenant_components")
    op.drop_table("tenant_components")
    op.drop_table("widget_templates")
