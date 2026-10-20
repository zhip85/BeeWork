"""Add tenant model environment configuration.

Revision ID: 0010_tenant_model_environments
Revises: 0009_platform_model_catalog
Create Date: 2026-10-22
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0010_tenant_model_environments"
down_revision: str | None = "0009_platform_model_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("plan", sa.String(length=24), nullable=False, server_default="standard"),
    )
    op.add_column(
        "tenants",
        sa.Column("region", sa.String(length=32), nullable=False, server_default="ap_singapore"),
    )
    op.create_table(
        "tenant_model_environments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("environment", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "environment", name="uq_tenant_model_environment"),
    )
    op.create_index(
        "ix_tenant_model_environments_tenant_id",
        "tenant_model_environments",
        ["tenant_id"],
    )
    op.create_table(
        "tenant_model_selections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("model_id", sa.Uuid(), nullable=False),
        sa.Column("model_type", sa.String(length=24), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["tenant_model_environments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["model_id"], ["platform_models.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "environment_id", "model_id", name="uq_environment_model_selection"
        ),
    )
    op.create_index(
        "ix_tenant_model_selections_environment_id",
        "tenant_model_selections",
        ["environment_id"],
    )
    op.create_index(
        "uq_tenant_model_default_type",
        "tenant_model_selections",
        ["environment_id", "model_type"],
        unique=True,
        postgresql_where=sa.text("is_default"),
        sqlite_where=sa.text("is_default = 1"),
    )


def downgrade() -> None:
    op.drop_index("uq_tenant_model_default_type", table_name="tenant_model_selections")
    op.drop_index(
        "ix_tenant_model_selections_environment_id",
        table_name="tenant_model_selections",
    )
    op.drop_table("tenant_model_selections")
    op.drop_index(
        "ix_tenant_model_environments_tenant_id",
        table_name="tenant_model_environments",
    )
    op.drop_table("tenant_model_environments")
    op.drop_column("tenants", "region")
    op.drop_column("tenants", "plan")
