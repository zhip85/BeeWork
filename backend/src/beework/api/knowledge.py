"""Knowledge base list, create, settings, copy and delete (T-030, PRD 3.4)."""

from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from beework.api.common import lock_tenant
from beework.api.conventions import (
    ApiError,
    Page,
    Pagination,
    check_row_version,
    get_pagination,
    paginate,
)
from beework.auth import require_csrf
from beework.db import get_db
from beework.knowledge import (
    DEFAULT_THRESHOLD,
    content_stats,
    derive_status,
    reference_count,
    request_dataset_deletion,
    request_sync,
)
from beework.models import AuditEvent, Membership, Organization, TenantRole, User, utc_now
from beework.models.knowledge import KnowledgeBase
from beework.rbac import organization_subtree, require_tenant_permission
from beework.schemas.knowledge import (
    KnowledgeBaseCreate,
    KnowledgeBaseResponse,
    KnowledgeBaseUpdate,
    KnowledgeReferencesResponse,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Authorization: tenant-scope roles see everything, organization-scope roles their organization subtree;
# assistant-scope roles are about assistants, not knowledge, and get nothing here.
# ---------------------------------------------------------------------------


def _scope(db: Session, membership: Membership) -> list[UUID] | None:
    """Organization ids the caller may reach, or ``None`` for the whole tenant."""
    if membership.role == "tenant_admin" or membership.custom_role_id is None:
        return None
    role = db.scalar(
        select(TenantRole).where(
            TenantRole.id == membership.custom_role_id,
            TenantRole.tenant_id == membership.tenant_id,
            TenantRole.is_active.is_(True),
        )
    )
    if role is None:
        raise ApiError(403, "auth.forbidden", "当前角色已停用")
    if role.scope == "tenant":
        return None
    if role.scope == "organization" and role.scope_organization_id is not None:
        return list(organization_subtree(db, membership.tenant_id, role.scope_organization_id))
    return []


def _visible(statement, membership: Membership, db: Session):
    allowed = _scope(db, membership)
    return statement if allowed is None else statement.where(KnowledgeBase.organization_id.in_(allowed))


def _load(db: Session, membership: Membership, kb_id: UUID, *, lock: bool = False) -> KnowledgeBase:
    statement = _visible(
        select(KnowledgeBase).where(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.tenant_id == membership.tenant_id,
            KnowledgeBase.deleted_at.is_(None),
        ),
        membership,
        db,
    )
    if lock:
        statement = statement.with_for_update()
    kb = db.scalar(statement)
    if kb is None:
        raise ApiError(404, "not_found", "未找到指定知识库")
    return kb


def _actor_name(db: Session, membership: Membership) -> str:
    user = db.get(User, membership.user_id)
    return user.display_name if user else ""


def _response(db: Session, membership: Membership, kb: KnowledgeBase) -> KnowledgeBaseResponse:
    stats = content_stats(db, kb)
    organization = db.get(Organization, kb.organization_id)
    return KnowledgeBaseResponse(
        id=kb.id, name=kb.name, description=kb.description, kb_type=kb.kb_type, language=kb.language,
        embedding_model=kb.embedding_model, chunking=kb.chunking, retrieval_threshold=kb.retrieval_threshold,
        organization_id=kb.organization_id, organization_name=organization.name if organization else "",
        owner_id=kb.owner_id, created_by=kb.created_by, updated_by=kb.updated_by,
        status=derive_status(kb, stats), docs_count=stats.documents, faq_count=stats.faqs,
        sync_status=kb.sync_status, sync_error=kb.sync_error, reindexing=kb.reindexing, row_version=kb.row_version,
        created_at=kb.created_at, updated_at=kb.updated_at, can_edit=True,
    )


def _unique_name(db: Session, tenant_id: UUID, name: str, exclude_id: UUID | None = None) -> str:
    name = name.strip()
    if not name:
        raise ApiError(422, "validation.invalid_name", "名称不能为空")
    statement = select(KnowledgeBase.id).where(
        KnowledgeBase.tenant_id == tenant_id,
        KnowledgeBase.deleted_at.is_(None),
        func.lower(KnowledgeBase.name) == name.lower(),
    )
    if exclude_id is not None:
        statement = statement.where(KnowledgeBase.id != exclude_id)
    if db.scalar(statement) is not None:
        raise ApiError(409, "knowledge.name_taken", "已存在同名知识库")
    return name


def _audit(db: Session, membership: Membership, action: str, kb: KnowledgeBase, **extra: str) -> None:
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id, actor_user_id=membership.user_id, action=action,
            details={"knowledge_base_id": str(kb.id), "name": kb.name, **extra},
        )
    )


