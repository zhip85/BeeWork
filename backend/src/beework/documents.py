"""Knowledge documents (T-031): validation, storage, hand-over to the engine, status polling, re-index and delete.

The API only validates, stores the original file and queues a Worker job; the engine (RAGFlow or Dify) parses, chunks
and embeds. A document is ``processing`` from upload until the engine reports it indexed or failed. Everything the
Worker does is repeatable: an interrupted upload or poll is simply retried, and a periodic sweep fails documents that
have been processing for too long, so none stays "processing" forever.
"""

import io
import logging
import math
import re
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit
from uuid import UUID, uuid4

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework import file_store
from beework.api.conventions import validate_public_https_url
from beework.entitlements import get_subscription, register_usage_counter
from beework.errors import ApiError
from beework.integrations.knowledge_engine import (
    DocumentRef,
    KnowledgeEngineError,
    engine_configured,
    open_engine,
)
from beework.knowledge import (
    COPY_HOOKS,
    DELETE_HOOKS,
    REINDEX_HOOKS,
    ContentStats,
    register_content_provider,
)
from beework.models.knowledge import KnowledgeBase, KnowledgeDocument
from beework.worker.queue import enqueue, job_handler

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_FILES_PER_BATCH = 20
MAX_URL_BYTES = 20 * 1024 * 1024
MAX_REDIRECTS = 3
PROCESSING_TIMEOUT = timedelta(hours=1)
JOB_ATTEMPTS = 8
GIB = 1024**3

EXTENSIONS = {
    ".pdf": "pdf", ".docx": "docx", ".html": "html", ".htm": "html",
    ".md": "markdown", ".markdown": "markdown", ".txt": "txt",
}
ACCEPTED_TEXT_ENCODINGS = ("utf-8", "gb18030")


# ---------------------------------------------------------------------------
# Validation.
# ---------------------------------------------------------------------------


def clean_filename(raw: str) -> str:
    name = re.split(r"[\\/]", raw or "")[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip().strip(".")
    if not name:
        raise ApiError(422, "knowledge.invalid_filename", "文件名无效")
    if len(name) > 200:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: 200 - len(ext) - 1] + "." + ext) if dot else name[:200]
    return name


def sniff_format(filename: str, data: bytes) -> str:
    """Extension AND content must agree: a renamed executable is not a PDF."""
    extension = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    document_format = EXTENSIONS.get(extension)
    if document_format is None:
        raise ApiError(422, "knowledge.unsupported_file", "仅支持 PDF、DOCX、HTML、Markdown、TXT 文件")
    if not data:
        raise ApiError(422, "knowledge.empty_file", "文件是空的")
    if len(data) > MAX_FILE_BYTES:
        raise ApiError(413, "knowledge.file_too_large", "单个文件不能超过 50 MB")
    if document_format == "pdf":
        ok = data[:5] == b"%PDF-"
    elif document_format == "docx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                ok = "word/document.xml" in archive.namelist()
        except zipfile.BadZipFile:
            ok = False
    else:
        ok = b"\x00" not in data[:8192] and _decodes(data)
    if not ok:
        raise ApiError(422, "knowledge.content_mismatch", "文件内容与扩展名不符，或文件已损坏")
    return document_format


def _decodes(data: bytes) -> bool:
    for encoding in ACCEPTED_TEXT_ENCODINGS:
        try:
            data[:65536].decode(encoding)
            return True
        except UnicodeDecodeError:
            continue
        # A multi-byte character cut at the 64 KB boundary is not corruption.
    return False


# ---------------------------------------------------------------------------
# Storage quota (T-010 ``storage_gb``).
# ---------------------------------------------------------------------------


def storage_used_bytes(db: Session, tenant_id: UUID) -> int:
    return db.scalar(
        select(func.coalesce(func.sum(KnowledgeDocument.size_bytes), 0)).where(
            KnowledgeDocument.tenant_id == tenant_id, KnowledgeDocument.deleted_at.is_(None)
        )
    ) or 0


def _storage_gb_used(db: Session, tenant_id: UUID) -> int:
    return math.ceil(storage_used_bytes(db, tenant_id) / GIB)


register_usage_counter("storage_gb", _storage_gb_used)


def ensure_storage(db: Session, tenant_id: UUID, add_bytes: int) -> None:
    subscription = get_subscription(db, tenant_id)
    limit = subscription.quotas.get("storage_gb")
    policy = subscription.overage_policy.get("storage_gb", "hard")
    if limit is not None and policy == "hard" and storage_used_bytes(db, tenant_id) + add_bytes > limit * GIB:
        raise ApiError(409, "plan.quota_exceeded", f"存储空间已达套餐上限（{limit} GB），请联系平台升级")


