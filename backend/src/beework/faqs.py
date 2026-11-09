"""FAQs of a knowledge base (T-032): lifecycle, pushing to the knowledge engine, expiry and hit counting.

BeeWork's table is the source of truth. A FAQ is searchable only while it is ``published`` and the engine holds it:
publishing queues ``knowledge.push_faq`` (one single-chunk document in the knowledge base's FAQ dataset, then polled
until indexed); unpublishing, expiring, editing a published FAQ or deleting queues removal of that document.
"""

import csv
import io
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework.errors import ApiError
from beework.integrations.knowledge_engine import (
    DocumentRef,
    KnowledgeEngineError,
    default_engine,
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
from beework.models.faq import Faq, FaqHit
from beework.models.knowledge import KnowledgeBase
from beework.worker.queue import enqueue, job_handler

logger = logging.getLogger(__name__)

MAX_QUESTION = 200
MAX_ANSWER = 2000
MAX_SIMILAR = 10
MAX_IMPORT_ROWS = 500
JOB_ATTEMPTS = 8
SYNC_TIMEOUT = timedelta(minutes=30)
HIT_WINDOW = timedelta(days=30)
HIT_RETENTION = timedelta(days=90)


# ---------------------------------------------------------------------------
# Validation.
# ---------------------------------------------------------------------------


def clean_fields(question: str, similar_questions: list[str], answer: str) -> tuple[str, list[str], str]:
    question, answer = (question or "").strip(), (answer or "").strip()
    if not question:
        raise ApiError(422, "faq.invalid", "请填写问题")
    if len(question) > MAX_QUESTION:
        raise ApiError(422, "faq.invalid", f"问题不能超过 {MAX_QUESTION} 个字符")
    if not answer:
        raise ApiError(422, "faq.invalid", "请填写答案")
    if len(answer) > MAX_ANSWER:
        raise ApiError(422, "faq.invalid", f"答案不能超过 {MAX_ANSWER} 个字符")
    similar = list(dict.fromkeys(item.strip() for item in similar_questions if item and item.strip()))
    similar = [item for item in similar if item != question]
    if len(similar) > MAX_SIMILAR:
        raise ApiError(422, "faq.invalid", f"相似问法最多 {MAX_SIMILAR} 条")
    if any(len(item) > MAX_QUESTION for item in similar):
        raise ApiError(422, "faq.invalid", f"每条相似问法不能超过 {MAX_QUESTION} 个字符")
    return question, similar, answer


def ensure_publishable(expires_at: date | None, today: date | None = None) -> None:
    if expires_at is not None and expires_at < (today or datetime.now(UTC).date()):
        raise ApiError(422, "faq.expired_date", "失效日期已过，请先修改失效日期")


def live_faqs(kb_id: UUID):
    return select(Faq).where(Faq.kb_id == kb_id, Faq.deleted_at.is_(None))


# ---------------------------------------------------------------------------
# Engine synchronisation (queueing).
# ---------------------------------------------------------------------------


def request_push(db: Session, faq: Faq) -> None:
    """Make the engine hold the current content of a published FAQ."""
    faq.sync_status = "pending"
    faq.sync_error = None
    faq.sync_started_at = datetime.now(UTC)
    enqueue(
        db, "knowledge.push_faq", {"faq_id": str(faq.id), "version": faq.row_version}, tenant_id=faq.tenant_id,
        idempotency_key=f"knowledge.push_faq:{faq.id}:{faq.row_version}:{uuid4().hex}", max_attempts=JOB_ATTEMPTS,
    )


def request_removal(db: Session, faq: Faq) -> None:
    """Remove the FAQ's document from the engine (queued, so an engine outage never blocks the edit)."""
    if faq.engine_document_id and faq.engine_dataset_id:
        kb = db.get(KnowledgeBase, faq.kb_id)
        enqueue(
            db, "knowledge.delete_engine_documents",
            {"engine": kb.engine if kb else default_engine(), "dataset_id": faq.engine_dataset_id,
             "document_ids": [faq.engine_document_id]},
            tenant_id=faq.tenant_id, idempotency_key=f"knowledge.delete_faq:{faq.id}:{faq.engine_document_id}",
            max_attempts=JOB_ATTEMPTS,
        )
    faq.engine_dataset_id = faq.engine_document_id = faq.engine_batch_id = None
    faq.sync_status = "none"
    faq.sync_error = None
    faq.sync_started_at = None


def publish(db: Session, faq: Faq, today: date | None = None) -> None:
    ensure_publishable(faq.expires_at, today)
    faq.status = "published"
    faq.published_at = datetime.now(UTC)
    faq.row_version += 1
    request_push(db, faq)


def unpublish(db: Session, faq: Faq) -> None:
    faq.status = "draft"
    faq.row_version += 1
    request_removal(db, faq)


def refresh_published(db: Session, faq: Faq) -> None:
    """A published FAQ was edited: replace its engine document with the new content."""
    request_removal(db, faq)
    request_push(db, faq)


def expire_due(db: Session, today: date | None = None) -> int:
    """Published FAQs whose last day has passed become ``expired`` and leave the engine. Safe to repeat."""
    day = today or datetime.now(UTC).date()
    expired = 0
    for faq in db.scalars(
        select(Faq).where(
            Faq.status == "published", Faq.deleted_at.is_(None), Faq.expires_at.is_not(None), Faq.expires_at < day
        )
    ):
        faq.status = "expired"
        faq.row_version += 1
        request_removal(db, faq)
        expired += 1
    db.commit()
    return expired


def fail_stuck(db: Session, now: datetime | None = None) -> int:
    """FAQs that have been syncing for too long (lost jobs, a stuck engine) are marked failed so they can be retried."""
    cutoff = (now or datetime.now(UTC)) - SYNC_TIMEOUT
    count = 0
    for faq in db.scalars(
        select(Faq).where(
            Faq.sync_status.in_(("pending", "processing")), Faq.deleted_at.is_(None), Faq.sync_started_at < cutoff
        )
    ):
        faq.sync_status = "failed"
        faq.sync_error = "同步超时，请重新发布或点击重试"
        count += 1
    db.commit()
    return count


def _live(db: Session, payload: dict[str, Any]) -> tuple[Faq, KnowledgeBase] | None:
    faq = db.get(Faq, UUID(payload["faq_id"]))
    if faq is None or faq.deleted_at is not None or faq.status != "published":
        return None
    if payload.get("version") is not None and faq.row_version != payload["version"]:
        return None  # edited meanwhile: a newer push owns it
    kb = db.get(KnowledgeBase, faq.kb_id)
    if kb is None or kb.deleted_at is not None:
        return None
    return faq, kb


def _fail(db: Session, faq: Faq, message: str) -> None:
    faq.sync_status = "failed"
    faq.sync_error = message[:500]
    db.commit()


def _queue_poll(db: Session, faq: Faq, attempt: int) -> None:
    delay = min(5 * (1.5**attempt), 120)
    enqueue(
        db, "knowledge.poll_faq",
        {"faq_id": str(faq.id), "version": faq.row_version, "engine_document_id": faq.engine_document_id, "attempt": attempt},
        tenant_id=faq.tenant_id, run_after=datetime.now(UTC) + timedelta(seconds=delay),
        idempotency_key=f"knowledge.poll_faq:{faq.id}:{faq.engine_document_id}:{attempt}", max_attempts=JOB_ATTEMPTS,
    )


@job_handler("knowledge.push_faq")
def push_faq_job(db: Session, payload: dict[str, Any]) -> None:
    live = _live(db, payload)
    if live is None:
        return
    faq, kb = live
    if not engine_configured(kb.engine):
        faq.sync_status = "not_configured"
        faq.sync_error = "知识引擎未配置；配置后重新发布"
        db.commit()
        return
    dataset_id = kb.engine_faq_dataset_id
    if not dataset_id:
        raise KnowledgeEngineError("知识库的 FAQ 数据集尚未创建，稍后重试")  # the creation job is still running
    if faq.engine_document_id and faq.engine_dataset_id == dataset_id:
        _queue_poll(db, faq, 0)  # an earlier attempt already pushed it
        db.commit()
        return
    with open_engine(kb.engine) as engine:
        ref = engine.add_faq(dataset_id, faq.question, list(faq.similar_questions or []), faq.answer)
    faq.engine_dataset_id = dataset_id
    faq.engine_document_id = ref.document_id
    faq.engine_batch_id = ref.batch_id
    faq.sync_status = "processing"
    _queue_poll(db, faq, 0)
    db.commit()


@job_handler("knowledge.poll_faq")
def poll_faq_job(db: Session, payload: dict[str, Any]) -> None:
    live = _live(db, payload)
    if live is None:
        return
    faq, kb = live
    if not faq.engine_document_id or faq.engine_document_id != payload.get("engine_document_id"):
        return
    with open_engine(kb.engine) as engine:
        state = engine.document_state(faq.engine_dataset_id, DocumentRef(faq.engine_document_id, faq.engine_batch_id))
    if state.status == "indexed":
        faq.sync_status = "synced"
        faq.sync_error = None
        db.commit()
    elif state.status == "failed":
        _fail(db, faq, state.message or "知识引擎索引失败")
    else:
        _queue_poll(db, faq, int(payload.get("attempt", 0)) + 1)
        db.commit()


@job_handler("knowledge.expire_faqs")
def expire_faqs_job(db: Session, _payload: dict[str, Any]) -> None:
    expire_due(db)
    fail_stuck(db)
    purge_old_hits(db)


# ---------------------------------------------------------------------------
# Hits.
# ---------------------------------------------------------------------------


def record_hits(db: Session, tenant_id: UUID, run_id: UUID | None, engine_document_ids: list[str]) -> int:
    """Count a hit for each FAQ that supplied a retrieved passage (matched by its engine document id)."""
    ids = [value for value in dict.fromkeys(engine_document_ids) if value]
    if not ids:
        return 0
    faq_ids = list(
        db.scalars(select(Faq.id).where(Faq.tenant_id == tenant_id, Faq.engine_document_id.in_(ids), Faq.deleted_at.is_(None)))
    )
    for faq_id in faq_ids:
        db.add(FaqHit(tenant_id=tenant_id, faq_id=faq_id, run_id=run_id))
    db.commit()
    return len(faq_ids)


def hits_30d(db: Session, faq_ids: list[UUID], now: datetime | None = None) -> dict[UUID, int]:
    if not faq_ids:
        return {}
    since = (now or datetime.now(UTC)) - HIT_WINDOW
    rows = db.execute(
        select(FaqHit.faq_id, func.count()).where(FaqHit.faq_id.in_(faq_ids), FaqHit.created_at >= since).group_by(FaqHit.faq_id)
    ).all()
    return {faq_id: count for faq_id, count in rows}


def purge_old_hits(db: Session, now: datetime | None = None) -> int:
    from sqlalchemy import delete

    result = db.execute(delete(FaqHit).where(FaqHit.created_at < (now or datetime.now(UTC)) - HIT_RETENTION))
    db.commit()
    return result.rowcount or 0


# ---------------------------------------------------------------------------
# CSV import: ``question,answer,similar questions separated by ;``.
# ---------------------------------------------------------------------------

HEADERS = {("问题", "答案"), ("问题", "答案", "相似问法"), ("question", "answer"), ("question", "answer", "similar_questions")}


def decode_csv(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ApiError(422, "faq.invalid_csv", "文件编码无法识别，请使用 UTF-8 保存")


def parse_csv(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Valid rows and per-row errors. Line numbers are the file's own (the header, if any, is line 1)."""
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    reader = csv.reader(io.StringIO(text))
    seen = 0
    for number, record in enumerate(reader, start=1):
        if not any(cell.strip() for cell in record):
            continue
        seen += 1
        if seen == 1 and tuple(cell.strip().lower() for cell in record[:3] if cell.strip()) in {tuple(h) for h in HEADERS}:
            continue
        if len(record) < 2:
            errors.append({"line": number, "message": "至少需要“问题,答案”两列"})
            continue
        if len(record) > 3:
            errors.append({"line": number, "message": "最多三列：问题,答案,相似问法（用 ; 分隔）；含逗号的内容请用双引号包起来"})
            continue
        similar = [part for part in (record[2] if len(record) == 3 else "").replace("；", ";").split(";")]
        try:
            question, similar_clean, answer = clean_fields(record[0], similar, record[1])
        except ApiError as exc:
            errors.append({"line": number, "message": exc.detail})
            continue
        rows.append({"line": number, "question": question, "similar_questions": similar_clean, "answer": answer})
    if len(rows) + len(errors) > MAX_IMPORT_ROWS:
        raise ApiError(422, "faq.too_many_rows", f"一次最多导入 {MAX_IMPORT_ROWS} 条")
    return rows, errors


# ---------------------------------------------------------------------------
# Hooks into knowledge bases (T-030).
# ---------------------------------------------------------------------------


def _content(db: Session, kb: KnowledgeBase) -> ContentStats:
    rows = db.execute(
        select(Faq.status, Faq.sync_status, func.count())
        .where(Faq.kb_id == kb.id, Faq.deleted_at.is_(None))
        .group_by(Faq.status, Faq.sync_status)
    ).all()
    return ContentStats(
        faqs=sum(count for _, _, count in rows),
        # Only what the engine can actually answer from counts as published (and makes a knowledge base "indexed").
        published_faqs=sum(count for status, sync, count in rows if status == "published" and sync == "synced"),
    )


def _reindex_into_new_dataset(db: Session, kb: KnowledgeBase, new_ids: dict[str, str | None]) -> None:
    """Rebuild step: the published FAQs follow into the new FAQ dataset (the old one is deleted whole)."""
    if not new_ids.get("faq"):
        return
    for faq in db.scalars(live_faqs(kb.id).where(Faq.status == "published")):
        faq.engine_dataset_id = faq.engine_document_id = faq.engine_batch_id = None
        faq.row_version += 1
        request_push(db, faq)


def _copy_faqs(db: Session, source: KnowledgeBase, copy: KnowledgeBase) -> None:
    for faq in db.scalars(live_faqs(source.id)):
        duplicate = Faq(
            tenant_id=copy.tenant_id, kb_id=copy.id, question=faq.question, similar_questions=list(faq.similar_questions or []),
            answer=faq.answer, status=faq.status, expires_at=faq.expires_at, published_at=faq.published_at,
            created_by=faq.created_by,
        )
        db.add(duplicate)
        db.flush()
        if duplicate.status == "published":
            request_push(db, duplicate)  # waits for the copy's datasets to exist, like a copied document does


def _remove_faqs_of(db: Session, kb: KnowledgeBase) -> None:
    now = datetime.now(UTC)
    for faq in db.scalars(live_faqs(kb.id)):
        faq.deleted_at = now  # their engine documents go with the datasets


register_content_provider(_content)
REINDEX_HOOKS.append(_reindex_into_new_dataset)
COPY_HOOKS.append(_copy_faqs)
DELETE_HOOKS.append(_remove_faqs_of)
