"""AI portal settings (T-141)

Revision ID: 0042_portal_settings
Revises: 0041_portal_apps
Create Date: 2026-11-11 15:00:00.000000

"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0042_portal_settings'
down_revision: str | None = '0041_portal_apps'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('portal_settings',
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('portal_name', sa.String(length=50), nullable=False),
    sa.Column('input_placeholder', sa.String(length=100), nullable=False),
    sa.Column('default_audience', sa.String(length=8), nullable=False),
    sa.Column('answer_style', sa.String(length=10), nullable=False),
    sa.Column('nav_search', sa.Boolean(), nullable=False),
    sa.Column('nav_projects', sa.Boolean(), nullable=False),
    sa.Column('nav_app_library', sa.Boolean(), nullable=False),
    sa.Column('nav_recent', sa.Boolean(), nullable=False),
    sa.Column('nav_pinned', sa.Boolean(), nullable=False),
    sa.Column('file_upload', sa.Boolean(), nullable=False),
    sa.Column('voice_input', sa.Boolean(), nullable=False),
    sa.Column('profile_edit', sa.Boolean(), nullable=False),
    sa.Column('personal_memory', sa.Boolean(), nullable=False),
    sa.Column('task_notifications', sa.Boolean(), nullable=False),
    sa.Column('delisted_history', sa.String(length=8), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('row_version', sa.Integer(), nullable=False),
    sa.Column('updated_by', sa.String(length=64), nullable=True),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('tenant_id')
    )


def downgrade() -> None:
    op.drop_table('portal_settings')
