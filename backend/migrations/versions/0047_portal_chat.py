"""AI portal conversations and projects (T-143)

Revision ID: 0047_portal_chat
Revises: 0046_portal_seats
Create Date: 2026-11-11 21:00:00.000000

"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0047_portal_chat'
down_revision: str | None = '0046_portal_seats'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portal_projects",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("description", sa.String(300), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_portal_projects_tenant_id", "portal_projects", ["tenant_id"])
    op.create_index("ix_portal_projects_owner", "portal_projects", ["tenant_id", "user_id"])
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(sa.Column("title", sa.String(120)))
        batch.add_column(sa.Column("portal_app_id", sa.Uuid()))
        batch.add_column(sa.Column("project_id", sa.Uuid()))
        batch.add_column(sa.Column("pinned_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True)))
        batch.create_foreign_key("fk_conversations_portal_app", "portal_apps", ["portal_app_id"], ["id"], ondelete="SET NULL")
        batch.create_foreign_key("fk_conversations_portal_project", "portal_projects", ["project_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_conversations_portal_owner", "conversations", ["tenant_id", "end_user_id", "last_active_at"])


def downgrade() -> None:
    op.drop_index("ix_conversations_portal_owner", table_name="conversations")
    with op.batch_alter_table("conversations") as batch:
        batch.drop_constraint("fk_conversations_portal_project", type_="foreignkey")
        batch.drop_constraint("fk_conversations_portal_app", type_="foreignkey")
        for column in ("deleted_at", "pinned_at", "project_id", "portal_app_id", "title"):
            batch.drop_column(column)
    op.drop_table("portal_projects")
