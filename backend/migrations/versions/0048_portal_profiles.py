"""AI portal employee profiles (T-145)

Revision ID: 0048_portal_profiles
Revises: 0047_portal_chat
Create Date: 2026-11-12 09:00:00.000000

"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0048_portal_profiles'
down_revision: str | None = '0047_portal_chat'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portal_profiles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_title", sa.String(60), nullable=False),
        sa.Column("default_app_id", sa.Uuid(), sa.ForeignKey("portal_apps.id", ondelete="SET NULL")),
        sa.Column("personal_memory", sa.Boolean(), nullable=False),
        sa.Column("task_notifications", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_portal_profiles_member"),
    )
    op.create_index("ix_portal_profiles_tenant_id", "portal_profiles", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("portal_profiles")
