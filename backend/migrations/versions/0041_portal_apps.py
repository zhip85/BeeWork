"""AI portal apps (T-140)

Revision ID: 0041_portal_apps
Revises: 0040_resource_references
Create Date: 2026-11-11 12:00:00.000000

"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0041_portal_apps'
down_revision: str | None = '0040_resource_references'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('portal_apps',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('source_type', sa.String(length=24), nullable=False),
    sa.Column('source_id', sa.Uuid(), nullable=True),
    sa.Column('name', sa.String(length=50), nullable=False),
    sa.Column('description', sa.String(length=200), nullable=False),
    sa.Column('category', sa.String(length=24), nullable=False),
    sa.Column('audience', sa.String(length=8), nullable=False),
    sa.Column('mode', sa.String(length=8), nullable=False),
    sa.Column('icon', sa.String(length=40), nullable=False),
    sa.Column('featured', sa.Boolean(), nullable=False),
    sa.Column('status', sa.String(length=8), nullable=False),
    sa.Column('listed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('listed_by', sa.String(length=64), nullable=True),
    sa.Column('delisted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('delisted_reason', sa.String(length=200), nullable=True),
    sa.Column('created_by', sa.String(length=64), nullable=True),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('row_version', sa.Integer(), nullable=False),
    sa.Column('updated_by', sa.String(length=64), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('id', 'tenant_id', name='uq_portal_apps_id_tenant_id')
    )
    op.create_index(op.f('ix_portal_apps_tenant_id'), 'portal_apps', ['tenant_id'], unique=False)
    op.create_index('ix_portal_apps_tenant_status', 'portal_apps', ['tenant_id', 'status'], unique=False)
    op.create_index('ix_portal_apps_source', 'portal_apps', ['tenant_id', 'source_type', 'source_id'], unique=False)
    op.create_table('portal_app_organizations',
    sa.Column('app_id', sa.Uuid(), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['app_id', 'tenant_id'], ['portal_apps.id', 'portal_apps.tenant_id'], name='fk_portal_app_org_app_same_tenant', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organization_id', 'tenant_id'], ['organizations.id', 'organizations.tenant_id'], name='fk_portal_app_org_same_tenant', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('app_id', 'organization_id')
    )
    op.create_index(op.f('ix_portal_app_organizations_tenant_id'), 'portal_app_organizations', ['tenant_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_portal_app_organizations_tenant_id'), table_name='portal_app_organizations')
    op.drop_table('portal_app_organizations')
    op.drop_index('ix_portal_apps_source', table_name='portal_apps')
    op.drop_index('ix_portal_apps_tenant_status', table_name='portal_apps')
    op.drop_index(op.f('ix_portal_apps_tenant_id'), table_name='portal_apps')
    op.drop_table('portal_apps')
