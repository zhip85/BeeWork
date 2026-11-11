"""Capabilities and MCP connections (T-072)

Revision ID: 0043_capabilities_mcp
Revises: 0042_portal_settings
Create Date: 2026-11-12 09:00:00.000000

"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0043_capabilities_mcp'
down_revision: str | None = '0042_portal_settings'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('capabilities',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=8), nullable=False),
    sa.Column('name', sa.String(length=50), nullable=False),
    sa.Column('description', sa.String(length=500), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_by', sa.String(length=64), nullable=True),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('row_version', sa.Integer(), nullable=False),
    sa.Column('updated_by', sa.String(length=64), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('id', 'tenant_id', name='uq_capabilities_id_tenant_id')
    )
    op.create_index(op.f('ix_capabilities_tenant_id'), 'capabilities', ['tenant_id'], unique=False)
    op.create_index('ix_capabilities_tenant_kind', 'capabilities', ['tenant_id', 'kind', 'status'], unique=False)
    op.create_table('mcp_connections',
    sa.Column('capability_id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('transport', sa.String(length=16), nullable=False),
    sa.Column('url', sa.String(length=500), nullable=False),
    sa.Column('auth_type', sa.String(length=8), nullable=False),
    sa.Column('secret_encrypted', sa.String(length=2000), nullable=True),
    sa.Column('secret_last4', sa.String(length=4), nullable=True),
    sa.Column('oauth_authorize_url', sa.String(length=500), nullable=False),
    sa.Column('oauth_token_url', sa.String(length=500), nullable=False),
    sa.Column('oauth_client_id', sa.String(length=200), nullable=False),
    sa.Column('oauth_client_secret_encrypted', sa.String(length=2000), nullable=True),
    sa.Column('oauth_client_secret_last4', sa.String(length=4), nullable=True),
    sa.Column('oauth_scopes', sa.String(length=300), nullable=False),
    sa.Column('oauth_access_encrypted', sa.String(length=4000), nullable=True),
    sa.Column('oauth_refresh_encrypted', sa.String(length=4000), nullable=True),
    sa.Column('oauth_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('tools', sa.JSON(), nullable=False),
    sa.Column('selected_tools', sa.JSON(), nullable=False),
    sa.Column('last_test_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_test_ok', sa.Boolean(), nullable=False),
    sa.Column('last_test_error', sa.String(length=300), nullable=True),
    sa.Column('health', sa.String(length=8), nullable=False),
    sa.Column('health_failures', sa.Integer(), nullable=False),
    sa.Column('health_checked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['capability_id', 'tenant_id'], ['capabilities.id', 'capabilities.tenant_id'], name='fk_mcp_connections_capability_same_tenant', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('capability_id')
    )
    op.create_index(op.f('ix_mcp_connections_tenant_id'), 'mcp_connections', ['tenant_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_mcp_connections_tenant_id'), table_name='mcp_connections')
    op.drop_table('mcp_connections')
    op.drop_index('ix_capabilities_tenant_kind', table_name='capabilities')
    op.drop_index(op.f('ix_capabilities_tenant_id'), table_name='capabilities')
    op.drop_table('capabilities')
