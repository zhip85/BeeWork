"""Tenant layer of the PII rule inheritance (T-052)."""

from uuid import UUID, uuid4

from sqlalchemy import Boolean, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base
from beework.models.mixins import TenantOwnedMixin, TimestampMixin, VersionedMixin


class TenantPiiRule(Base, TenantOwnedMixin, TimestampMixin, VersionedMixin):
    """A tenant PII rule: its own (``from_id`` empty) or an override of a platform rule (``from_id`` = that rule's id).

    ``from_id`` has no foreign key on purpose: deleting a platform rule turns overrides into rules of the tenant (PLT-133).
    """

    __tablename__ = "tenant_pii_rules"
    __table_args__ = (Index("ix_tenant_pii_rules_tenant_from", "tenant_id", "from_id"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    from_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    match_type: Mapped[str] = mapped_column(String(16), nullable=False, default="regex")
    pattern: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stage: Mapped[str] = mapped_column(String(8), nullable=False, default="both")
    action: Mapped[str] = mapped_column(String(8), nullable=False, default="mask")
    block_message: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class TenantPiiSettings(Base, TimestampMixin, VersionedMixin):
    """The tenant switch “inherit platform PII rules” (SET-051); a tenant without a row inherits."""

    __tablename__ = "tenant_pii_settings"

    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    inherit_platform: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
