"""Add tenant-scoped custom roles and permission sets.

Revision ID: 0005_custom_roles
Revises: 0004_drop_old_org_fks
Create Date: 2026-10-21
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0005_custom_roles"
down_revision: str | None = "0004_drop_old_org_fks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenant_roles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("scope_organization_id", sa.Uuid(), nullable=True),
        sa.Column("permissions", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["scope_organization_id", "tenant_id"],
            ["organizations.id", "organizations.tenant_id"],
            ondelete="RESTRICT",
            name="fk_tenant_roles_scope_organization_same_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_tenant_roles_id_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id", "name", name="uq_tenant_roles_tenant_name"
        ),
    )
    op.create_index("ix_tenant_roles_tenant_id", "tenant_roles", ["tenant_id"])
    op.create_index(
        "ix_tenant_roles_scope_organization_id",
        "tenant_roles",
        ["scope_organization_id"],
    )
    op.add_column("memberships", sa.Column("custom_role_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_memberships_custom_role_id", "memberships", ["custom_role_id"]
    )
    op.create_foreign_key(
        "fk_memberships_custom_role_same_tenant",
        "memberships",
        "tenant_roles",
        ["custom_role_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_memberships_custom_role_same_tenant",
        "memberships",
        type_="foreignkey",
    )
    op.drop_index("ix_memberships_custom_role_id", table_name="memberships")
    op.drop_column("memberships", "custom_role_id")
    op.drop_index(
        "ix_tenant_roles_scope_organization_id", table_name="tenant_roles"
    )
    op.drop_index("ix_tenant_roles_tenant_id", table_name="tenant_roles")
    op.drop_table("tenant_roles")
