from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

KbType = Literal["standard", "faq"]
Language = Literal["multilingual", "zh-CN", "en"]
EmbeddingModel = Literal["bge-m3", "text-embedding-3-large", "cohere-embed-v3"]
Chunking = Literal["semantic", "fixed_512", "by_heading"]
KbStatus = Literal["draft", "processing", "indexed"]
Engine = Literal["ragflow", "dify"]
SyncStatus = Literal["pending", "synced", "failed", "not_configured"]


class KnowledgeBaseCreate(BaseModel):
    kb_type: KbType = "standard"
    name: str = Field(min_length=1, max_length=50)
    description: str = Field(default="", max_length=500)
    language: Language = "multilingual"
    organization_id: UUID | None = None


class KnowledgeBaseUpdate(BaseModel):
    """Settings save (KNW-032); every field is sent, ``row_version`` guards against concurrent edits."""

    name: str = Field(min_length=1, max_length=50)
    description: str = Field(default="", max_length=500)
    language: Language
    kb_type: KbType
    embedding_model: EmbeddingModel
    chunking: Chunking
    retrieval_threshold: Decimal = Field(ge=0, le=1, decimal_places=2)
    row_version: int
    # The client confirmed that changing the model / chunking re-indexes every document.
    confirm_reindex: bool = False


class KnowledgeBaseResponse(BaseModel):
    id: UUID
    name: str
    description: str
    kb_type: KbType
    language: Language
    embedding_model: EmbeddingModel
    chunking: Chunking
    retrieval_threshold: Decimal
    organization_id: UUID
    organization_name: str
    owner_id: UUID | None
    created_by: str | None
    updated_by: str | None
    status: KbStatus
    docs_count: int
    faq_count: int
    engine: Engine
    sync_status: SyncStatus
    sync_error: str | None
    reindexing: bool
    row_version: int
    created_at: datetime
    updated_at: datetime
    can_edit: bool


class KnowledgeReferencesResponse(BaseModel):
    count: int


DocumentFormat = Literal["pdf", "docx", "html", "markdown", "txt", "url"]
DocumentStatus = Literal["processing", "indexed", "failed"]


class DocumentResponse(BaseModel):
    id: UUID
    kb_id: UUID
    name: str
    format: DocumentFormat
    source_url: str | None
    size_bytes: int
    chunks: int
    owner_name: str | None
    status: DocumentStatus
    error_message: str | None
    indexed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    can_download: bool


class DocumentPage(BaseModel):
    items: list[DocumentResponse]
    total: int
    limit: int
    offset: int
    counts: dict[str, int]  # status -> number of documents, plus "all"; for the status bar (KNW-030)


class UrlDocumentsRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=20)


class ChunkPreviewResponse(BaseModel):
    items: list[str]
    total: int
    page: int
