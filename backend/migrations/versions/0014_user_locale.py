"""Add per-user interface locale preference.

Revision ID: 0014_user_locale
Revises: 0013_worker_queue
Create Date: 2026-10-28
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_user_locale"
down_revision: str | None = "0013_worker_queue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("locale", sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "locale")
