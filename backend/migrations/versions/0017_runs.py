"""Add runs: one row per assistant execution (T-025).

Revision ID: 0017_runs
Revises: 0016_assistants
Create Date: 2026-10-28

"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0017_runs'
down_revision: str | None = '0016_assistants'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('runs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('assistant_id', sa.Uuid(), nullable=False),
    sa.Column('version_id', sa.Uuid(), nullable=True),
    sa.Column('draft_row_version', sa.Integer(), nullable=True),
    sa.Column('conversation_id', sa.Uuid(), nullable=True),
    sa.Column('environment', sa.String(length=16), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('config', sa.JSON(), nullable=False),
    sa.Column('input', sa.JSON(), nullable=False),
    sa.Column('output', sa.JSON(), nullable=True),
    sa.Column('error_code', sa.String(length=64), nullable=True),
    sa.Column('error', sa.String(length=500), nullable=True),
    sa.Column('retrieval_mode', sa.String(length=16), nullable=True),
    sa.Column('cancel_requested', sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('tokens_in', sa.Integer(), nullable=True),
    sa.Column('tokens_out', sa.Integer(), nullable=True),
    sa.Column('trace_url', sa.String(length=500), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['assistant_id', 'tenant_id'], ['assistants.id', 'assistants.tenant_id'], name='fk_runs_assistant_same_tenant', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_runs_assistant_created', 'runs', ['assistant_id', 'created_at'], unique=False)
    op.create_index('ix_runs_tenant_created', 'runs', ['tenant_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_runs_tenant_id'), 'runs', ['tenant_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_runs_tenant_id'), table_name='runs')
    op.drop_index('ix_runs_tenant_created', table_name='runs')
    op.drop_index('ix_runs_assistant_created', table_name='runs')
    op.drop_table('runs')
