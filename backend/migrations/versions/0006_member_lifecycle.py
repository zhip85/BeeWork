"""Add tenant member lifecycle and session revocation.

Revision ID: 0006_member_lifecycle
Revises: 0005_custom_roles
Create Date: 2026-10-21
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0006_member_lifecycle"
down_revision: str | None = "0005_custom_roles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("seat_limit", sa.Integer(), nullable=False, server_default="10"),
    )
    op.add_column(
        "memberships",
        sa.Column("status", sa.String(length=24), nullable=False, server_default="active"),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_id"),
    )
    op.create_index("ix_auth_sessions_token_id", "auth_sessions", ["token_id"])
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_table(
        "member_invitations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("custom_role_id", sa.Uuid(), nullable=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("invited_by", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["organization_id", "tenant_id"],
            ["organizations.id", "organizations.tenant_id"],
            ondelete="RESTRICT",
            name="fk_member_invitations_organization_same_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["custom_role_id", "tenant_id"],
            ["tenant_roles.id", "tenant_roles.tenant_id"],
            ondelete="RESTRICT",
            name="fk_member_invitations_custom_role_same_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_member_invitations_tenant_id", "member_invitations", ["tenant_id"]
    )
    op.create_index(
        "ix_member_invitations_email", "member_invitations", ["email"]
    )


def downgrade() -> None:
    op.drop_index("ix_member_invitations_email", table_name="member_invitations")
    op.drop_index("ix_member_invitations_tenant_id", table_name="member_invitations")
    op.drop_table("member_invitations")
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_token_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_column("memberships", "status")
    op.drop_column("tenants", "seat_limit")
