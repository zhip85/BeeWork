"""Unified capability registry (T-160)

Revision ID: 0052_capability_registry
Revises: 0051_retire_flow_canvas
Create Date: 2026-11-12 20:00:00.000000

"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0052_capability_registry'
down_revision: str | None = '0051_retire_flow_canvas'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('capability_entries',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('source_id', sa.Uuid(), nullable=False),
    sa.Column('identifier', sa.String(length=120), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('description', sa.String(length=500), nullable=False),
    sa.Column('intent_description', sa.String(length=500), nullable=False),
    sa.Column('intent_examples', sa.JSON(), nullable=False),
    sa.Column('input_schema', sa.JSON(), nullable=False),
    sa.Column('writes', sa.Boolean(), nullable=False),
    sa.Column('direct_enabled', sa.Boolean(), nullable=False),
    sa.Column('direct_output', sa.Boolean(), nullable=False),
    sa.Column('timeout_ms', sa.Integer(), nullable=True),
    sa.Column('retry', sa.JSON(), nullable=True),
    sa.Column('meta', sa.JSON(), nullable=False),
    sa.Column('removed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('row_version', sa.Integer(), nullable=False),
    sa.Column('updated_by', sa.String(length=64), nullable=True),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'kind', 'source_id', 'identifier', name='uq_capability_entries_source')
    )
    op.create_index('ix_capability_entries_source', 'capability_entries', ['tenant_id', 'source_id'], unique=False)
    op.create_index('ix_capability_entries_tenant_kind', 'capability_entries', ['tenant_id', 'kind'], unique=False)
    op.create_index(op.f('ix_capability_entries_tenant_id'), 'capability_entries', ['tenant_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_capability_entries_tenant_id'), table_name='capability_entries')
    op.drop_index('ix_capability_entries_tenant_kind', table_name='capability_entries')
    op.drop_index('ix_capability_entries_source', table_name='capability_entries')
    op.drop_table('capability_entries')
