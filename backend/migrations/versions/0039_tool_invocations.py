"""Tool invocation records of the controlled tool executor (T-071)

Revision ID: 0039_tool_invocations
Revises: 0038_agents
Create Date: 2026-11-10 23:00:00.000000

"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0039_tool_invocations'
down_revision: str | None = '0038_agents'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('tool_invocations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=True),
    sa.Column('assistant_id', sa.Uuid(), nullable=True),
    sa.Column('call_index', sa.Integer(), nullable=False),
    sa.Column('tool_name', sa.String(length=200), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('capability_id', sa.Uuid(), nullable=True),
    sa.Column('is_write', sa.Boolean(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('error_code', sa.String(length=64), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=False),
    sa.Column('request_bytes', sa.Integer(), nullable=False),
    sa.Column('response_bytes', sa.Integer(), nullable=False),
    sa.Column('idempotency_key', sa.String(length=120), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'idempotency_key', name='uq_tool_invocations_idempotency')
    )
    op.create_index(op.f('ix_tool_invocations_tenant_id'), 'tool_invocations', ['tenant_id'], unique=False)
    op.create_index('ix_tool_invocations_run', 'tool_invocations', ['run_id', 'call_index'], unique=False)
    op.create_index('ix_tool_invocations_tenant_time', 'tool_invocations', ['tenant_id', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_tool_invocations_tenant_time', table_name='tool_invocations')
    op.drop_index('ix_tool_invocations_run', table_name='tool_invocations')
    op.drop_index(op.f('ix_tool_invocations_tenant_id'), table_name='tool_invocations')
    op.drop_table('tool_invocations')
