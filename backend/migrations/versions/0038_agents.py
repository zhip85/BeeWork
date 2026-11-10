"""Agents, Chatflows and Workflows (T-060)

Revision ID: 0038_agents
Revises: 0037_tenant_glossary
Create Date: 2026-11-10 12:00:00.000000

"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0038_agents'
down_revision: str | None = '0037_tenant_glossary'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('agents',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=12), nullable=False),
    sa.Column('is_team', sa.Boolean(), nullable=False),
    sa.Column('name', sa.String(length=50), nullable=False),
    sa.Column('description', sa.String(length=200), nullable=False),
    sa.Column('icon', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('draft_config', sa.JSON(), nullable=False),
    sa.Column('available_config', sa.JSON(), nullable=True),
    sa.Column('available_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.String(length=64), nullable=True),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('row_version', sa.Integer(), nullable=False),
    sa.Column('updated_by', sa.String(length=64), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('id', 'tenant_id', name='uq_agents_id_tenant_id')
    )
    op.create_index(op.f('ix_agents_tenant_id'), 'agents', ['tenant_id'], unique=False)
    op.create_index('ix_agents_tenant_kind', 'agents', ['tenant_id', 'kind', 'status'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_agents_tenant_kind', table_name='agents')
    op.drop_index(op.f('ix_agents_tenant_id'), table_name='agents')
    op.drop_table('agents')
