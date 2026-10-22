"""Track AI gateway (LiteLLM) registration of models and tenant environments.

Revision ID: 0012_gateway_sync
Revises: 0011_lifecycle_and_staging
Create Date: 2026-10-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_gateway_sync"
down_revision: str | None = "0011_lifecycle_and_staging"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sync_columns(table: str) -> None:
    op.add_column(
        table,
        sa.Column(
            "gateway_sync_status",
            sa.String(length=16),
            nullable=False,
            server_default="not_configured",
        ),
    )
    op.alter_column(table, "gateway_sync_status", server_default=None)
    op.add_column(table, sa.Column("gateway_error", sa.String(length=500), nullable=True))
    op.add_column(
        table, sa.Column("gateway_synced_at", sa.DateTime(timezone=True), nullable=True)
    )


def upgrade() -> None:
    _sync_columns("platform_models")
    _sync_columns("tenant_model_environments")
    op.add_column(
        "tenant_model_environments",
        sa.Column("gateway_team_id", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "tenant_model_environments",
        sa.Column("gateway_key_encrypted", sa.String(length=4096), nullable=True),
    )


def downgrade() -> None:
    for column in ("gateway_key_encrypted", "gateway_team_id"):
        op.drop_column("tenant_model_environments", column)
    for table in ("tenant_model_environments", "platform_models"):
        for column in ("gateway_synced_at", "gateway_error", "gateway_sync_status"):
            op.drop_column(table, column)
