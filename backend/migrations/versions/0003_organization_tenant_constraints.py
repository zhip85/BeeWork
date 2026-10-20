"""Constrain organization trees and memberships to one tenant.

Revision ID: 0003_org_tenant_constraints
Revises: 0002_organizations
Create Date: 2026-10-21
"""
from typing import Sequence

from alembic import op

revision: str = "0003_org_tenant_constraints"
down_revision: str | None = "0002_organizations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_organizations_id_tenant_id",
        "organizations",
        ["id", "tenant_id"],
    )
    op.create_foreign_key(
        "fk_organizations_parent_same_tenant",
        "organizations",
        "organizations",
        ["parent_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_memberships_organization_same_tenant",
        "memberships",
        "organizations",
        ["organization_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_memberships_organization_same_tenant",
        "memberships",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_organizations_parent_same_tenant",
        "organizations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_organizations_id_tenant_id",
        "organizations",
        type_="unique",
    )
