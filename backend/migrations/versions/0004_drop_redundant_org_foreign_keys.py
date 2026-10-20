"""Remove superseded single-column organization foreign keys.

Revision ID: 0004_drop_old_org_fks
Revises: 0003_org_tenant_constraints
Create Date: 2026-10-21
"""
from typing import Sequence

from alembic import op

revision: str = "0004_drop_old_org_fks"
down_revision: str | None = "0003_org_tenant_constraints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "fk_memberships_organization_id_organizations",
        "memberships",
        type_="foreignkey",
    )
    op.drop_constraint(
        "organizations_parent_id_fkey",
        "organizations",
        type_="foreignkey",
    )


def downgrade() -> None:
    op.create_foreign_key(
        "organizations_parent_id_fkey",
        "organizations",
        "organizations",
        ["parent_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_memberships_organization_id_organizations",
        "memberships",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="RESTRICT",
    )
