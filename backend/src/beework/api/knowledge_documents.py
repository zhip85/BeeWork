"""Documents of a knowledge base: upload, URL import, list, chunk preview, re-index, download and delete (T-031)."""

from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework import documents as documents_service
from beework import file_store
from beework.api.conventions import ApiError, Pagination, get_pagination, paginate
from beework.api.knowledge import _actor_name, _load
from beework.auth import require_csrf
from beework.db import get_db
from beework.integrations.knowledge_engine import (
    KnowledgeEngineError,
    engine_configured,
    open_engine,
)
from beework.models import AuditEvent, Membership
from beework.models.knowledge import KnowledgeBase, KnowledgeDocument
from beework.rbac import require_tenant_permission
from beework.schemas.knowledge import (
    ChunkPreviewResponse,
    DocumentPage,
    DocumentResponse,
    UrlDocumentsRequest,
)

router = APIRouter()
CONFLICT_CHOICES = ("ask", "overwrite", "keep_both")


def _response(document: KnowledgeDocument) -> DocumentResponse:
    return DocumentResponse(
        id=document.id, kb_id=document.kb_id, name=document.name, format=document.format,
        source_url=document.source_url, size_bytes=document.size_bytes, chunks=document.chunks,
        owner_name=document.owner_name, status=document.status, error_message=document.error_message,
        indexed_at=document.indexed_at, created_at=document.created_at, updated_at=document.updated_at,
        can_download=document.file_key is not None and document.format != "url",
    )


def _standard_kb(db: Session, membership: Membership, kb_id: UUID, *, lock: bool = False) -> KnowledgeBase:
    kb = _load(db, membership, kb_id, lock=lock)
    if kb.kb_type != "standard":
        raise ApiError(422, "knowledge.faq_no_documents", "FAQ 知识库不提供文档上传")
    return kb


def _load_document(db: Session, membership: Membership, kb_id: UUID, document_id: UUID) -> tuple[KnowledgeBase, KnowledgeDocument]:
    kb = _load(db, membership, kb_id)
    document = db.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.id == document_id,
            KnowledgeDocument.kb_id == kb.id,
            KnowledgeDocument.tenant_id == membership.tenant_id,
            KnowledgeDocument.deleted_at.is_(None),
        )
    )
    if document is None:
        raise ApiError(404, "not_found", "未找到指定文档")
    return kb, document


def _audit(db: Session, membership: Membership, action: str, kb: KnowledgeBase, document: KnowledgeDocument, **extra: str) -> None:
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id, actor_user_id=membership.user_id, action=action,
            details={"knowledge_base_id": str(kb.id), "document_id": str(document.id), "name": document.name, **extra},
        )
    )


@router.get("/knowledge-bases/{kb_id}/documents", response_model=DocumentPage)
def list_documents(
    kb_id: UUID,
    q: str | None = Query(default=None, max_length=100),
    status_filter: str | None = Query(default=None, alias="status", pattern="^(processing|indexed|failed)$"),
    pagination: Pagination = Depends(get_pagination),
    membership: Membership = Depends(require_tenant_permission("knowledge:view")),
    db: Session = Depends(get_db),
) -> DocumentPage:
    kb = _load(db, membership, kb_id)
    base = select(KnowledgeDocument).where(KnowledgeDocument.kb_id == kb.id, KnowledgeDocument.deleted_at.is_(None))
    searched = base
    if q and q.strip():
        searched = searched.where(func.lower(KnowledgeDocument.name).like(f"%{q.strip().lower()}%"))
    counts = {"processing": 0, "indexed": 0, "failed": 0}
    matching_ids = select(searched.subquery().c.id)
    for state, number in db.execute(
        select(KnowledgeDocument.status, func.count())
        .where(KnowledgeDocument.id.in_(matching_ids))
        .group_by(KnowledgeDocument.status)
    ):
        counts[state] = number
    counts["all"] = sum(counts.values())
    statement = searched.where(KnowledgeDocument.status == status_filter) if status_filter else searched
    items, total = paginate(db, statement.order_by(KnowledgeDocument.created_at.desc(), KnowledgeDocument.id), pagination)
    return DocumentPage(
        items=[_response(item) for item in items], total=total, limit=pagination.limit, offset=pagination.offset, counts=counts
    )


