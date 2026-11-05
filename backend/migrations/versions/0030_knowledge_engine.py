"""knowledge_engine

Knowledge bases name their engine (RAGFlow or Dify) and the dataset id columns lose their RAGFlow-specific names.

Revision ID: 0030_knowledge_engine
Revises: 0029_knowledge_bases
Create Date: 2026-11-05 12:00:00.000000

"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0030_knowledge_engine'
down_revision: str | None = '0029_knowledge_bases'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column('knowledge_bases', 'ragflow_dataset_id', new_column_name='engine_dataset_id')
    op.alter_column('knowledge_bases', 'ragflow_faq_dataset_id', new_column_name='engine_faq_dataset_id')
    op.add_column('knowledge_bases', sa.Column('engine', sa.String(length=16), server_default='ragflow', nullable=False))


def downgrade() -> None:
    op.drop_column('knowledge_bases', 'engine')
    op.alter_column('knowledge_bases', 'engine_faq_dataset_id', new_column_name='ragflow_faq_dataset_id')
    op.alter_column('knowledge_bases', 'engine_dataset_id', new_column_name='ragflow_dataset_id')
