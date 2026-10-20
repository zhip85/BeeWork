"""Add tenant organizations and membership scope.

Revision ID: 0002_organizations
Revises: 0001_tenant_identity
Create Date: 2026-10-21
"""
from typing import Sequence
from uuid import uuid4

from alembic import op
import sqlalchemy as sa

revision: str = "0002_organizations"
down_revision: str | None = "0001_tenant_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["organizations.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "parent_id", "name", name="uq_organizations_tenant_parent_name"
        ),
    )
    op.create_index("ix_organizations_tenant_id", "organizations", ["tenant_id"])
    op.create_index("ix_organizations_parent_id", "organizations", ["parent_id"])
    op.add_column("memberships", sa.Column("organization_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_memberships_organization_id", "memberships", ["organization_id"]
    )
    op.create_foreign_key(
        "fk_memberships_organization_id_organizations",
        "memberships",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    bind = op.get_bind()
    tenants = bind.execute(sa.text("SELECT id, name FROM tenants")).mappings().all()
    for tenant in tenants:
        organization_id = uuid4()
        bind.execute(
            sa.text(
                "INSERT INTO organizations (id, tenant_id, parent_id, name, created_at) "
                "VALUES (:id, :tenant_id, NULL, :name, CURRENT_TIMESTAMP)"
            ),
            {
                "id": organization_id,
                "tenant_id": tenant["id"],
                "name": tenant["name"],
            },
        )
        bind.execute(
            sa.text(
                "UPDATE memberships SET organization_id = :organization_id "
                "WHERE tenant_id = :tenant_id"
            ),
            {"organization_id": organization_id, "tenant_id": tenant["id"]},
        )

    op.alter_column("memberships", "organization_id", nullable=False)
    bind.execute(
        sa.text(
            "UPDATE memberships SET role = 'viewer' WHERE role = 'member'"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE memberships SET role = 'auditor' WHERE role = 'approver'"
        )
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_memberships_organization_id_organizations",
        "memberships",
        type_="foreignkey",
    )
    bind = op.get_bind()
    bind.execute(
        sa.text("UPDATE memberships SET role = 'member' WHERE role = 'viewer'")
    )
    bind.execute(
        sa.text("UPDATE memberships SET role = 'approver' WHERE role = 'auditor'")
    )
    op.drop_index("ix_memberships_organization_id", table_name="memberships")
    op.drop_column("memberships", "organization_id")
    op.drop_index("ix_organizations_parent_id", table_name="organizations")
    op.drop_index("ix_organizations_tenant_id", table_name="organizations")
    op.drop_table("organizations")
