from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base, utc_now
from beework.models.mixins import TenantOwnedMixin


class EndUser(Base, TenantOwnedMixin):
    """The person on the other side of a conversation (channel identity). Memory keys on this row (T-056)."""

    __tablename__ = "end_users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "channel_type", "external_id", name="uq_end_users_channel_identity"),
        UniqueConstraint("id", "tenant_id", name="uq_end_users_id_tenant_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    channel_type: Mapped[str] = mapped_column(String(24), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str | None] = mapped_column(String(120))
    vip: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    profile: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class Conversation(Base, TenantOwnedMixin):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_conversations_id_tenant_id"),
        ForeignKeyConstraint(
            ["assistant_id", "tenant_id"],
            ["assistants.id", "assistants.tenant_id"],
            ondelete="CASCADE",
            name="fk_conversations_assistant_same_tenant",
        ),
        Index("ix_conversations_assistant_active", "assistant_id", "last_active_at"),
        Index("ix_conversations_tenant_active", "tenant_id", "last_active_at"),
        Index("ix_conversations_status_active", "status", "last_active_at"),
        Index("ix_conversations_portal_owner", "tenant_id", "end_user_id", "last_active_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    assistant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    assistant_version: Mapped[str | None] = mapped_column(String(64))
    channel_instance_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))  # T-040
    environment: Mapped[str] = mapped_column(String(16), nullable=False, default="production")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="debug")
    end_user_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("end_users.id", ondelete="SET NULL"))
    end_user_name: Mapped[str | None] = mapped_column(String(120))
    country: Mapped[str | None] = mapped_column(String(8))
    intent: Mapped[str | None] = mapped_column(String(120))
    turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quality_score: Mapped[int | None] = mapped_column(Integer)
    csat: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="in_progress")
    created_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # AI portal conversations (T-143): the employee's own title / pin / project, the portal app it was started from and
    # their soft delete. Unused (NULL) for every other source.
    title: Mapped[str | None] = mapped_column(String(120))
    portal_app_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("portal_apps.id", ondelete="SET NULL"))
    project_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("portal_projects.id", ondelete="SET NULL"))
    pinned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Message(Base, TenantOwnedMixin):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uq_messages_conversation_seq"),
        ForeignKeyConstraint(
            ["conversation_id", "tenant_id"],
            ["conversations.id", "conversations.tenant_id"],
            ondelete="CASCADE",
            name="fk_messages_conversation_same_tenant",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    # Stored redacted when the tenant's "redact personal information in logs" policy is on.
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    widgets: Mapped[dict | None] = mapped_column(JSON)
    # Knowledge passages the answer was based on: [{document_name, chunk_id, snippet, kind, kb_name, score}].
    citations: Mapped[list | None] = mapped_column(JSON)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), index=True)
    feedback: Mapped[str | None] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class TenantCompliancePolicy(Base, TenantOwnedMixin):
    """Tenant data & privacy settings (SET-070). A missing row means defaults; see ``compliance.py``."""

    __tablename__ = "tenant_compliance_policies"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_tenant_compliance_policies_tenant"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False, default=180)
    redact_pii: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_export: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