# ---------------------------------------------------------------------------
# Creating, replacing, re-indexing and deleting documents (inside the caller's transaction).
# ---------------------------------------------------------------------------


def _live_documents(kb_id: UUID):
    return select(KnowledgeDocument).where(KnowledgeDocument.kb_id == kb_id, KnowledgeDocument.deleted_at.is_(None))


def find_by_name(db: Session, kb_id: UUID, name: str) -> KnowledgeDocument | None:
    return db.scalar(_live_documents(kb_id).where(func.lower(KnowledgeDocument.name) == name.lower()))


def unique_name(db: Session, kb_id: UUID, name: str) -> str:
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    index = 2
    candidate = name
    while find_by_name(db, kb_id, candidate) is not None:
        candidate = f"{stem} ({index}){'.' + ext if ext else ''}"
        index += 1
    return candidate


def _queue_ingest(db: Session, document: KnowledgeDocument) -> None:
    kind = "knowledge.upload_document" if document.file_key else "knowledge.fetch_url"
    enqueue(
        db, kind, {"document_id": str(document.id)}, tenant_id=document.tenant_id,
        idempotency_key=f"{kind}:{document.id}:{uuid4().hex}", max_attempts=JOB_ATTEMPTS,
    )


def create_file_document(
    db: Session, kb: KnowledgeBase, *, filename: str, data: bytes, owner_id: UUID | None, owner_name: str | None,
    on_conflict: str = "ask",
) -> KnowledgeDocument:
    name = clean_filename(filename)
    document_format = sniff_format(name, data)
    existing = find_by_name(db, kb.id, name)
    if existing is not None:
        if on_conflict == "ask":
            raise ApiError(409, "knowledge.duplicate_name", f"已有同名文档“{name}”，请选择覆盖或保留两者")
        if on_conflict == "overwrite":
            remove_document(db, existing)
            db.flush()
        else:
            name = unique_name(db, kb.id, name)
    ensure_storage(db, kb.tenant_id, len(data))
    document_id = uuid4()
    key = file_store.make_key(kb.tenant_id, kb.id, document_id)
    file_store.save(key, data)
    document = KnowledgeDocument(
        id=document_id, tenant_id=kb.tenant_id, kb_id=kb.id, name=name, format=document_format, file_key=key,
        size_bytes=len(data), owner_id=owner_id, owner_name=owner_name, status="processing",
        processing_started_at=datetime.now(UTC),
    )
    db.add(document)
    db.flush()
    _queue_ingest(db, document)
    return document


def create_url_document(
    db: Session, kb: KnowledgeBase, *, url: str, owner_id: UUID | None, owner_name: str | None
) -> KnowledgeDocument:
    validate_public_https_url(url)
    parts = urlsplit(url)
    name = (parts.hostname or "") + (parts.path if parts.path != "/" else "")
    document = KnowledgeDocument(
        tenant_id=kb.tenant_id, kb_id=kb.id, name=unique_name(db, kb.id, name[:200]), format="url", source_url=url,
        owner_id=owner_id, owner_name=owner_name, status="processing", processing_started_at=datetime.now(UTC),
    )
    db.add(document)
    db.flush()
    _queue_ingest(db, document)
    return document


def _queue_engine_removal(db: Session, document: KnowledgeDocument, kb: KnowledgeBase | None = None) -> None:
    if not (document.engine_document_id and document.engine_dataset_id):
        return
    kb = kb or db.get(KnowledgeBase, document.kb_id)
    enqueue(
        db, "knowledge.delete_engine_documents",
        {"engine": kb.engine if kb else "ragflow", "dataset_id": document.engine_dataset_id,
         "document_ids": [document.engine_document_id]},
        tenant_id=document.tenant_id, idempotency_key=f"knowledge.delete_doc:{document.engine_document_id}:{uuid4().hex}",
        max_attempts=JOB_ATTEMPTS,
    )


def remove_document(db: Session, document: KnowledgeDocument) -> None:
    """Soft-delete: the row stays for the audit trail, the file and the engine copy go."""
    _queue_engine_removal(db, document)
    if document.file_key:
        file_store.delete_after_commit(db, document.file_key)
    document.deleted_at = datetime.now(UTC)
    document.engine_document_id = None
    document.status = "failed" if document.status == "processing" else document.status


def reindex_document(db: Session, document: KnowledgeDocument) -> None:
    if document.file_key is None and not document.source_url:
        raise ApiError(409, "knowledge.no_source", "没有可用于重新索引的原文件")
    _queue_engine_removal(db, document)
    document.engine_document_id = None
    document.engine_batch_id = None
    document.status = "processing"
    document.error_message = None
    document.chunks = 0
    document.processing_started_at = datetime.now(UTC)
    _queue_ingest(db, document)