def _organization_for_create(db: Session, membership: Membership, requested: UUID | None) -> UUID:
    organization_id = requested or membership.organization_id
    if db.scalar(
        select(Organization.id).where(Organization.id == organization_id, Organization.tenant_id == membership.tenant_id)
    ) is None:
        raise ApiError(404, "not_found", "未找到指定组织")
    allowed = _scope(db, membership)
    if allowed is not None and organization_id not in allowed:
        raise ApiError(403, "auth.forbidden", "不能在该组织下创建知识库")
    return organization_id


# ---------------------------------------------------------------------------
# Endpoints.
# ---------------------------------------------------------------------------


@router.get("/knowledge-bases", response_model=Page[KnowledgeBaseResponse])
def list_knowledge_bases(
    q: str | None = Query(default=None, max_length=100),
    kb_type: str | None = Query(default=None, pattern="^(standard|faq)$"),
    status_filter: str | None = Query(default=None, alias="status", pattern="^(draft|processing|indexed)$"),
    pagination: Pagination = Depends(get_pagination),
    membership: Membership = Depends(require_tenant_permission("knowledge:view")),
    db: Session = Depends(get_db),
) -> Page[KnowledgeBaseResponse]:
    statement = _visible(
        select(KnowledgeBase).where(KnowledgeBase.tenant_id == membership.tenant_id, KnowledgeBase.deleted_at.is_(None)),
        membership,
        db,
    )
    if q and q.strip():
        pattern = f"%{q.strip().lower()}%"
        statement = statement.where(
            or_(func.lower(KnowledgeBase.name).like(pattern), func.lower(KnowledgeBase.description).like(pattern))
        )
    if kb_type:
        statement = statement.where(KnowledgeBase.kb_type == kb_type)
    statement = statement.order_by(KnowledgeBase.updated_at.desc(), KnowledgeBase.id)
    if not status_filter:
        items, total = paginate(db, statement, pagination)
        responses = [_response(db, membership, item) for item in items]
    else:
        # The status is derived from content rather than stored, so filter first and page the result.
        matching = [item for item in (_response(db, membership, row) for row in db.scalars(statement)) if item.status == status_filter]
        total = len(matching)
        responses = matching[pagination.offset : pagination.offset + pagination.limit]
    return Page[KnowledgeBaseResponse](items=responses, total=total, limit=pagination.limit, offset=pagination.offset)


