"""Flow connections: Chatflow / Workflow connections (T-075)

Revision ID: 0049_flow_connections
Revises: 0048_portal_profiles
Create Date: 2026-11-12 14:00:00.000000

"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0049_flow_connections'
down_revision: str | None = '0048_portal_profiles'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('flow_connections',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=8), nullable=False),
    sa.Column('name', sa.String(length=50), nullable=False),
    sa.Column('description', sa.String(length=500), nullable=False),
    sa.Column('source_type', sa.String(length=8), nullable=False),
    sa.Column('flow_id', sa.Uuid(), nullable=True),
    sa.Column('base_url', sa.String(length=500), nullable=False),
    sa.Column('app_id', sa.String(length=200), nullable=False),
    sa.Column('credential_encrypted', sa.String(length=2000), nullable=True),
    sa.Column('credential_last4', sa.String(length=4), nullable=True),
    sa.Column('input_mapping', sa.JSON(), nullable=False),
    sa.Column('output_mapping', sa.JSON(), nullable=False),
    sa.Column('streaming', sa.Boolean(), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('last_test_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_test_ok', sa.Boolean(), nullable=True),
    sa.Column('last_test_error', sa.String(length=300), nullable=True),
    sa.Column('last_test_ms', sa.Integer(), nullable=True),
    sa.Column('created_by', sa.String(length=64), nullable=True),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('row_version', sa.Integer(), nullable=False),
    sa.Column('updated_by', sa.String(length=64), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['flow_id', 'tenant_id'], ['agents.id', 'agents.tenant_id'], name='fk_flow_connections_flow_same_tenant'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('id', 'tenant_id', name='uq_flow_connections_id_tenant_id')
    )
    op.create_index('ix_flow_connections_flow', 'flow_connections', ['tenant_id', 'flow_id'], unique=False)
    op.create_index('ix_flow_connections_tenant_kind', 'flow_connections', ['tenant_id', 'kind', 'status'], unique=False)
    op.create_index(op.f('ix_flow_connections_tenant_id'), 'flow_connections', ['tenant_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_flow_connections_tenant_id'), table_name='flow_connections')
    op.drop_index('ix_flow_connections_tenant_kind', table_name='flow_connections')
    op.drop_index('ix_flow_connections_flow', table_name='flow_connections')
    op.drop_table('flow_connections')