# ---------------------------------------------------------------------------
# Worker jobs.
# ---------------------------------------------------------------------------


def _fail(db: Session, document: KnowledgeDocument, message: str) -> None:
    document.status = "failed"
    document.error_message = message[:500]
    db.commit()


def _queue_poll(db: Session, document: KnowledgeDocument, attempt: int) -> None:
    delay = min(5 * (1.5**attempt), 120)
    enqueue(
        db, "knowledge.poll_document",
        {"document_id": str(document.id), "engine_document_id": document.engine_document_id, "attempt": attempt},
        tenant_id=document.tenant_id, run_after=datetime.now(UTC) + timedelta(seconds=delay),
        idempotency_key=f"knowledge.poll:{document.id}:{document.engine_document_id}:{attempt}",
        max_attempts=JOB_ATTEMPTS,
    )


def _live(db: Session, payload: dict[str, Any]) -> tuple[KnowledgeDocument, KnowledgeBase] | None:
    document = db.get(KnowledgeDocument, UUID(payload["document_id"]))
    if document is None or document.deleted_at is not None or document.status != "processing":
        return None
    kb = db.get(KnowledgeBase, document.kb_id)
    if kb is None or kb.deleted_at is not None:
        return None
    return document, kb


@job_handler("knowledge.upload_document")
def upload_document_job(db: Session, payload: dict[str, Any]) -> None:
    live = _live(db, payload)
    if live is None:
        return
    document, kb = live
    if not engine_configured(kb.engine):
        _fail(db, document, "知识引擎未配置；配置后点击“重新索引”")
        return
    dataset_id = kb.engine_dataset_id
    if not dataset_id:
        raise KnowledgeEngineError("知识库的数据集尚未创建，稍后重试")  # the creation job is still running
    if document.engine_document_id and document.engine_dataset_id == dataset_id:
        _queue_poll(db, document, 0)  # an earlier attempt already uploaded it
        db.commit()
        return
    if not document.file_key or not file_store.exists(document.file_key):
        _fail(db, document, "原文件已丢失，无法索引")
        return
    data = file_store.read(document.file_key)
    with open_engine(kb.engine) as engine:
        ref = engine.add_document(dataset_id, document.name, data, chunking=kb.chunking)
    document.engine_dataset_id = dataset_id
    document.engine_document_id = ref.document_id
    document.engine_batch_id = ref.batch_id
    document.processing_started_at = datetime.now(UTC)
    _queue_poll(db, document, 0)
    db.commit()


@job_handler("knowledge.poll_document")
def poll_document_job(db: Session, payload: dict[str, Any]) -> None:
    live = _live(db, payload)
    if live is None:
        return
    document, kb = live
    if not document.engine_document_id or document.engine_document_id != payload.get("engine_document_id"):
        return  # re-indexed meanwhile: a newer poll owns it
    with open_engine(kb.engine) as engine:
        state = engine.document_state(
            document.engine_dataset_id, DocumentRef(document.engine_document_id, document.engine_batch_id)
        )
    if state.status == "indexed":
        document.status = "indexed"
        document.chunks = state.chunks
        document.error_message = None
        document.indexed_at = datetime.now(UTC)
        db.commit()
    elif state.status == "failed":
        _fail(db, document, state.message or "知识引擎解析失败")
    else:
        _queue_poll(db, document, int(payload.get("attempt", 0)) + 1)
        db.commit()


@job_handler("knowledge.fetch_url")
def fetch_url_job(db: Session, payload: dict[str, Any]) -> None:
    live = _live(db, payload)
    if live is None:
        return
    document, kb = live
    if document.file_key:
        _queue_ingest(db, document)
        db.commit()
        return
    try:
        data, document_format = fetch_url(document.source_url or "")
    except (ApiError, httpx.HTTPError) as exc:
        _fail(db, document, f"网页抓取失败：{getattr(exc, 'detail', None) or exc.__class__.__name__}")
        return
    ensure_storage(db, kb.tenant_id, len(data))
    key = file_store.make_key(kb.tenant_id, kb.id, document.id)
    file_store.save(key, data)
    document.file_key = key
    document.size_bytes = len(data)
    db.flush()
    _queue_ingest(db, document)
    db.commit()


