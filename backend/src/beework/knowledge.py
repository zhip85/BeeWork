"""Knowledge bases (T-030): BeeWork keeps the business record, the knowledge engine (RAGFlow or Dify) holds one
dataset per content kind.

The database is the source of truth. Creating or changing a knowledge base only writes the row and queues a Worker
job, so the engine being unavailable or unconfigured never blocks configuration; ``sync_status`` says whether the
datasets match. Documents (T-031) and FAQs (T-032) plug in through the registries below instead of this module
knowing about their tables.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.integrations.knowledge_engine import (
    KnowledgeEngine,
    KnowledgeEngineError,
    engine_configured,
    open_engine,
)
from beework.models.knowledge import KnowledgeBase
from beework.worker.queue import enqueue, job_handler

logger = logging.getLogger(__name__)

KB_TYPES = ("standard", "faq")
LANGUAGES = ("multilingual", "zh-CN", "en")
EMBEDDING_MODELS = ("bge-m3", "text-embedding-3-large", "cohere-embed-v3")
CHUNKINGS = ("semantic", "fixed_512", "by_heading")
DEFAULT_THRESHOLD = "0.62"
DATASET_JOB_ATTEMPTS = 10

@dataclass(frozen=True)
class ContentStats:
    """What a knowledge base currently holds; T-031 / T-032 report their part."""

    documents: int = 0
    faqs: int = 0
    indexed_documents: int = 0
    processing_documents: int = 0
    published_faqs: int = 0


ContentProvider = Callable[[Session, KnowledgeBase], ContentStats]
_content_providers: list[ContentProvider] = []
# Return how many objects (assistants, agents) mount the knowledge base; T-033 registers the assistant mounts.
ReferenceCounter = Callable[[Session, KnowledgeBase], int]
REFERENCE_COUNTERS: list[ReferenceCounter] = []
# Re-upload and re-parse every document of the knowledge base into the freshly created datasets (T-031).
ReindexHook = Callable[[Session, KnowledgeBase, dict[str, str | None]], None]
REINDEX_HOOKS: list[ReindexHook] = []


# Called inside the API transaction when a knowledge base is copied (documents / FAQs follow the copy) or deleted.
CopyHook = Callable[[Session, KnowledgeBase, KnowledgeBase], None]
DeleteHook = Callable[[Session, KnowledgeBase], None]
COPY_HOOKS: list[CopyHook] = []
DELETE_HOOKS: list[DeleteHook] = []


def register_content_provider(provider: ContentProvider) -> None:
    _content_providers.append(provider)


def content_stats(db: Session, kb: KnowledgeBase) -> ContentStats:
    totals = [provider(db, kb) for provider in _content_providers]
    return ContentStats(
        documents=sum(item.documents for item in totals),
        faqs=sum(item.faqs for item in totals),
        indexed_documents=sum(item.indexed_documents for item in totals),
        processing_documents=sum(item.processing_documents for item in totals),
        published_faqs=sum(item.published_faqs for item in totals),
    )


def reference_count(db: Session, kb: KnowledgeBase) -> int:
    return sum(counter(db, kb) for counter in REFERENCE_COUNTERS)


def derive_status(kb: KnowledgeBase, stats: ContentStats) -> str:
    """PRD 3.4.4: processing while documents are being processed or the index is being rebuilt; indexed once at
    least one document is indexed or one FAQ is published; otherwise draft."""
    if stats.processing_documents > 0 or kb.reindexing:
        return "processing"
    if stats.indexed_documents > 0 or stats.published_faqs > 0:
        return "indexed"
    return "draft"


def dataset_name(kb: KnowledgeBase, *, faq: bool = False, generation: int | None = None) -> str:
    # Built from the id only: a user-typed name could collide, be non-ASCII or leak into the engine. Kept under 40
    # characters (Dify's limit); 24 hex digits of a random UUID are unique in practice, and the engine key is the
    # dataset id anyway, the name only lets a retried creation find what an earlier attempt made.
    return f"bw-{kb.id.hex[:24]}-g{generation or kb.dataset_generation}" + ("-faq" if faq else "")


def request_sync(db: Session, kb: KnowledgeBase, *, rebuild: bool = False) -> None:
    """Queue the dataset work for ``kb`` and mark it ``pending`` (or ``not_configured`` when there is no engine)."""
    if not engine_configured(kb.engine):
        kb.sync_status = "not_configured"
        kb.sync_error = None
        return
    kb.sync_status = "pending"
    kb.sync_error = None
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    enqueue(
        db,
        "knowledge.rebuild_datasets" if rebuild else "knowledge.ensure_datasets",
        {"kb_id": str(kb.id)},
        tenant_id=kb.tenant_id,
        idempotency_key=f"knowledge.sync:{kb.id}:{stamp}",
        max_attempts=DATASET_JOB_ATTEMPTS,
    )


def request_dataset_deletion(db: Session, tenant_id: UUID, engine: str, dataset_ids: list[str | None]) -> None:
    ids = [item for item in dataset_ids if item]
    if not ids:
        return
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    enqueue(
        db, "knowledge.delete_datasets", {"engine": engine, "dataset_ids": ids}, tenant_id=tenant_id,
        idempotency_key=f"knowledge.delete:{ids[0]}:{stamp}", max_attempts=DATASET_JOB_ATTEMPTS,
    )


# ---------------------------------------------------------------------------
# Worker jobs.
# ---------------------------------------------------------------------------


def _create_or_reuse(engine: KnowledgeEngine, kb: KnowledgeBase, *, faq: bool, generation: int) -> str:
    name = dataset_name(kb, faq=faq, generation=generation)
    return engine.find_dataset(name) or engine.create_knowledge_dataset(
        name, faq=faq, chunking=kb.chunking, embedding_model=kb.embedding_model
    )


def _create_datasets(engine: KnowledgeEngine, kb: KnowledgeBase, generation: int) -> dict[str, str | None]:
    """Create (or find, after a half-finished earlier attempt) the datasets ``kb`` needs; returns their ids."""
    return {
        "documents": _create_or_reuse(engine, kb, faq=False, generation=generation) if kb.kb_type == "standard" else None,
        "faq": _create_or_reuse(engine, kb, faq=True, generation=generation),
    }


def ensure_datasets(db: Session, kb: KnowledgeBase) -> None:
    """Bring the datasets in line with the row: create what is missing. Raises for retry when the engine fails."""
    if not engine_configured(kb.engine):
        kb.sync_status = "not_configured"
        kb.sync_error = None
        db.commit()
        return
    try:
        with open_engine(kb.engine) as engine:
            if kb.kb_type == "standard" and not kb.engine_dataset_id:
                kb.engine_dataset_id = _create_or_reuse(engine, kb, faq=False, generation=kb.dataset_generation)
                db.commit()  # keep the id even if the next call fails: a retry then does not recreate it
            if not kb.engine_faq_dataset_id:
                kb.engine_faq_dataset_id = _create_or_reuse(engine, kb, faq=True, generation=kb.dataset_generation)
                db.commit()
    except KnowledgeEngineError as exc:
        kb.sync_status = "failed"
        kb.sync_error = str(exc)[:500]
        db.commit()
        raise
    db.refresh(kb)
    if kb.deleted_at is not None:  # deleted while the datasets were being created: hand them to the deletion job
        request_dataset_deletion(db, kb.tenant_id, kb.engine, [kb.engine_dataset_id, kb.engine_faq_dataset_id])
        db.commit()
        return
    kb.sync_status = "synced"
    kb.sync_error = None
    db.commit()


def rebuild_datasets(db: Session, kb: KnowledgeBase) -> None:
    """Replace the datasets after an embedding model / chunking change (3.4.4): build new ones, re-index the documents
    into them, switch over, and only then delete the old ones, so mounted assistants keep a working index meanwhile."""
    if not engine_configured(kb.engine):
        kb.sync_status = "not_configured"
        db.commit()
        return
    old = {"documents": kb.engine_dataset_id, "faq": kb.engine_faq_dataset_id}
    generation = kb.dataset_generation + 1  # the same on every retry until the switch below bumps it
    try:
        with open_engine(kb.engine) as engine:
            new = _create_datasets(engine, kb, generation)
        for hook in REINDEX_HOOKS:
            hook(db, kb, new)
    except KnowledgeEngineError as exc:
        db.rollback()  # a hook that failed midway must not leave its half-applied changes behind
        kb.sync_status = "failed"
        kb.sync_error = str(exc)[:500]
        db.commit()
        raise
    db.refresh(kb)  # the row may have changed while the engine was busy
    if kb.deleted_at is not None:
        # Deleted meanwhile: its deletion job only knew the old datasets, so the new ones would be orphans.
        request_dataset_deletion(db, kb.tenant_id, kb.engine, [new["documents"], new["faq"], old["documents"], old["faq"]])
        db.commit()
        return
    kb.engine_dataset_id, kb.engine_faq_dataset_id = new["documents"], new["faq"]
    kb.dataset_generation = generation
    kb.reindexing = False
    kb.sync_status = "synced"
    kb.sync_error = None
    request_dataset_deletion(db, kb.tenant_id, kb.engine, [old["documents"], old["faq"]])
    db.commit()


@job_handler("knowledge.ensure_datasets")
def ensure_datasets_job(db: Session, payload: dict[str, Any]) -> None:
    kb = db.get(KnowledgeBase, UUID(payload["kb_id"]))
    if kb is not None and kb.deleted_at is None:
        ensure_datasets(db, kb)


@job_handler("knowledge.rebuild_datasets")
def rebuild_datasets_job(db: Session, payload: dict[str, Any]) -> None:
    kb = db.get(KnowledgeBase, UUID(payload["kb_id"]))
    if kb is None or kb.deleted_at is not None:
        return
    if kb.engine_dataset_id is None and kb.engine_faq_dataset_id is None:
        ensure_datasets(db, kb)  # nothing was created yet, so there is nothing to replace
        kb.reindexing = False
        db.commit()
        return
    rebuild_datasets(db, kb)


@job_handler("knowledge.delete_datasets")
def delete_datasets_job(_db: Session, payload: dict[str, Any]) -> None:
    ids = list(payload.get("dataset_ids") or [])
    if not ids:
        return
    with open_engine(payload.get("engine") or "ragflow") as engine:  # raises when unconfigured: the job retries
        engine.delete_datasets(ids)


@job_handler("knowledge.reconcile")
def reconcile_job(db: Session, _payload: dict[str, Any]) -> None:
    """Hourly: queue a sync for knowledge bases stuck failed / not_configured / pending once their engine is configured."""
    stuck = db.scalars(
        select(KnowledgeBase).where(
            KnowledgeBase.deleted_at.is_(None), KnowledgeBase.sync_status.in_(("failed", "not_configured", "pending"))
        )
    )
    hour = datetime.now(UTC).strftime("%Y%m%d%H")
    for kb in stuck:
        if not engine_configured(kb.engine):
            continue
        enqueue(
            db, "knowledge.rebuild_datasets" if kb.reindexing else "knowledge.ensure_datasets", {"kb_id": str(kb.id)},
            tenant_id=kb.tenant_id, idempotency_key=f"knowledge.reconcile:{kb.id}:{hour}",
            max_attempts=DATASET_JOB_ATTEMPTS,
        )
    db.commit()

