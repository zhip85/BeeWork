from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base
from beework.models.mixins import SoftDeleteMixin, TenantOwnedMixin, TimestampMixin, VersionedMixin


class FlowConnection(Base, TenantOwnedMixin, TimestampMixin, VersionedMixin, SoftDeleteMixin):
    """A Chatflow / Workflow *connection* (T-075, 演进 PRD 8): wraps a published flow of this platform (``native``) or an
    external Dify application (``dify``) as a resource that agents mount, OC routes pick and portal apps use.

    ``status``: ``enabled`` | ``disabled``. Whether the connection is usable is derived on read (``flow_connection_service.
    state_of``): a native one is *invalid* once its flow is disabled / deleted / unpublished. The Dify API key is encrypted
    (``model_secrets``) and only ever shown as a mask."""

    __tablename__ = "flow_connections"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_flow_connections_id_tenant_id"),
        Index("ix_flow_connections_tenant_kind", "tenant_id", "kind", "status"),
        Index("ix_flow_connections_flow", "tenant_id", "flow_id"),
        ForeignKeyConstraint(
            ["flow_id", "tenant_id"], ["agents.id", "agents.tenant_id"], name="fk_flow_connections_flow_same_tenant",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)  # chatflow | workflow
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    source_type: Mapped[str] = mapped_column(String(8), nullable=False, default="native")  # native | dify
    flow_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))  # native: the published flow in ``agents``
    base_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    app_id: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    credential_encrypted: Mapped[str | None] = mapped_column(String(2000))
    credential_last4: Mapped[str | None] = mapped_column(String(4))
    input_mapping: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    output_mapping: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    streaming: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="enabled")
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_test_error: Mapped[str | None] = mapped_column(String(300))
    last_test_ms: Mapped[int | None] = mapped_column()
    created_by: Mapped[str | None] = mapped_column(String(64))
