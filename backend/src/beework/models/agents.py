from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base, utc_now
from beework.models.mixins import SoftDeleteMixin, TenantOwnedMixin, TimestampMixin, VersionedMixin


class Agent(Base, TenantOwnedMixin, TimestampMixin, VersionedMixin, SoftDeleteMixin):
    """A Chatflow or Workflow — shown as “流程画布” (演进 PRD 6、8). The table keeps its historical name ``agents``;
    the product's “智能体” is the ``assistants`` table (T-067).

    ``draft_config`` is what the editor saves and what debugging / test runs use; ``published_config`` is the snapshot of
    the latest published version and is the only one callers execute (RUL-002). Both hold ``FlowConfigV1``.
    """

    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_agents_id_tenant_id"),
        Index("ix_agents_tenant_kind", "tenant_id", "kind", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)  # chatflow | workflow
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    icon: Mapped[str] = mapped_column(String(16), nullable=False, default="chat")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")  # draft | published | disabled
    draft_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    published_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    live_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(64))


class FlowVersion(Base, TenantOwnedMixin):
    """An immutable published version of a flow; ``agents.live_version_id`` points at the latest one."""

    __tablename__ = "flow_versions"
    __table_args__ = (
        UniqueConstraint("flow_id", "version_seq", name="uq_flow_versions_seq"),
        ForeignKeyConstraint(
            ["flow_id", "tenant_id"], ["agents.id", "agents.tenant_id"], ondelete="CASCADE",
            name="fk_flow_versions_flow_same_tenant",
        ),
        Index("ix_flow_versions_flow", "flow_id", "version_seq"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    flow_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[str] = mapped_column(String(16), nullable=False)  # "v1", "v2", ...
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    change_note: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    published_by: Mapped[str | None] = mapped_column(String(64))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


@event.listens_for(FlowVersion, "before_update")
def _forbid_flow_version_update(_mapper, _connection, _target) -> None:
    raise ValueError("流程版本是不可变的，不能修改")
