"""Add assistants, assistant visibility links and assistant-scoped roles (T-020).

Revision ID: 0016_assistants
Revises: 0015_plans_subscriptions
Create Date: 2026-10-28

"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0016_assistants'
down_revision: str | None = '0015_plans_subscriptions'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('assistants',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=50), nullable=False),
    sa.Column('description', sa.String(length=500), nullable=False),
    sa.Column('category', sa.String(length=24), nullable=False),
    sa.Column('icon', sa.String(length=40), nullable=True),
    sa.Column('color', sa.String(length=16), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('visibility', sa.String(length=16), nullable=False),
    sa.Column('draft_config', sa.JSON(), nullable=False),
    sa.Column('draft_version', sa.String(length=16), nullable=False),
    sa.Column('live_version_id', sa.Uuid(), nullable=True),
    sa.Column('template_id', sa.Uuid(), nullable=True),
    sa.Column('owner_id', sa.Uuid(), nullable=True),
    sa.Column('created_by', sa.String(length=64), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('row_version', sa.Integer(), nullable=False),
    sa.Column('updated_by', sa.String(length=64), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['organization_id', 'tenant_id'], ['organizations.id', 'organizations.tenant_id'], name='fk_assistants_organization_same_tenant', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('id', 'tenant_id', name='uq_assistants_id_tenant_id')
    )
    op.create_index('ix_assistants_organization_id', 'assistants', ['organization_id'], unique=False)
    op.create_index(op.f('ix_assistants_tenant_id'), 'assistants', ['tenant_id'], unique=False)
    op.create_index('ix_assistants_tenant_status', 'assistants', ['tenant_id', 'status'], unique=False)
    op.create_table('assistant_visible_organizations',
    sa.Column('assistant_id', sa.Uuid(), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['assistant_id', 'tenant_id'], ['assistants.id', 'assistants.tenant_id'], name='fk_assistant_visible_assistant_same_tenant', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organization_id', 'tenant_id'], ['organizations.id', 'organizations.tenant_id'], name='fk_assistant_visible_org_same_tenant', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('assistant_id', 'organization_id')
    )
    op.create_index(op.f('ix_assistant_visible_organizations_tenant_id'), 'assistant_visible_organizations', ['tenant_id'], unique=False)
    op.add_column('tenant_roles', sa.Column('scope_assistant_ids', sa.JSON(), nullable=False, server_default=sa.text("'[]'")))


def downgrade() -> None:
    op.drop_column('tenant_roles', 'scope_assistant_ids')
    op.drop_index(op.f('ix_assistant_visible_organizations_tenant_id'), table_name='assistant_visible_organizations')
    op.drop_table('assistant_visible_organizations')
    op.drop_index('ix_assistants_tenant_status', table_name='assistants')
    op.drop_index(op.f('ix_assistants_tenant_id'), table_name='assistants')
    op.drop_index('ix_assistants_organization_id', table_name='assistants')
    op.drop_table('assistants')
