from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base, utc_now
from beework.models.mixins import TenantOwnedMixin, VersionedMixin


class DomainEvent(Base, TenantOwnedMixin):
    """The outbox of the event bus (T-087): written in the transaction that caused the event, dispatched by the Worker."""

    __tablename__ = "domain_events"
    __table_args__ = (Index("ix_domain_events_tenant_time", "tenant_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # ids and a short summary only
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebhookEndpoint(Base, TenantOwnedMixin, VersionedMixin):
    """A tenant's Webhook endpoint (SET-091). The signing secret is encrypted; it is shown once, when created or rotated."""

    __tablename__ = "webhook_endpoints"
    __table_args__ = (UniqueConstraint("id", "tenant_id", name="uq_webhook_endpoints_id_tenant_id"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    events: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    secret_last4: Mapped[str] = mapped_column(String(4), nullable=False, default="")
    # Start of the current run of failures; cleared by a success. 24 hours of it disables the endpoint.
    failing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(16))  # delivered | failed
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_reason: Mapped[str | None] = mapped_column(String(40))  # "failing" when the platform switched it off
    created_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class WebhookDelivery(Base, TenantOwnedMixin):
    """One event for one endpoint. ``id`` is the delivery id the receiver sees and it stays the same through every retry (dedupe)."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("endpoint_id", "event_id", name="uq_webhook_deliveries_event"),
        Index("ix_webhook_deliveries_endpoint_time", "endpoint_id", "created_at"),
        Index("ix_webhook_deliveries_status", "status", "next_retry_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    endpoint_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("webhook_endpoints.id", ondelete="CASCADE"), nullable=False)
    event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")  # pending | retrying | delivered | dead | cancelled
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status_code: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(200))
    is_test: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
