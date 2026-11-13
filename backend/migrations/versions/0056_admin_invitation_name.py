"""Remember the invitee name on an admin invitation (T-008b)

Revision ID: 0056_admin_invitation_name
Revises: 0055_mail_deliveries
Create Date: 2026-11-13 18:00:00.000000

A resend must rebuild the original invitation mail, including the greeting.  The
name used to live only in the provisioning request, so it was lost afterwards.
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0056_admin_invitation_name'
down_revision: str | None = '0055_mail_deliveries'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "member_invitations",
        sa.Column("invitee_name", sa.String(length=120), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("member_invitations", "invitee_name")
