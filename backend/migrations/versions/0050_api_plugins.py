"""API plugins (T-073)

Revision ID: 0050_api_plugins
Revises: 0049_flow_connections
Create Date: 2026-11-12 16:00:00.000000

"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0050_api_plugins'
down_revision: str | None = '0049_flow_connections'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('api_plugins',
    sa.Column('capability_id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('api_type', sa.String(length=8), nullable=False),
    sa.Column('base_url', sa.String(length=500), nullable=False),
    sa.Column('auth_type', sa.String(length=10), nullable=False),
    sa.Column('api_key_header', sa.String(length=100), nullable=False),
    sa.Column('secret_encrypted', sa.String(length=2000), nullable=True),
    sa.Column('secret_last4', sa.String(length=4), nullable=True),
    sa.Column('oauth_token_url', sa.String(length=500), nullable=False),
    sa.Column('oauth_client_id', sa.String(length=200), nullable=False),
    sa.Column('oauth_client_secret_encrypted', sa.String(length=2000), nullable=True),
    sa.Column('oauth_client_secret_last4', sa.String(length=4), nullable=True),
    sa.Column('oauth_scope', sa.String(length=300), nullable=False),
    sa.Column('oauth_access_encrypted', sa.String(length=4000), nullable=True),
    sa.Column('oauth_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('spec_text', sa.Text(), nullable=False),
    sa.Column('spec_filename', sa.String(length=200), nullable=False),
    sa.Column('tools', sa.JSON(), nullable=False),
    sa.Column('selected_tools', sa.JSON(), nullable=False),
    sa.Column('last_test_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_test_ok', sa.Boolean(), nullable=False),
    sa.Column('last_test_tool', sa.String(length=64), nullable=True),
    sa.Column('last_test_error', sa.String(length=300), nullable=True),
    sa.ForeignKeyConstraint(['capability_id', 'tenant_id'], ['capabilities.id', 'capabilities.tenant_id'], name='fk_api_plugins_capability_same_tenant', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('capability_id')
    )
    op.create_index(op.f('ix_api_plugins_tenant_id'), 'api_plugins', ['tenant_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_api_plugins_tenant_id'), table_name='api_plugins')
    op.drop_table('api_plugins')
