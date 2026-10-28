from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKeyConstraint, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base, utc_now
from beework.models.mixins import TenantOwnedMixin


class EndUserMemory(Base, TenantOwnedMixin):
    """One long-term memory entry about an end user (PRD 3.6.4). Always stored PII-redacted.

    ``profile`` entries (UserProfile) have no assistant and are shared by every assistant of the
    tenant; ``memory`` (UserMemory) and ``session`` (conversation summaries) are private to one assistant.
    """

    __tablename__ = "end_user_memories"
    __table_args__ = (
        ForeignKeyConstraint(
            ["end_user_id", "tenant_id"],
            ["end_users.id", "end_users.tenant_id"],
            ondelete="CASCADE",
            name="fk_end_user_memories_end_user_same_tenant",
        ),
        ForeignKeyConstraint(
            ["assistant_id", "tenant_id"],
            ["assistants.id", "assistants.tenant_id"],
            ondelete="CASCADE",
            name="fk_end_user_memories_assistant_same_tenant",
        ),
        Index("ix_end_user_memories_lookup", "tenant_id", "end_user_id", "file"),
        Index("ix_end_user_memories_source", "source_conversation_id", "file"),
        Index("ix_end_user_memories_expires", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    end_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    assistant_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    file: Mapped[str] = mapped_column(String(16), nullable=False)  # profile | memory | session
    type: Mapped[str | None] = mapped_column(String(16))  # preference | history | fact (UserMemory)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_conversation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
