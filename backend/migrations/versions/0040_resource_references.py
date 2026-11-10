"""Resource references: who mounts / calls what (T-070)

Revision ID: 0040_resource_references
Revises: 0039_tool_invocations
Create Date: 2026-11-11 09:00:00.000000

"""
import json
import uuid
from datetime import UTC, datetime
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0040_resource_references'
down_revision: str | None = '0039_tool_invocations'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TARGET_FOR_MOUNT = {
    "kb": "kb", "agent": "assistant", "skill": "skill", "mcp": "mcp", "api": "api",
    "chatflow": "chatflow_connection", "flow": "workflow_connection",
}
NODE_TARGET = {"assistant": "assistant", "flow": "flow", "connection": "connection"}


def upgrade() -> None:
    table = op.create_table('resource_references',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('referrer_type', sa.String(length=16), nullable=False),
    sa.Column('referrer_id', sa.Uuid(), nullable=False),
    sa.Column('layer', sa.String(length=8), nullable=False),
    sa.Column('relation', sa.String(length=16), nullable=False),
    sa.Column('target_type', sa.String(length=24), nullable=False),
    sa.Column('target_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('referrer_type', 'referrer_id', 'layer', 'relation', 'target_type', 'target_id', name='uq_resource_references_edge')
    )
    op.create_index(op.f('ix_resource_references_tenant_id'), 'resource_references', ['tenant_id'], unique=False)
    op.create_index('ix_resource_references_target', 'resource_references', ['tenant_id', 'target_type', 'target_id'], unique=False)
    op.create_index('ix_resource_references_referrer', 'resource_references', ['referrer_type', 'referrer_id', 'layer'], unique=False)
    _backfill(op.get_bind(), table)


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return value or {}


def _backfill(bind, table) -> None:
    """Existing assistants (draft and live version) and flows get their rows, so counts are right from day one."""
    now = datetime.now(UTC)
    rows: dict[tuple, dict] = {}

    def add(tenant_id, referrer_type, referrer_id, layer, relation, target_type, target_id) -> None:
        try:
            target = uuid.UUID(str(target_id))
        except ValueError:
            return
        key = (referrer_type, referrer_id, layer, relation, target_type, target)
        rows[key] = {
            "id": uuid.uuid4(), "tenant_id": tenant_id, "referrer_type": referrer_type, "referrer_id": referrer_id,
            "layer": layer, "relation": relation, "target_type": target_type, "target_id": target, "created_at": now,
        }

    def mounts_of(config, tenant_id, referrer_id, layer) -> None:
        mounts = (_json(config).get("mounts") or {})
        for mount_kind, target_type in TARGET_FOR_MOUNT.items():
            for value in mounts.get(mount_kind) or []:
                add(tenant_id, "assistant", referrer_id, layer, "mount", target_type, value)

    for row in bind.execute(sa.text("SELECT id, tenant_id, draft_config, live_version_id FROM assistants WHERE deleted_at IS NULL")):
        mounts_of(row.draft_config, row.tenant_id, row.id, "draft")
        if row.live_version_id is not None:
            snapshot = bind.execute(
                sa.text("SELECT snapshot FROM assistant_versions WHERE id = :id"), {"id": row.live_version_id}
            ).scalar()
            mounts_of(_json(snapshot).get("config"), row.tenant_id, row.id, "live")
    for row in bind.execute(sa.text("SELECT id, tenant_id, draft_config, published_config FROM agents WHERE deleted_at IS NULL")):
        for layer, config in (("draft", row.draft_config), ("live", row.published_config)):
            for node in (_json(config).get("nodes") or []):
                target_type = NODE_TARGET.get(node.get("target_kind") or "")
                if target_type and node.get("target_id") and node.get("type") != "note":
                    add(row.tenant_id, "flow", row.id, layer, "node", target_type, node["target_id"])
    if rows:
        op.bulk_insert(table, list(rows.values()))


def downgrade() -> None:
    op.drop_index('ix_resource_references_referrer', table_name='resource_references')
    op.drop_index('ix_resource_references_target', table_name='resource_references')
    op.drop_index(op.f('ix_resource_references_tenant_id'), table_name='resource_references')
    op.drop_table('resource_references')
