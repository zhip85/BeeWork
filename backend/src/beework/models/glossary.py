"""Platform industry dictionary and industry glossaries (T-051, PRD 3.16.1)."""

from uuid import UUID, uuid4

from sqlalchemy import JSON, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base
from beework.models.mixins import TimestampMixin, VersionedMixin


class Industry(Base, TimestampMixin):
    """One entry of the unified industry dictionary every module references (PRD 8.3 #6)."""

    __tablename__ = "industries"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name_zh: Mapped[str] = mapped_column(String(60), nullable=False)
    name_en: Mapped[str] = mapped_column(String(80), nullable=False)
    sort_order: Mapped[int] = mapped_column(nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="active")  # active | disabled


class IndustryGlossary(Base, TimestampMixin, VersionedMixin):
    """A platform glossary of one industry; tenants may inherit it (T-053)."""

    __tablename__ = "industry_glossaries"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    industry_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    sort_order: Mapped[int] = mapped_column(nullable=False, default=0)


class PlatformTerm(Base, TimestampMixin, VersionedMixin):
    """A standard term with aliases and a definition; unique per glossary, case-insensitively."""

    __tablename__ = "platform_terms"
    __table_args__ = (
        UniqueConstraint("glossary_id", "term_key", name="uq_platform_terms_glossary_term"),
        Index("ix_platform_terms_glossary", "glossary_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    glossary_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("industry_glossaries.id", ondelete="CASCADE"), nullable=False)
    term: Mapped[str] = mapped_column(String(200), nullable=False)
    term_key: Mapped[str] = mapped_column(String(200), nullable=False)  # lower-cased term, the uniqueness key
    aliases: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    definition: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
