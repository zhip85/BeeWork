from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Date, DateTime, ForeignKeyConstraint, Index, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base, utc_now
from beework.models.mixins import SoftDeleteMixin, TenantOwnedMixin, TimestampMixin, VersionedMixin


class Faq(Base, TenantOwnedMixin, TimestampMixin, VersionedMixin, SoftDeleteMixin):
    """A standard question–answer of a knowledge base (PRD 4.6 Faq). BeeWork is the source of truth; only
    ``published`` FAQs live in the engine's FAQ dataset, one single-chunk document each."""

    __tablename__ = "faqs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["kb_id", "tenant_id"],
            ["knowledge_bases.id", "knowledge_bases.tenant_id"],
            ondelete="CASCADE",
            name="fk_faqs_kb_same_tenant",
        ),
        Index("ix_faqs_kb_status", "kb_id", "status"),
        Index("ix_faqs_engine_document", "engine_document_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    kb_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    question: Mapped[str] = mapped_column(String(200), nullable=False)
    similar_questions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    answer: Mapped[str] = mapped_column(String(2000), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="draft")  # draft | published | expired
    expires_at: Mapped[date | None] = mapped_column(Date)  # the last day it is served; the day after it expires
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(64))
    # none | pending | processing | synced | failed | not_configured: whether the engine holds this FAQ for retrieval.
    sync_status: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    sync_error: Mapped[str | None] = mapped_column(String(500))
    sync_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    engine_dataset_id: Mapped[str | None] = mapped_column(String(64))
    engine_document_id: Mapped[str | None] = mapped_column(String(64))
    engine_batch_id: Mapped[str | None] = mapped_column(String(64))

class FaqHit(Base, TenantOwnedMixin):
    """One retrieval that answered from an FAQ; ``hits_30d`` counts the last 30 days."""

    __tablename__ = "faq_hits"
    __table_args__ = (Index("ix_faq_hits_faq_created", "faq_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    faq_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
