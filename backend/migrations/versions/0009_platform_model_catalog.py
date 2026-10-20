"""Add platform model catalog.

Revision ID: 0009_platform_model_catalog
Revises: 0008_audit_chain_and_retention
Create Date: 2026-10-22
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0009_platform_model_catalog"
down_revision: str | None = "0008_audit_chain_and_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_models",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model_type", sa.String(length=24), nullable=False),
        sa.Column("availability", sa.String(length=24), nullable=False),
        sa.Column("input_price_per_million", sa.Numeric(12, 6), nullable=False),
        sa.Column("output_price_per_million", sa.Numeric(12, 6), nullable=False),
        sa.Column("input_points_per_thousand", sa.Numeric(12, 6), nullable=False),
        sa.Column("output_points_per_thousand", sa.Numeric(12, 6), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("provider_model_id", sa.String(length=160), nullable=False),
        sa.Column("endpoint_url", sa.String(length=2048), nullable=False),
        sa.Column("encrypted_api_key", sa.String(length=4096), nullable=False),
        sa.Column("api_key_last_four", sa.String(length=4), nullable=False),
        sa.Column("api_version", sa.String(length=80), nullable=True),
        sa.Column("region", sa.String(length=32), nullable=False),
        sa.Column("context_window", sa.Integer(), nullable=True),
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
        sa.Column("default_temperature", sa.Numeric(3, 2), nullable=True),
        sa.Column("rpm_limit", sa.Integer(), nullable=True),
        sa.Column("fallback_model_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["fallback_model_id"],
            ["platform_models.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("display_name"),
    )
    op.create_index(
        "ix_platform_models_provider_region_type",
        "platform_models",
        ["provider", "region", "model_type"],
    )
    op.create_table(
        "platform_model_tenant_allowlist",
        sa.Column("model_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["model_id"], ["platform_models.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("model_id", "tenant_id"),
    )
    op.create_index(
        "ix_platform_model_tenant_allowlist_tenant_id",
        "platform_model_tenant_allowlist",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_platform_model_tenant_allowlist_tenant_id",
        table_name="platform_model_tenant_allowlist",
    )
    op.drop_table("platform_model_tenant_allowlist")
    op.drop_index("ix_platform_models_provider_region_type", table_name="platform_models")
    op.drop_table("platform_models")
