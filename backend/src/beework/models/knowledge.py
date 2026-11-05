from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base
from beework.models.mixins import SoftDeleteMixin, TenantOwnedMixin, TimestampMixin, VersionedMixin


class KnowledgeBase(Base, TenantOwnedMixin, TimestampMixin, VersionedMixin, SoftDeleteMixin):
    """A knowledge base (PRD 4.6). Parsing, chunking and retrieval run in RAGFlow, one dataset per content kind:
    documents live in ``ragflow_dataset_id`` and FAQs (``qa`` method) in ``ragflow_faq_dataset_id``."""

    __tablename__ = "knowledge_bases"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_knowledge_bases_id_tenant_id"),
        ForeignKeyConstraint(
            ["organization_id", "tenant_id"],
            ["organizations.id", "organizations.tenant_id"],
            ondelete="RESTRICT",
            name="fk_knowledge_bases_organization_same_tenant",
        ),
        Index("ix_knowledge_bases_tenant_type", "tenant_id", "kb_type"),
        Index("ix_knowledge_bases_organization_id", "organization_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    kb_type: Mapped[str] = mapped_column(String(16), nullable=False, default="standard")  # standard | faq
    language: Mapped[str] = mapped_column(String(16), nullable=False, default="multilingual")
    embedding_model: Mapped[str] = mapped_column(String(80), nullable=False, default="bge-m3")
    chunking: Mapped[str] = mapped_column(String(24), nullable=False, default="semantic")
    retrieval_threshold: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False, default=Decimal("0.62"))
    owner_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_by: Mapped[str | None] = mapped_column(String(64))
    ragflow_dataset_id: Mapped[str | None] = mapped_column(String(64))
    ragflow_faq_dataset_id: Mapped[str | None] = mapped_column(String(64))
    # pending | synced | failed | not_configured: whether the RAGFlow datasets match this row.
    sync_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    sync_error: Mapped[str | None] = mapped_column(String(500))
    # Part of the dataset names, so a rebuild creates datasets beside the live ones instead of finding them again.
    dataset_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # True from a model / chunking change until the datasets are rebuilt; the old datasets keep serving meanwhile.
    reindexing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
