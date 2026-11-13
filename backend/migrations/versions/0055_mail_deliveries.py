"""Mail delivery records (T-008)

Revision ID: 0055_mail_deliveries
Revises: 0054_execution_policy
Create Date: 2026-11-13 10:00:00.000000

"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0055_mail_deliveries'
down_revision: str | None = '0054_execution_policy'  # T-165 and T-163 are ahead of this migration on main
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('mail_deliveries',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=True),
    sa.Column('job_id', sa.Uuid(), nullable=True),
    sa.Column('template', sa.String(length=64), nullable=False),
    sa.Column('locale', sa.String(length=8), nullable=False),
    sa.Column('recipient_masked', sa.String(length=320), nullable=False),
    sa.Column('recipient_hash', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_mail_deliveries_tenant_id'), 'mail_deliveries', ['tenant_id'], unique=False)
    op.create_index('ix_mail_deliveries_recipient_template', 'mail_deliveries', ['recipient_hash', 'template', 'created_at'], unique=False)
    op.create_index('ix_mail_deliveries_status_created', 'mail_deliveries', ['status', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_mail_deliveries_status_created', table_name='mail_deliveries')
    op.drop_index('ix_mail_deliveries_recipient_template', table_name='mail_deliveries')
    op.drop_index(op.f('ix_mail_deliveries_tenant_id'), table_name='mail_deliveries')
    op.drop_table('mail_deliveries')
