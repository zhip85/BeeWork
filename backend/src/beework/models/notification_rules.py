from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base, utc_now
from beework.models.mixins import TenantOwnedMixin, VersionedMixin


class NotificationRule(Base, TenantOwnedMixin, VersionedMixin):
    """Who is told about which event, over which channel and how often (SET-100/101)."""

    __tablename__ = "notification_rules"
    __table_args__ = (Index("ix_notification_rules_tenant_event", "tenant_id", "event"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    event: Mapped[str] = mapped_column(String(32), nullable=False)  # one of the 12 notification events
    channel: Mapped[str] = mapped_column(String(12), nullable=False)  # email | slack | sms | webhook
    # email: {members: [user id], roles: [role code], emails: [address]}; slack: {url_sealed, url_mask}; sms: {phones: [...]}; webhook: {webhook_ids: [...]}
    recipients: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    frequency: Mapped[str] = mapped_column(String(10), nullable=False, default="realtime")  # realtime | hourly | daily
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class NotificationQueueItem(Base, TenantOwnedMixin):
    """An event waiting for the next hourly or daily digest of a rule."""

    __tablename__ = "notification_queue"
    __table_args__ = (
        UniqueConstraint("rule_id", "event_id", name="uq_notification_queue_event"),
        Index("ix_notification_queue_pending", "rule_id", "sent_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    rule_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("notification_rules.id", ondelete="CASCADE"), nullable=False)
    event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    rule_event: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