@router.post("/knowledge-bases/{kb_id}/documents", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    kb_id: UUID,
    request: Request,
    filename: str = Query(min_length=1, max_length=300),
    on_conflict: str = Query(default="ask", pattern="^(ask|overwrite|keep_both)$"),
    membership: Membership = Depends(require_tenant_permission("knowledge:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> DocumentResponse:
    """One file per request, sent as the raw request body (the browser uploads a batch one file at a time)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > documents_service.MAX_FILE_BYTES:
        raise ApiError(413, "knowledge.file_too_large", "单个文件不能超过 50 MB")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():  # never buffer more than the limit, whatever Content-Length claimed
        total += len(chunk)
        if total > documents_service.MAX_FILE_BYTES:
            raise ApiError(413, "knowledge.file_too_large", "单个文件不能超过 50 MB")
        chunks.append(chunk)
    data = b"".join(chunks)
    return await run_in_threadpool(_store_upload, db, membership, kb_id, filename, data, on_conflict)


def _store_upload(db: Session, membership: Membership, kb_id: UUID, filename: str, data: bytes, on_conflict: str) -> DocumentResponse:
    kb = _standard_kb(db, membership, kb_id, lock=True)
    document = documents_service.create_file_document(
        db, kb, filename=filename, data=data, owner_id=membership.user_id, owner_name=_actor_name(db, membership),
        on_conflict=on_conflict,
    )
    _audit(db, membership, "knowledge.document_uploaded", kb, document, size=str(len(data)), on_conflict=on_conflict)
    db.commit()
    return _response(document)


@router.post("/knowledge-bases/{kb_id}/documents/urls", response_model=list[DocumentResponse], status_code=status.HTTP_201_CREATED)
def import_urls(
    kb_id: UUID,
    payload: UrlDocumentsRequest,
    membership: Membership = Depends(require_tenant_permission("knowledge:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> list[DocumentResponse]:
    kb = _standard_kb(db, membership, kb_id, lock=True)
    actor = _actor_name(db, membership)
    urls = list(dict.fromkeys(url.strip() for url in payload.urls if url.strip()))
    created = [
        documents_service.create_url_document(db, kb, url=url, owner_id=membership.user_id, owner_name=actor)
        for url in urls  # validates every URL first: one bad address rejects the whole request, nothing is queued
    ]
    for document in created:
        _audit(db, membership, "knowledge.document_url_added", kb, document, url=document.source_url or "")
    db.commit()
    return [_response(document) for document in created]


@router.get("/knowledge-bases/{kb_id}/documents/{document_id}", response_model=DocumentResponse)
def get_document(
    kb_id: UUID,
    document_id: UUID,
    membership: Membership = Depends(require_tenant_permission("knowledge:view")),
    db: Session = Depends(get_db),
) -> DocumentResponse:
    return _response(_load_document(db, membership, kb_id, document_id)[1])


@router.get("/knowledge-bases/{kb_id}/documents/{document_id}/chunks", response_model=ChunkPreviewResponse)
def preview_chunks(
    kb_id: UUID,
    document_id: UUID,
    page: int = Query(default=1, ge=1, le=10000),
    size: int = Query(default=20, ge=1, le=50),
    membership: Membership = Depends(require_tenant_permission("knowledge:view")),
    db: Session = Depends(get_db),
) -> ChunkPreviewResponse:
    kb, document = _load_document(db, membership, kb_id, document_id)
    if document.status != "indexed" or not document.engine_document_id or not document.engine_dataset_id:
        raise ApiError(409, "knowledge.not_indexed", "文档尚未完成索引，暂无分片可预览")
    if not engine_configured(kb.engine):
        raise ApiError(503, "knowledge.engine_unavailable", "知识引擎未配置")
    try:
        with open_engine(kb.engine) as engine:
            items, total = engine.list_chunks(document.engine_dataset_id, document.engine_document_id, page=page, size=size)
    except KnowledgeEngineError as exc:
        raise ApiError(502, "knowledge.engine_error", str(exc)[:300]) from exc
    return ChunkPreviewResponse(items=items, total=total, page=page)


@router.post("/knowledge-bases/{kb_id}/documents/{document_id}/reindex", response_model=DocumentResponse)
def reindex(
    kb_id: UUID,
    document_id: UUID,
    membership: Membership = Depends(require_tenant_permission("knowledge:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> DocumentResponse:
    kb, document = _load_document(db, membership, kb_id, document_id)
    if document.status == "processing":
        raise ApiError(409, "knowledge.processing", "文档正在处理中")
    documents_service.reindex_document(db, document)
    _audit(db, membership, "knowledge.document_reindexed", kb, document)
    db.commit()
    return _response(document)


@router.get("/knowledge-bases/{kb_id}/documents/{document_id}/download")
def download(
    kb_id: UUID,
    document_id: UUID,
    membership: Membership = Depends(require_tenant_permission("knowledge:view")),
    db: Session = Depends(get_db),
) -> Response:
    _, document = _load_document(db, membership, kb_id, document_id)
    if document.format == "url" or not document.file_key or not file_store.exists(document.file_key):
        return JSONResponse({"detail": "该文档没有可下载的原文件", "code": "knowledge.no_file", "url": document.source_url}, status_code=409)
    return Response(
        content=file_store.read(document.file_key),
        media_type="application/octet-stream",  # never let a browser render an uploaded HTML file on our origin
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(document.name)}",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        },
    )


@router.delete("/knowledge-bases/{kb_id}/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    kb_id: UUID,
    document_id: UUID,
    membership: Membership = Depends(require_tenant_permission("knowledge:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    kb, document = _load_document(db, membership, kb_id, document_id)
    documents_service.remove_document(db, document)
    _audit(db, membership, "knowledge.document_deleted", kb, document)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
