"""Channel framework: channel types, connection config, health, immutable snapshots (T-041)

Revision ID: 0057_channel_framework
Revises: 0056_admin_invitation_name
Create Date: 2026-11-14 12:00:00.000000
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0057_channel_framework'
down_revision: str | None = '0056_admin_invitation_name'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "channel_types",
        sa.Column("code", sa.String(length=16), primary_key=True),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("version", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("connection_params", sa.JSON(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("default_experience", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("channel_instances", sa.Column("config", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("channel_instances", sa.Column("secret_config", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("channel_instances", sa.Column("health_failures", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("channel_instances", sa.Column("health_error", sa.String(length=200), nullable=True))
    op.add_column("channel_instances", sa.Column("health_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "channel_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("version_seq", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("published_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["channel_id", "tenant_id"], ["channel_instances.id", "channel_instances.tenant_id"],
            ondelete="CASCADE", name="fk_channel_snapshots_channel_same_tenant",
        ),
        sa.UniqueConstraint("channel_id", "version_seq", name="uq_channel_snapshots_version"),
    )
    op.create_index("ix_channel_snapshots_tenant_id", "channel_snapshots", ["tenant_id"])
    # A published version never changes (CHN-025): later edits publish a new row.
    op.execute(
        """
        CREATE FUNCTION beework_guard_channel_snapshot_update() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'channel snapshots are immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_channel_snapshots_immutable
        BEFORE UPDATE ON channel_snapshots
        FOR EACH ROW EXECUTE FUNCTION beework_guard_channel_snapshot_update()
        """
    )
    # The snapshots of T-040 become the current version of their channel, so the first diff has something to compare with.
    op.execute(
        """
        INSERT INTO channel_snapshots (id, tenant_id, channel_id, version_seq, snapshot, created_at)
        SELECT gen_random_uuid(), tenant_id, id, version_seq, published_snapshot, COALESCE(published_at, now())
        FROM channel_instances WHERE published_snapshot IS NOT NULL AND version_seq > 0
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_channel_snapshots_immutable ON channel_snapshots")
    op.execute("DROP FUNCTION beework_guard_channel_snapshot_update()")
    op.drop_index("ix_channel_snapshots_tenant_id", table_name="channel_snapshots")
    op.drop_table("channel_snapshots")
    for column in ("health_checked_at", "health_error", "health_failures", "secret_config", "config"):
        op.drop_column("channel_instances", column)
    op.drop_table("channel_types")
