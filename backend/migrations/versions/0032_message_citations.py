"""Store the knowledge passages an assistant answer was based on (T-033).

Revision ID: 0032_message_citations
Revises: 0031_knowledge_documents
Create Date: 2026-11-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_message_citations"
down_revision: str | None = "0031_knowledge_documents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("citations", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "citations")
