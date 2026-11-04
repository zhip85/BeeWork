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
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base, utc_now
from beework.models.mixins import TenantOwnedMixin, TimestampMixin, VersionedMixin


class TenantApiKey(Base, TenantOwnedMixin):
    """A tenant API key for the open API (SET-090). Only the hash and a mask are stored; the plaintext is shown once."""

    __tablename__ = "tenant_api_keys"
    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_tenant_api_keys_hash"),
        Index("ix_tenant_api_keys_tenant", "tenant_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False, default="production")
    scopes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # chat / knowledge.read / ...
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    masked: Mapped[str] = mapped_column(String(40), nullable=False)  # first 8 chars + •••• + last 4
    created_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_by_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(60))


class ChannelInstance(Base, TenantOwnedMixin, TimestampMixin, VersionedMixin):
    """A channel an assistant is served through (PRD 4.9). Only ``website`` exists so far (T-040).

    Edits change the draft columns only; what visitors get is ``published_snapshot``, frozen at publish (RUL-003).
    """

    __tablename__ = "channel_instances"
    __table_args__ = (
        UniqueConstraint("site_key", name="uq_channel_instances_site_key"),
        UniqueConstraint("id", "tenant_id", name="uq_channel_instances_id_tenant_id"),
        ForeignKeyConstraint(
            ["assistant_id", "tenant_id"],
            ["assistants.id", "assistants.tenant_id"],
            ondelete="CASCADE",
            name="fk_channel_instances_assistant_same_tenant",
        ),
        Index("ix_channel_instances_assistant", "assistant_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    type: Mapped[str] = mapped_column(String(16), nullable=False, default="website")
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    assistant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False, default="production")
    allowed_origins: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Overrides of the inherited experience: primary_color, assistant_name, welcome (empty = inherit).
    experience: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")  # draft / active / disabled
    # Public identifier embedded in pages; it identifies the channel but authorises nothing by itself.
    site_key: Mapped[str] = mapped_column(String(40), nullable=False)
    published_snapshot: Mapped[dict | None] = mapped_column(JSON)
    version_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(64))
