"""Add scoped audit hash chains and retention checkpoints.

Revision ID: 0008_audit_chain_and_retention
Revises: 0007_audit_log_query
Create Date: 2026-10-22
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0008_audit_chain_and_retention"
down_revision: str | None = "0007_audit_log_query"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audit_events",
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="tenant"),
    )
    op.add_column("audit_events", sa.Column("chain_scope", sa.String(length=64)))
    op.add_column("audit_events", sa.Column("chain_sequence", sa.Integer()))
    op.add_column("audit_events", sa.Column("previous_hash", sa.String(length=64)))
    op.add_column("audit_events", sa.Column("event_hash", sa.String(length=64)))
    op.execute(
        sa.text(
            """
            UPDATE audit_events
            SET scope = CASE WHEN tenant_id IS NULL THEN 'platform' ELSE 'tenant' END,
                chain_scope = CASE
                    WHEN tenant_id IS NULL THEN 'platform'
                    ELSE 'tenant:' || tenant_id::text
                END
            """
        )
    )
    op.alter_column("audit_events", "chain_scope", nullable=False)
    op.create_index(
        "ix_audit_events_chain_scope",
        "audit_events",
        ["chain_scope"],
    )
    op.create_unique_constraint(
        "uq_audit_events_chain_scope_sequence",
        "audit_events",
        ["chain_scope", "chain_sequence"],
    )
    op.create_table(
        "audit_chain_checkpoints",
        sa.Column("chain_scope", sa.String(length=64), nullable=False),
        sa.Column("through_sequence", sa.Integer(), nullable=False),
        sa.Column("through_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("chain_scope"),
    )
    op.execute(
        """
        CREATE FUNCTION beework_guard_audit_mutation() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF current_setting('beework.audit_retention', true) = 'on' THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'audit records are append-only';
            END IF;
            IF current_setting('beework.audit_retention', true) = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'audit records are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_audit_events_append_only
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION beework_guard_audit_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_audit_chain_checkpoints_append_only
        BEFORE UPDATE OR DELETE ON audit_chain_checkpoints
        FOR EACH ROW EXECUTE FUNCTION beework_guard_audit_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_audit_chain_checkpoints_append_only ON audit_chain_checkpoints")
    op.execute("DROP TRIGGER trg_audit_events_append_only ON audit_events")
    op.execute("DROP FUNCTION beework_guard_audit_mutation()")
    op.drop_table("audit_chain_checkpoints")
    op.drop_constraint(
        "uq_audit_events_chain_scope_sequence",
        "audit_events",
        type_="unique",
    )
    op.drop_index("ix_audit_events_chain_scope", table_name="audit_events")
    op.drop_column("audit_events", "event_hash")
    op.drop_column("audit_events", "previous_hash")
    op.drop_column("audit_events", "chain_sequence")
    op.drop_column("audit_events", "chain_scope")
    op.drop_column("audit_events", "scope")