@router.post("/knowledge-bases", response_model=KnowledgeBaseResponse, status_code=status.HTTP_201_CREATED)
def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    membership: Membership = Depends(require_tenant_permission("knowledge:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> KnowledgeBaseResponse:
    lock_tenant(db, membership.tenant_id)
    organization_id = _organization_for_create(db, membership, payload.organization_id)
    name = _unique_name(db, membership.tenant_id, payload.name)
    actor = _actor_name(db, membership)
    kb = KnowledgeBase(
        tenant_id=membership.tenant_id, organization_id=organization_id, name=name,
        description=payload.description.strip(), kb_type=payload.kb_type, language=payload.language,
        retrieval_threshold=Decimal(DEFAULT_THRESHOLD), owner_id=membership.user_id, created_by=actor, updated_by=actor,
    )
    db.add(kb)
    db.flush()
    request_sync(db, kb)
    _audit(db, membership, "knowledge.created", kb, kb_type=kb.kb_type)
    db.commit()
    return _response(db, membership, kb)


@router.get("/knowledge-bases/{kb_id}", response_model=KnowledgeBaseResponse)
def get_knowledge_base(
    kb_id: UUID,
    membership: Membership = Depends(require_tenant_permission("knowledge:view")),
    db: Session = Depends(get_db),
) -> KnowledgeBaseResponse:
    return _response(db, membership, _load(db, membership, kb_id))


@router.put("/knowledge-bases/{kb_id}", response_model=KnowledgeBaseResponse)
def update_knowledge_base(
    kb_id: UUID,
    payload: KnowledgeBaseUpdate,
    membership: Membership = Depends(require_tenant_permission("knowledge:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> KnowledgeBaseResponse:
    lock_tenant(db, membership.tenant_id)  # serialises renames, so two edits cannot take the same name
    kb = _load(db, membership, kb_id, lock=True)
    check_row_version(kb, payload.row_version, db)
    stats = content_stats(db, kb)
    name = _unique_name(db, membership.tenant_id, payload.name, exclude_id=kb.id)

    old_documents_dataset = kb.ragflow_dataset_id
    switching_type = payload.kb_type != kb.kb_type
    if switching_type and payload.kb_type == "faq" and stats.documents > 0:
        raise ApiError(
            409, "knowledge.has_documents", "知识库中已有文档，清空文档后才能切换为 FAQ 知识库"
        )
    # Embedding model and chunking only exist on a standard knowledge base; an FAQ one keeps its stored values.
    effective_type = payload.kb_type
    index_changed = effective_type == "standard" and (
        payload.embedding_model != kb.embedding_model or payload.chunking != kb.chunking
    )
    if index_changed and stats.documents > 0 and not payload.confirm_reindex:
        raise ApiError(409, "knowledge.reindex_required", "需要重新索引全部文档，请确认后再保存")

    kb.name = name
    kb.description = payload.description.strip()
    kb.language = payload.language
    kb.retrieval_threshold = payload.retrieval_threshold
    if effective_type == "standard":
        kb.embedding_model = payload.embedding_model
        kb.chunking = payload.chunking
    kb.kb_type = payload.kb_type

    if switching_type and payload.kb_type == "faq":
        kb.ragflow_dataset_id = None  # the (empty) document dataset is no longer needed
        request_dataset_deletion(db, kb.tenant_id, [old_documents_dataset])
    if index_changed and (kb.ragflow_dataset_id or kb.ragflow_faq_dataset_id):
        kb.reindexing = True
        request_sync(db, kb, rebuild=True)
    elif switching_type and payload.kb_type == "standard":
        request_sync(db, kb)  # the document dataset has to be created now
    kb.row_version += 1
    kb.updated_by = _actor_name(db, membership)
    kb.updated_at = utc_now()
    _audit(db, membership, "knowledge.updated", kb, reindex=str(index_changed).lower())
    db.commit()
    return _response(db, membership, kb)


@router.post("/knowledge-bases/{kb_id}/retry-sync", response_model=KnowledgeBaseResponse)
def retry_sync(
    kb_id: UUID,
    membership: Membership = Depends(require_tenant_permission("knowledge:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> KnowledgeBaseResponse:
    kb = _load(db, membership, kb_id, lock=True)
    if kb.sync_status == "synced":
        raise ApiError(409, "knowledge.already_synced", "知识库已与知识引擎同步")
    request_sync(db, kb, rebuild=kb.reindexing)
    db.commit()
    return _response(db, membership, kb)


@router.post("/knowledge-bases/{kb_id}/copy", response_model=KnowledgeBaseResponse, status_code=status.HTTP_201_CREATED)
def copy_knowledge_base(
    kb_id: UUID,
    membership: Membership = Depends(require_tenant_permission("knowledge:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> KnowledgeBaseResponse:
    lock_tenant(db, membership.tenant_id)
    source = _load(db, membership, kb_id)
    suffix = "（副本）"
    base = source.name[: 50 - len(suffix)] + suffix
    name, index = base, 1
    while db.scalar(
        select(KnowledgeBase.id).where(
            KnowledgeBase.tenant_id == membership.tenant_id,
            KnowledgeBase.deleted_at.is_(None),
            func.lower(KnowledgeBase.name) == name.lower(),
        )
    ):
        index += 1
        tail = f" {index}"
        name = source.name[: 50 - len(suffix) - len(tail)] + suffix + tail
    actor = _actor_name(db, membership)
    copy = KnowledgeBase(
        tenant_id=membership.tenant_id, organization_id=source.organization_id, name=name,
        description=source.description, kb_type=source.kb_type, language=source.language,
        embedding_model=source.embedding_model, chunking=source.chunking,
        retrieval_threshold=source.retrieval_threshold, owner_id=membership.user_id, created_by=actor, updated_by=actor,
    )
    db.add(copy)
    db.flush()
    request_sync(db, copy)
    _audit(db, membership, "knowledge.copied", copy, source_id=str(source.id))
    db.commit()
    return _response(db, membership, copy)


@router.get("/knowledge-bases/{kb_id}/references", response_model=KnowledgeReferencesResponse)
def knowledge_references(
    kb_id: UUID,
    membership: Membership = Depends(require_tenant_permission("knowledge:view")),
    db: Session = Depends(get_db),
) -> KnowledgeReferencesResponse:
    """How many assistants / agents mount this knowledge base, for the delete confirmation (KNW-042)."""
    return KnowledgeReferencesResponse(count=reference_count(db, _load(db, membership, kb_id)))


@router.delete("/knowledge-bases/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_knowledge_base(
    kb_id: UUID,
    membership: Membership = Depends(require_tenant_permission("knowledge:delete")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    kb = _load(db, membership, kb_id, lock=True)
    references = reference_count(db, kb)
    kb.deleted_at = utc_now()
    kb.row_version += 1
    request_dataset_deletion(db, kb.tenant_id, [kb.ragflow_dataset_id, kb.ragflow_faq_dataset_id])
    _audit(db, membership, "knowledge.deleted", kb, references=str(references))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