def fetch_url(url: str) -> tuple[bytes, str]:
    """Download a public page. Every hop is re-validated (public https only) and redirects are followed by hand, so a
    redirect cannot lead into the internal network. DNS rebinding between validation and connection remains possible
    (tracked with T-071)."""
    current = url
    with httpx.Client(follow_redirects=False, timeout=20, headers={"User-Agent": "BeeWork-KnowledgeBot/1.0"}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            validate_public_https_url(current)
            with client.stream("GET", current) as response:
                location = response.headers.get("location")
                if response.status_code in (301, 302, 303, 307, 308) and location:
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    raise ApiError(502, "knowledge.fetch_failed", f"网页返回 {response.status_code}")
                content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                data = _read_capped(response)
            break
        else:
            raise ApiError(502, "knowledge.fetch_failed", "重定向次数过多")
    if content_type == "application/pdf":
        sniff_format("page.pdf", data)
        return data, "pdf"
    if content_type not in ("text/html", "application/xhtml+xml", "text/plain", "text/markdown"):
        raise ApiError(502, "knowledge.fetch_failed", f"不支持的网页内容类型：{content_type or '未知'}")
    sniff_format("page.html", data)
    return data, "html"


def _read_capped(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > MAX_URL_BYTES:
            raise ApiError(502, "knowledge.fetch_failed", "网页内容超过 20 MB")
        chunks.append(chunk)
    return b"".join(chunks)


@job_handler("knowledge.delete_engine_documents")
def delete_engine_documents_job(_db: Session, payload: dict[str, Any]) -> None:
    with open_engine(payload.get("engine") or "ragflow") as engine:
        engine.delete_documents(payload["dataset_id"], list(payload["document_ids"]))


@job_handler("knowledge.sweep_documents")
def sweep_documents_job(db: Session, _payload: dict[str, Any]) -> None:
    """Fail documents that have been processing for too long (lost jobs, a stuck engine)."""
    cutoff = datetime.now(UTC) - PROCESSING_TIMEOUT
    for document in db.scalars(
        select(KnowledgeDocument).where(
            KnowledgeDocument.status == "processing",
            KnowledgeDocument.deleted_at.is_(None),
            KnowledgeDocument.processing_started_at < cutoff,
        )
    ):
        document.status = "failed"
        document.error_message = "处理超时，请点击“重新索引”重试"
    db.commit()


# ---------------------------------------------------------------------------
# Hooks into knowledge bases (T-030).
# ---------------------------------------------------------------------------


def _content(db: Session, kb: KnowledgeBase) -> ContentStats:
    rows = dict(
        db.execute(
            select(KnowledgeDocument.status, func.count()).where(
                KnowledgeDocument.kb_id == kb.id, KnowledgeDocument.deleted_at.is_(None)
            ).group_by(KnowledgeDocument.status)
        ).all()
    )
    return ContentStats(
        documents=sum(rows.values()), indexed_documents=rows.get("indexed", 0), processing_documents=rows.get("processing", 0)
    )


def _reindex_into_new_dataset(db: Session, kb: KnowledgeBase, new_ids: dict[str, str | None]) -> None:
    """Rebuild step: point every document at the new dataset and queue it again (the old dataset goes away whole)."""
    if not new_ids.get("documents"):
        return
    for document in db.scalars(_live_documents(kb.id)):
        document.engine_dataset_id = new_ids["documents"]
        document.engine_document_id = None
        document.engine_batch_id = None
        document.status = "processing"
        document.error_message = None
        document.processing_started_at = datetime.now(UTC)
        _queue_ingest(db, document)


def _copy_documents(db: Session, source: KnowledgeBase, copy: KnowledgeBase) -> None:
    owner = None
    for document in db.scalars(_live_documents(source.id)):
        if document.file_key is None and not document.source_url:
            continue
        new_id = uuid4()
        key = None
        if document.file_key and file_store.exists(document.file_key):
            key = file_store.make_key(copy.tenant_id, copy.id, new_id)
            file_store.save(key, file_store.read(document.file_key))
        elif document.file_key:
            continue  # the original is gone; there is nothing to re-index from
        duplicate = KnowledgeDocument(
            id=new_id, tenant_id=copy.tenant_id, kb_id=copy.id, name=document.name, format=document.format,
            source_url=document.source_url, file_key=key, size_bytes=document.size_bytes, owner_id=owner,
            owner_name=document.owner_name, status="processing", processing_started_at=datetime.now(UTC),
        )
        db.add(duplicate)
        db.flush()
        _queue_ingest(db, duplicate)


def _remove_documents_of(db: Session, kb: KnowledgeBase) -> None:
    for document in db.scalars(_live_documents(kb.id)):
        if document.file_key:
            file_store.delete_after_commit(db, document.file_key)  # the engine copies go with the datasets
        document.deleted_at = datetime.now(UTC)


register_content_provider(_content)
REINDEX_HOOKS.append(_reindex_into_new_dataset)
COPY_HOOKS.append(_copy_documents)
DELETE_HOOKS.append(_remove_documents_of)
