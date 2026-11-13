"""Mail delivery records (T-008): who was mailed what, and whether it arrived.

The recipient is stored masked plus a SHA-256 hash (the hash powers the anti-abuse rate limit and
lookup); bodies and template context never enter this table.
"""
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base
from beework.models.mixins import TimestampMixin


class MailDelivery(Base, TimestampMixin):
    """One outbound mail. ``status``: ``queued`` (enqueued, worker has not sent it yet), ``sent``
    or ``failed`` (the last attempt errored; the queue keeps retrying until the job turns dead)."""

    __tablename__ = "mail_deliveries"
    __table_args__ = (
        Index("ix_mail_deliveries_recipient_template", "recipient_hash", "template", "created_at"),
        Index("ix_mail_deliveries_status_created", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    job_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    template: Mapped[str] = mapped_column(String(64), nullable=False)
    locale: Mapped[str] = mapped_column(String(8), nullable=False, default="zh-CN")
    recipient_masked: Mapped[str] = mapped_column(String(320), nullable=False)
    recipient_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="queued")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
