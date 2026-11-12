"""Retire the self-built flow canvas (T-169)

Drops ``flow_versions`` and ``agents`` (the canvas of T-060) and ``flow_connections.flow_id``. The migration refuses to run
while live (not soft-deleted) canvases exist, so data is never lost silently: export or delete them first, or set
``BEEWORK_DROP_FLOW_CANVAS=1`` to confirm that they may be discarded. Reference rows of the canvas are removed; a leftover
``native`` flow connection (it pointed at a canvas) is disabled and shows as invalid. The downgrade recreates the empty tables.

Revision ID: 0051_retire_flow_canvas
Revises: 0050_api_plugins
Create Date: 2026-11-12 18:00:00.000000

"""
import os
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0051_retire_flow_canvas'
down_revision: str | None = '0050_api_plugins'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    live = bind.execute(sa.text("SELECT count(*) FROM agents WHERE deleted_at IS NULL")).scalar() or 0
    if live and os.environ.get("BEEWORK_DROP_FLOW_CANVAS") != "1":
        raise RuntimeError(
            f"发现 {live} 个未删除的流程画布，自研画布已取消，删除这些数据前需要确认。"
            "请先导出需要保留的内容，然后设置环境变量 BEEWORK_DROP_FLOW_CANVAS=1 再重试迁移。"
        )
    op.execute("DELETE FROM resource_references WHERE referrer_type = 'flow' OR target_type = 'flow'")
    op.execute("UPDATE flow_connections SET status = 'disabled' WHERE source_type = 'native' AND deleted_at IS NULL")
    op.drop_constraint('fk_flow_connections_flow_same_tenant', 'flow_connections', type_='foreignkey')
    op.drop_index('ix_flow_connections_flow', table_name='flow_connections')
    op.drop_column('flow_connections', 'flow_id')
    op.drop_index(op.f('ix_flow_versions_tenant_id'), table_name='flow_versions')
    op.drop_index('ix_flow_versions_flow', table_name='flow_versions')
    op.drop_table('flow_versions')
    op.drop_index('ix_agents_tenant_kind', table_name='agents')
    op.drop_index(op.f('ix_agents_tenant_id'), table_name='agents')
    op.drop_table('agents')


def downgrade() -> None:
    op.create_table('agents',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=12), nullable=False),
    sa.Column('name', sa.String(length=50), nullable=False),
    sa.Column('description', sa.String(length=200), nullable=False),
    sa.Column('icon', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('draft_config', sa.JSON(), nullable=False),
    sa.Column('published_config', sa.JSON(), nullable=True),
    sa.Column('live_version_id', sa.Uuid(), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.String(length=64), nullable=True),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('row_version', sa.Integer(), nullable=False),
    sa.Column('updated_by', sa.String(length=64), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('id', 'tenant_id', name='uq_agents_id_tenant_id')
    )
    op.create_index(op.f('ix_agents_tenant_id'), 'agents', ['tenant_id'], unique=False)
    op.create_index('ix_agents_tenant_kind', 'agents', ['tenant_id', 'kind', 'status'], unique=False)
    op.create_table('flow_versions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('flow_id', sa.Uuid(), nullable=False),
    sa.Column('version_seq', sa.Integer(), nullable=False),
    sa.Column('version', sa.String(length=16), nullable=False),
    sa.Column('snapshot', sa.JSON(), nullable=False),
    sa.Column('change_note', sa.String(length=500), nullable=False),
    sa.Column('published_by', sa.String(length=64), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['flow_id', 'tenant_id'], ['agents.id', 'agents.tenant_id'], name='fk_flow_versions_flow_same_tenant', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('flow_id', 'version_seq', name='uq_flow_versions_seq')
    )
    op.create_index('ix_flow_versions_flow', 'flow_versions', ['flow_id', 'version_seq'], unique=False)
    op.create_index(op.f('ix_flow_versions_tenant_id'), 'flow_versions', ['tenant_id'], unique=False)
    op.add_column('flow_connections', sa.Column('flow_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('fk_flow_connections_flow_same_tenant', 'flow_connections', 'agents', ['flow_id', 'tenant_id'], ['id', 'tenant_id'])
    op.create_index('ix_flow_connections_flow', 'flow_connections', ['tenant_id', 'flow_id'], unique=False)
