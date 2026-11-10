"""Tenant layer of the glossary inheritance (T-053)."""

from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base
from beework.models.mixins import TenantOwnedMixin, TimestampMixin, VersionedMixin


class TenantTerm(Base, TenantOwnedMixin, TimestampMixin, VersionedMixin):
    """A tenant term: its own (``from_id`` empty), an override of an inherited term, or a hidden marker for one.

    ``from_id`` points at a platform term and has no foreign key: deleting a platform term converts overrides and drops
    markers (``glossary.PLATFORM_TERM_DELETE_HOOKS``).
    """

    __tablename__ = "tenant_terms"
    __table_args__ = (Index("ux_tenant_terms_tenant_from", "tenant_id", "from_id", unique=True),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    from_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    term: Mapped[str] = mapped_column(String(200), nullable=False)
    term_key: Mapped[str] = mapped_column(String(200), nullable=False)
    aliases: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    definition: Mapped[str] = mapped_column(String(1000), nullable=False, default="")


class TenantGlossarySettings(Base, TimestampMixin, VersionedMixin):
    """The industry glossaries a tenant inherits, in order (SET-061); a tenant without a row inherits none."""

    __tablename__ = "tenant_glossary_settings"

    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    glossary_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
