"""FAQs of a knowledge base: list, create, edit, copy, publish / unpublish, delete and CSV import (T-032, KNW-031)."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from beework import faqs as faq_service
from beework.api.conventions import (
    ApiError,
    Pagination,
    check_row_version,
    get_pagination,
    paginate,
)
from beework.api.knowledge import _actor_name, _load
from beework.auth import require_csrf
from beework.db import get_db
from beework.models import AuditEvent, Membership
from beework.models.faq import Faq
from beework.models.knowledge import KnowledgeBase
from beework.rbac import require_tenant_permission
from beework.schemas.faq import (
    FaqImportResponse,
    FaqListResponse,
    FaqResponse,
    FaqWriteRequest,
    ImportPreviewRow,
    ImportRowError,
)

router = APIRouter()

MAX_IMPORT_BYTES = 1_000_000
PREVIEW_ROWS = 5


def _response(faq: Faq, hits: int = 0) -> FaqResponse:
    return FaqResponse(
        id=faq.id, kb_id=faq.kb_id, question=faq.question, similar_questions=list(faq.similar_questions or []),
        answer=faq.answer, status=faq.status, expires_at=faq.expires_at, hits_30d=hits, sync_status=faq.sync_status,
        sync_error=faq.sync_error, published_at=faq.published_at, created_by=faq.created_by, row_version=faq.row_version,
        created_at=faq.created_at, updated_at=faq.updated_at,
    )


def _load_faq(db: Session, membership: Membership, kb_id: UUID, faq_id: UUID, *, lock: bool = False) -> tuple[KnowledgeBase, Faq]:
    kb = _load(db, membership, kb_id)
    statement = select(Faq).where(Faq.id == faq_id, Faq.kb_id == kb.id, Faq.tenant_id == membership.tenant_id, Faq.deleted_at.is_(None))
    faq = db.scalar(statement.with_for_update() if lock else statement)
    if faq is None:
        raise ApiError(404, "not_found", "未找到指定 FAQ")
    return kb, faq


def _audit(db: Session, membership: Membership, action: str, faq: Faq | None, **details: str) -> None:
    payload = dict(details)
    if faq is not None:
        payload.update(faq_id=str(faq.id), kb_id=str(faq.kb_id), question=faq.question[:80])
    db.add(AuditEvent(tenant_id=membership.tenant_id, actor_user_id=membership.user_id, action=action, details=payload))


def _require_publish(db: Session, membership: Membership) -> None:
    """Publishing is its own permission (operators may edit drafts but not publish)."""
    from beework.rbac import flatten_permissions, permissions_for_role

    if membership.role == "tenant_admin":
        return
    if membership.custom_role_id is not None:
        from beework.models import TenantRole

        role = db.scalar(select(TenantRole).where(TenantRole.id == membership.custom_role_id, TenantRole.tenant_id == membership.tenant_id, TenantRole.is_active.is_(True)))
        if role and "faq:publish" in flatten_permissions(role.permissions):
            return
    elif "faq:publish" in permissions_for_role(membership.role):
        return
    raise ApiError(403, "auth.forbidden", "当前角色无权发布 FAQ")


@router.get("/knowledge-bases/{kb_id}/faqs", response_model=FaqListResponse)
def list_faqs(
    kb_id: UUID,
    q: str | None = Query(default=None, max_length=100),
    status_filter: str | None = Query(default=None, alias="status", pattern="^(draft|published|expired)$"),
    pagination: Pagination = Depends(get_pagination),
    membership: Membership = Depends(require_tenant_permission("faq:view")),
    db: Session = Depends(get_db),
) -> FaqListResponse:
    kb = _load(db, membership, kb_id)
    base = select(Faq).where(Faq.kb_id == kb.id, Faq.tenant_id == membership.tenant_id, Faq.deleted_at.is_(None))
    counts = {"draft": 0, "published": 0, "expired": 0}
    for state, count in db.execute(
        select(Faq.status, func.count()).where(Faq.kb_id == kb.id, Faq.deleted_at.is_(None)).group_by(Faq.status)
    ):
        counts[state] = count
    counts["all"] = sum(counts.values())
    statement = base
    if q and q.strip():
        pattern = f"%{q.strip().lower()}%"
        statement = statement.where(or_(func.lower(Faq.question).like(pattern), func.lower(Faq.answer).like(pattern)))
    if status_filter:
        statement = statement.where(Faq.status == status_filter)
    items, total = paginate(db, statement.order_by(Faq.updated_at.desc(), Faq.id), pagination)
    hits = faq_service.hits_30d(db, [item.id for item in items])
    return FaqListResponse(
        items=[_response(item, hits.get(item.id, 0)) for item in items], total=total,
        limit=pagination.limit, offset=pagination.offset, counts=counts,
    )


@router.post("/knowledge-bases/{kb_id}/faqs", response_model=FaqResponse, status_code=status.HTTP_201_CREATED)
def create_faq(
    kb_id: UUID,
    payload: FaqWriteRequest,
    membership: Membership = Depends(require_tenant_permission("faq:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> FaqResponse:
    kb = _load(db, membership, kb_id)
    question, similar, answer = faq_service.clean_fields(payload.question, payload.similar_questions, payload.answer)
    if payload.status == "published":
        _require_publish(db, membership)
        faq_service.ensure_publishable(payload.expires_at)
    faq = Faq(
        tenant_id=membership.tenant_id, kb_id=kb.id, question=question, similar_questions=similar, answer=answer,
        expires_at=payload.expires_at, created_by=_actor_name(db, membership), updated_by=_actor_name(db, membership),
    )
    db.add(faq)
    db.flush()
    if payload.status == "published":
        faq_service.publish(db, faq)
    _audit(db, membership, "faq.created", faq, status=faq.status)
    db.commit()
    return _response(faq)


@router.get("/knowledge-bases/{kb_id}/faqs/{faq_id}", response_model=FaqResponse)
def get_faq(
    kb_id: UUID,
    faq_id: UUID,
    membership: Membership = Depends(require_tenant_permission("faq:view")),
    db: Session = Depends(get_db),
) -> FaqResponse:
    _, faq = _load_faq(db, membership, kb_id, faq_id)
    return _response(faq, faq_service.hits_30d(db, [faq.id]).get(faq.id, 0))


@router.put("/knowledge-bases/{kb_id}/faqs/{faq_id}", response_model=FaqResponse)
def update_faq(
    kb_id: UUID,
    faq_id: UUID,
    payload: FaqWriteRequest,
    membership: Membership = Depends(require_tenant_permission("faq:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> FaqResponse:
    """Edit content and expiry. The lifecycle (publish / unpublish) has its own endpoints; ``status`` here only matters
    when it asks to publish a draft or expired FAQ, which needs the faq:publish permission."""
    if payload.row_version is None:
        raise ApiError(422, "validation.invalid", "缺少 row_version")
    _, faq = _load_faq(db, membership, kb_id, faq_id, lock=True)
    check_row_version(faq, payload.row_version, db)
    question, similar, answer = faq_service.clean_fields(payload.question, payload.similar_questions, payload.answer)
    changed = (question, similar, answer) != (faq.question, list(faq.similar_questions or []), faq.answer)
    faq.question, faq.similar_questions, faq.answer, faq.expires_at = question, similar, answer, payload.expires_at
    faq.updated_by = _actor_name(db, membership)
    if payload.status == "published" and faq.status != "published":
        _require_publish(db, membership)
        faq_service.publish(db, faq)  # also revives an expired FAQ once its date was moved (the state machine, 5.1)
    else:
        faq.row_version += 1
        if faq.status == "published":
            faq_service.ensure_publishable(faq.expires_at)
            if changed:
                faq_service.refresh_published(db, faq)
    _audit(db, membership, "faq.updated", faq)
    db.commit()
    return _response(faq)


@router.post("/knowledge-bases/{kb_id}/faqs/{faq_id}/copy", response_model=FaqResponse, status_code=status.HTTP_201_CREATED)
def copy_faq(
    kb_id: UUID,
    faq_id: UUID,
    membership: Membership = Depends(require_tenant_permission("faq:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> FaqResponse:
    kb, source = _load_faq(db, membership, kb_id, faq_id)
    suffix = "（副本）"
    question = source.question[: faq_service.MAX_QUESTION - len(suffix)] + suffix
    duplicate = Faq(
        tenant_id=membership.tenant_id, kb_id=kb.id, question=question, similar_questions=list(source.similar_questions or []),
        answer=source.answer, expires_at=source.expires_at, created_by=_actor_name(db, membership),
        updated_by=_actor_name(db, membership),
    )  # a copy starts as a draft
    db.add(duplicate)
    db.flush()
    _audit(db, membership, "faq.copied", duplicate, source_id=str(source.id))
    db.commit()
    return _response(duplicate)


@router.post("/knowledge-bases/{kb_id}/faqs/{faq_id}/publish", response_model=FaqResponse)
def publish_faq(
    kb_id: UUID,
    faq_id: UUID,
    membership: Membership = Depends(require_tenant_permission("faq:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> FaqResponse:
    _, faq = _load_faq(db, membership, kb_id, faq_id, lock=True)
    if faq.status == "published":
        raise ApiError(409, "faq.invalid_transition", "FAQ 已经是已发布状态")
    faq_service.publish(db, faq)  # a past expiry date is rejected: move it first (5.1)
    _audit(db, membership, "faq.published", faq)
    db.commit()
    return _response(faq)


@router.post("/knowledge-bases/{kb_id}/faqs/{faq_id}/unpublish", response_model=FaqResponse)
def unpublish_faq(
    kb_id: UUID,
    faq_id: UUID,
    membership: Membership = Depends(require_tenant_permission("faq:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> FaqResponse:
    _, faq = _load_faq(db, membership, kb_id, faq_id, lock=True)
    if faq.status != "published":
        raise ApiError(409, "faq.invalid_transition", "只有已发布的 FAQ 可以下线")
    faq_service.unpublish(db, faq)
    _audit(db, membership, "faq.unpublished", faq)
    db.commit()
    return _response(faq)


@router.post("/knowledge-bases/{kb_id}/faqs/{faq_id}/retry", response_model=FaqResponse)
def retry_faq_sync(
    kb_id: UUID,
    faq_id: UUID,
    membership: Membership = Depends(require_tenant_permission("faq:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> FaqResponse:
    _, faq = _load_faq(db, membership, kb_id, faq_id, lock=True)
    if faq.status != "published" or faq.sync_status not in {"failed", "not_configured"}:
        raise ApiError(409, "faq.invalid_transition", "只有同步失败的已发布 FAQ 可以重试")
    faq.engine_dataset_id = faq.engine_document_id = faq.engine_batch_id = None
    faq.row_version += 1
    faq_service.request_push(db, faq)
    db.commit()
    return _response(faq)


@router.delete("/knowledge-bases/{kb_id}/faqs/{faq_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_faq(
    kb_id: UUID,
    faq_id: UUID,
    membership: Membership = Depends(require_tenant_permission("faq:delete")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    _, faq = _load_faq(db, membership, kb_id, faq_id, lock=True)
    faq_service.request_removal(db, faq)
    faq.deleted_at = datetime.now(UTC)
    faq.row_version += 1
    _audit(db, membership, "faq.deleted", faq)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/knowledge-bases/{kb_id}/faqs/import", response_model=FaqImportResponse)
async def import_faqs(
    kb_id: UUID,
    request: Request,
    dry_run: bool = Query(default=False),
    publish: bool = Query(default=False),
    membership: Membership = Depends(require_tenant_permission("faq:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> FaqImportResponse:
    """CSV ``问题,答案,相似问法`` (similar phrasings separated by ``;``). Valid rows are imported, every bad row is
    reported with its line number; ``dry_run`` only reports what would be imported (the preview before importing)."""
    kb = _load(db, membership, kb_id)
    data = await request.body()
    if not data.strip():
        raise ApiError(422, "faq.invalid_csv", "文件是空的")
    if len(data) > MAX_IMPORT_BYTES:
        raise ApiError(413, "faq.file_too_large", "文件超过 1 MB")
    rows, errors = faq_service.parse_csv(faq_service.decode_csv(data))
    if publish and not dry_run:
        _require_publish(db, membership)
    created = 0
    if not dry_run:
        actor = _actor_name(db, membership)
        for row in rows:
            faq = Faq(
                tenant_id=membership.tenant_id, kb_id=kb.id, question=row["question"], similar_questions=row["similar_questions"],
                answer=row["answer"], created_by=actor, updated_by=actor,
            )
            db.add(faq)
            db.flush()
            if publish:
                faq_service.publish(db, faq)
            created += 1
        _audit(db, membership, "faq.imported", None, kb_id=str(kb.id), created=str(created), errors=str(len(errors)))
        db.commit()
    return FaqImportResponse(
        dry_run=dry_run, recognized=len(rows), created=created,
        errors=[ImportRowError(**error) for error in errors], preview=[ImportPreviewRow(**row) for row in rows[:PREVIEW_ROWS]],
    )
