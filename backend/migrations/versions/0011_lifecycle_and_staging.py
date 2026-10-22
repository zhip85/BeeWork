"""Tenant lifecycle review, staging environment rename and models permission.

Revision ID: 0011_lifecycle_and_staging
Revises: 0010_tenant_model_environments
Create Date: 2026-10-22
"""
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_lifecycle_and_staging"
down_revision: str | None = "0010_tenant_model_environments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _copy_settings_to_models(permissions: dict, reverse: bool = False) -> dict:
    updated = dict(permissions)
    if reverse:
        updated.pop("models", None)
        return updated
    settings_actions = [action for action in updated.get("settings", []) if action in {"view", "edit", "delete"}]
    if settings_actions:
        updated["models"] = sorted(set(updated.get("models", [])) | set(settings_actions))
    return updated


def _rewrite_custom_role_permissions(reverse: bool = False) -> None:
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, permissions FROM tenant_roles")).all()
    for role_id, permissions in rows:
        if isinstance(permissions, str):
            permissions = json.loads(permissions)
        updated = _copy_settings_to_models(permissions or {}, reverse=reverse)
        if updated != permissions:
            connection.execute(
                sa.text("UPDATE tenant_roles SET permissions = CAST(:permissions AS JSON) WHERE id = :id"),
                {"permissions": json.dumps(updated), "id": role_id},
            )


def upgrade() -> None:
    op.add_column("tenants", sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tenants", sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "platform_operation_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(length=48), nullable=False),
        sa.Column("target_tenant_id", sa.Uuid(), nullable=False),
        sa.Column("from_status", sa.String(length=24), nullable=False),
        sa.Column("to_status", sa.String(length=24), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("review_comment", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["target_tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_platform_operation_requests_target_tenant_id",
        "platform_operation_requests",
        ["target_tenant_id"],
    )
    op.create_index(
        "ix_platform_operation_requests_status_created",
        "platform_operation_requests",
        ["status", "created_at"],
    )
    op.execute(
        "UPDATE tenant_model_environments SET environment = 'staging' WHERE environment = 'testing'"
    )
    _rewrite_custom_role_permissions()


def downgrade() -> None:
    _rewrite_custom_role_permissions(reverse=True)
    op.execute(
        "UPDATE tenant_model_environments SET environment = 'testing' WHERE environment = 'staging'"
    )
    op.drop_index("ix_platform_operation_requests_status_created", table_name="platform_operation_requests")
    op.drop_index("ix_platform_operation_requests_target_tenant_id", table_name="platform_operation_requests")
    op.drop_table("platform_operation_requests")
    op.drop_column("tenants", "status_changed_at")
    op.drop_column("tenants", "trial_ends_at")
