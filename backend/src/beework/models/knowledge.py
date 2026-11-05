from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
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
    """A knowledge base (PRD 4.6). Parsing, chunking and retrieval run in the knowledge engine (RAGFlow or Dify), one
    dataset per content kind: documents live in ``engine_dataset_id`` and FAQs in ``engine_faq_dataset_id``."""

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
    # The knowledge engine ("ragflow" | "dify") holding this knowledge base's datasets; fixed when it is created.
    engine: Mapped[str] = mapped_column(String(16), nullable=False, default="ragflow", server_default="ragflow")
    engine_dataset_id: Mapped[str | None] = mapped_column(String(64))
    engine_faq_dataset_id: Mapped[str | None] = mapped_column(String(64))
    # pending | synced | failed | not_configured: whether the RAGFlow datasets match this row.
    sync_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    sync_error: Mapped[str | None] = mapped_column(String(500))
    # Part of the dataset names, so a rebuild creates datasets beside the live ones instead of finding them again.
    dataset_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # True from a model / chunking change until the datasets are rebuilt; the old datasets keep serving meanwhile.
    reindexing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class KnowledgeDocument(Base, TenantOwnedMixin, TimestampMixin, SoftDeleteMixin):
    """A document of a knowledge base (PRD 4.6 Document). The original file stays in BeeWork's file store so it can
    be downloaded and re-indexed; parsing, chunking and embedding happen in the knowledge engine."""

    __tablename__ = "knowledge_documents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["kb_id", "tenant_id"],
            ["knowledge_bases.id", "knowledge_bases.tenant_id"],
            ondelete="CASCADE",
            name="fk_knowledge_documents_kb_same_tenant",
        ),
        Index("ix_knowledge_documents_kb_status", "kb_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    kb_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    format: Mapped[str] = mapped_column(String(16), nullable=False)  # pdf | docx | html | markdown | txt | url
    source_url: Mapped[str | None] = mapped_column(String(2048))
    file_key: Mapped[str | None] = mapped_column(String(200))  # None until a URL document has been fetched
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    chunks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    owner_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    owner_name: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="processing")  # processing|indexed|failed
    error_message: Mapped[str | None] = mapped_column(String(500))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Where the document currently is in the engine; cleared and refilled on re-indexing.
    engine_dataset_id: Mapped[str | None] = mapped_column(String(64))
    engine_document_id: Mapped[str | None] = mapped_column(String(64))
    engine_batch_id: Mapped[str | None] = mapped_column(String(64))
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
