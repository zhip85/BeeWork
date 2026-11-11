"""Skills: platform built-in skills and tenant skills (T-074)

Revision ID: 0044_skills
Revises: 0043_capabilities_mcp
Create Date: 2026-11-11 12:00:00.000000

"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0044_skills'
down_revision: str | None = '0043_capabilities_mcp'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('platform_skills',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('slug', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=50), nullable=False),
    sa.Column('description', sa.String(length=500), nullable=False),
    sa.Column('content', sa.String(length=20000), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_by', sa.String(length=64), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('slug')
    )
    op.create_table('skills',
    sa.Column('capability_id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('platform_skill_id', sa.Uuid(), nullable=True),
    sa.Column('filename', sa.String(length=200), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('package_key', sa.String(length=300), nullable=True),
    sa.Column('package_version', sa.Integer(), nullable=False),
    sa.Column('manifest', sa.JSON(), nullable=False),
    sa.Column('has_scripts', sa.Boolean(), nullable=False),
    sa.Column('security_status', sa.String(length=10), nullable=False),
    sa.Column('checks', sa.JSON(), nullable=False),
    sa.Column('checked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('uploaded_by_user_id', sa.Uuid(), nullable=True),
    sa.Column('enable_warning', sa.String(length=300), nullable=True),
    sa.ForeignKeyConstraint(['capability_id', 'tenant_id'], ['capabilities.id', 'capabilities.tenant_id'], name='fk_skills_capability_same_tenant', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('capability_id')
    )
    op.create_index(op.f('ix_skills_platform_skill_id'), 'skills', ['platform_skill_id'], unique=False)
    op.create_index(op.f('ix_skills_tenant_id'), 'skills', ['tenant_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_skills_tenant_id'), table_name='skills')
    op.drop_index(op.f('ix_skills_platform_skill_id'), table_name='skills')
    op.drop_table('skills')
    op.drop_table('platform_skills')
