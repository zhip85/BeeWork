from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base
from beework.models.mixins import SoftDeleteMixin, TenantOwnedMixin, TimestampMixin, VersionedMixin


class Agent(Base, TenantOwnedMixin, TimestampMixin, VersionedMixin, SoftDeleteMixin):
    """An Agent, Chatflow or Workflow (PRD 4.5).

    ``draft_config`` is what the editor saves and what debugging / test runs use; ``available_config`` is the copy
    taken by “设为可用” and is the only one callers (assistants, routes, other agents) execute (RUL-002).
    Both hold a versioned DSL (``AgentConfigV1`` / ``FlowConfigV1``).
    """

    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_agents_id_tenant_id"),
        Index("ix_agents_tenant_kind", "tenant_id", "kind", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)  # agent | chatflow | workflow
    is_team: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    icon: Mapped[str] = mapped_column(String(16), nullable=False, default="brain")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")  # draft | available | disabled
    draft_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    available_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(64))
