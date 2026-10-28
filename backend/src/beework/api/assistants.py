from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from beework.api.common import lock_tenant
from beework.api.conventions import (
    ApiError,
    Page,
    Pagination,
    check_row_version,
    get_pagination,
    normalize_name,
    paginate,
)
from beework.auth import require_csrf
from beework.db import get_db
from beework.entitlements import enforce_quota, get_subscription, register_usage_counter
from beework.models import (
    AuditEvent,
    Membership,
    Organization,
    PlatformModel,
    TenantModelEnvironment,
    TenantModelSelection,
    TenantRole,
    User,
    utc_now,
)
from beework.models.assistants import Assistant, AssistantVisibleOrganization
from beework.rbac import organization_subtree, require_tenant_permission
from beework.schemas.assistants import (
    MOUNT_KINDS,
    AssistantDraftConfig,
    AssistantModelOption,
    AssistantModelOptionsResponse,
    AssistantQuotaResponse,
    AssistantResponse,
    AssistantSummaryResponse,
    AssistantWriteRequest,
)

router = APIRouter()

MODEL_ENVIRONMENT = "production"  # until T-006 adds the environment switcher
UNNAMED = "未命名助理"

# Callbacks that return a reason string when something (e.g. a channel binding, T-040) blocks deletion.
DELETE_BLOCKERS: list[Callable[[Session, Assistant], str | None]] = []


def _count_assistants(db: Session, tenant_id: UUID) -> int:
    return db.scalar(
        select(func.count())
        .select_from(Assistant)
        .where(Assistant.tenant_id == tenant_id, Assistant.deleted_at.is_(None))
    ) or 0


register_usage_counter("assistants", _count_assistants)


# ---------------------------------------------------------------------------
# Authorization: visibility (read) and ownership (write).
# ---------------------------------------------------------------------------


def _scoped_role(db: Session, membership: Membership) -> TenantRole | None:
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
    return role


def _visible_statement(statement, membership: Membership, db: Session):
    """Restrict an assistant query to what the caller may see.

    Organization scope sees assistants of its organization subtree, plus assistants shared with the
    whole tenant (``all``) or with one of its organizations (``specified``). Assistant scope sees only
    the listed assistants.
    """
    role = _scoped_role(db, membership)
    if role is None or role.scope == "tenant":
        return statement
    if role.scope == "assistant":
        return statement.where(Assistant.id.in_([UUID(item) for item in role.scope_assistant_ids or []]))
    if role.scope == "organization" and role.scope_organization_id is not None:
        allowed = organization_subtree(db, membership.tenant_id, role.scope_organization_id)
        shared = exists().where(
            AssistantVisibleOrganization.assistant_id == Assistant.id,
            AssistantVisibleOrganization.organization_id.in_(allowed),
        )
        return statement.where(
            or_(
                Assistant.organization_id.in_(allowed),
                Assistant.visibility == "all",
                and_(Assistant.visibility == "specified", shared),
            )
        )
    return statement.where(False)


def _can_edit(membership: Membership, assistant: Assistant, db: Session) -> bool:
    role = _scoped_role(db, membership)
    if role is None or role.scope == "tenant":
        return True
    if role.scope == "assistant":
        return str(assistant.id) in (role.scope_assistant_ids or [])
    if role.scope == "organization" and role.scope_organization_id is not None:
        return assistant.organization_id in organization_subtree(
            db, membership.tenant_id, role.scope_organization_id
        )
    return False


def _load(db: Session, membership: Membership, assistant_id: UUID, *, lock: bool = False) -> Assistant:
    statement = _visible_statement(
        select(Assistant).where(
            Assistant.id == assistant_id,
            Assistant.tenant_id == membership.tenant_id,
            Assistant.deleted_at.is_(None),
        ),
        membership,
        db,
    )
    if lock:
        statement = statement.with_for_update()
    assistant = db.scalar(statement)
    if assistant is None:
        raise ApiError(404, "not_found", "未找到指定助理")
    return assistant


def _load_for_write(
    db: Session, membership: Membership, assistant_id: UUID, *, lock: bool = True
) -> Assistant:
    assistant = _load(db, membership, assistant_id, lock=lock)
    if not _can_edit(membership, assistant, db):
        raise ApiError(403, "auth.forbidden", "当前角色无权修改此助理")
    return assistant


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _actor_name(db: Session, membership: Membership) -> str:
    user = db.get(User, membership.user_id)
    return user.display_name if user else ""


def _enabled_chat_models(db: Session, tenant_id: UUID) -> tuple[list[tuple[PlatformModel, bool]], str]:
    environment = db.scalar(
        select(TenantModelEnvironment).where(
            TenantModelEnvironment.tenant_id == tenant_id,
            TenantModelEnvironment.environment == MODEL_ENVIRONMENT,
        )
    )
    if environment is None:
        return [], MODEL_ENVIRONMENT
    rows = db.execute(
        select(PlatformModel, TenantModelSelection.is_default)
        .join(TenantModelSelection, TenantModelSelection.model_id == PlatformModel.id)
        .where(
            TenantModelSelection.environment_id == environment.id,
            TenantModelSelection.model_type == "chat",
        )
        .order_by(PlatformModel.provider, PlatformModel.display_name)
    ).all()
    return [(model, bool(is_default)) for model, is_default in rows], MODEL_ENVIRONMENT


def _validate_organization(db: Session, membership: Membership, organization_id: UUID) -> None:
    exists_in_tenant = db.scalar(
        select(Organization.id).where(
            Organization.id == organization_id, Organization.tenant_id == membership.tenant_id
        )
    )
    if exists_in_tenant is None:
        raise ApiError(404, "not_found", "未找到指定组织")
    role = _scoped_role(db, membership)
    if role is None or role.scope == "tenant":
        return
    if role.scope != "organization" or role.scope_organization_id is None:
        raise ApiError(403, "auth.forbidden", "当前角色无权创建助理")
    if organization_id not in organization_subtree(db, membership.tenant_id, role.scope_organization_id):
        raise ApiError(404, "not_found", "未找到指定组织")


def _validate_config(db: Session, membership: Membership, config: AssistantDraftConfig) -> None:
    if config.driven_by is not None:
        raise ApiError(422, "assistant.driver_unavailable", "驱动方式暂未开放")
    for kind in MOUNT_KINDS:
        if getattr(config.mounts, kind):
            raise ApiError(422, "assistant.mount_unavailable", "该类型的能力暂未开放挂载")
    if config.model_id is not None:
        models, _ = _enabled_chat_models(db, membership.tenant_id)
        if config.model_id not in {model.id for model, _ in models}:
            raise ApiError(422, "assistant.model_unavailable", "所选模型未在当前环境启用，请在设置中启用对话模型")


def _validate_visible_orgs(db: Session, membership: Membership, org_ids: list[UUID]) -> None:
    if not org_ids:
        return
    found = set(
        db.scalars(
            select(Organization.id).where(
                Organization.tenant_id == membership.tenant_id, Organization.id.in_(org_ids)
            )
        )
    )
    if found != set(org_ids):
        raise ApiError(404, "not_found", "可见范围中的组织不属于当前企业")


def _ensure_name_free(db: Session, tenant_id: UUID, name: str, exclude_id: UUID | None = None) -> None:
    statement = select(Assistant.id).where(
        Assistant.tenant_id == tenant_id,
        Assistant.deleted_at.is_(None),
        func.lower(Assistant.name) == name.lower(),
    )
    if exclude_id is not None:
        statement = statement.where(Assistant.id != exclude_id)
    if db.scalar(statement.limit(1)) is not None:
        raise ApiError(409, "assistant.name_taken", f"助理名称已存在：{name}")


def _unique_unnamed(db: Session, tenant_id: UUID) -> str:
    candidate, index = UNNAMED, 1
    while True:
        taken = db.scalar(
            select(Assistant.id).where(
                Assistant.tenant_id == tenant_id,
                Assistant.deleted_at.is_(None),
                func.lower(Assistant.name) == candidate.lower(),
            )
        )
        if taken is None:
            return candidate
        index += 1
        candidate = f"{UNNAMED} {index}"


def _resolve_name_and_required(
    db: Session, membership: Membership, payload: AssistantWriteRequest, *, exclude_id: UUID | None, relaxed: bool
) -> str:
    if relaxed and not (payload.name or "").strip():
        return _unique_unnamed(db, membership.tenant_id)
    name = normalize_name(payload.name, 50)
    _ensure_name_free(db, membership.tenant_id, name, exclude_id)
    if not relaxed:
        if not payload.description.strip():
            raise ApiError(422, "validation.invalid", "请填写使用场景 / 简介")
        if not payload.config.prompt.strip():
            raise ApiError(422, "validation.invalid", "请填写助理提示词")
    return name


def _organization_name(db: Session, organization_id: UUID) -> str:
    return db.scalar(select(Organization.name).where(Organization.id == organization_id)) or ""


def _visible_org_ids(db: Session, assistant_id: UUID) -> list[UUID]:
    return list(
        db.scalars(
            select(AssistantVisibleOrganization.organization_id).where(
                AssistantVisibleOrganization.assistant_id == assistant_id
            )
        )
    )


def _set_visible_orgs(db: Session, assistant: Assistant, org_ids: list[UUID]) -> None:
    for link in db.scalars(
        select(AssistantVisibleOrganization).where(AssistantVisibleOrganization.assistant_id == assistant.id)
    ):
        db.delete(link)
    db.flush()
    for org_id in dict.fromkeys(org_ids):
        db.add(
            AssistantVisibleOrganization(
                assistant_id=assistant.id, organization_id=org_id, tenant_id=assistant.tenant_id
            )
        )


def _response(db: Session, membership: Membership, assistant: Assistant) -> AssistantResponse:
    return AssistantResponse(
        id=assistant.id,
        name=assistant.name,
        description=assistant.description,
        organization_id=assistant.organization_id,
        organization_name=_organization_name(db, assistant.organization_id),
        category=assistant.category,
        icon=assistant.icon,
        color=assistant.color,
        status=assistant.status,
        visibility=assistant.visibility,
        visible_org_ids=_visible_org_ids(db, assistant.id),
        config=AssistantDraftConfig.model_validate(assistant.draft_config or {}),
        draft_version=assistant.draft_version,
        live_version_id=assistant.live_version_id,
        owner_id=assistant.owner_id,
        created_by=assistant.created_by,
        updated_by=assistant.updated_by,
        row_version=assistant.row_version,
        created_at=assistant.created_at,
        updated_at=assistant.updated_at,
        can_edit=_can_edit(membership, assistant, db),
    )


def _audit(db: Session, membership: Membership, action: str, assistant: Assistant, **extra: str) -> None:
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            action=action,
            details={"assistant_id": str(assistant.id), "name": assistant.name, **extra},
        )
    )


def _fill_default_model(db: Session, membership: Membership, config: AssistantDraftConfig, relaxed: bool) -> None:
    if config.model_id is not None:
        return
    models, _ = _enabled_chat_models(db, membership.tenant_id)
    default = next((model for model, is_default in models if is_default), None)
    if default is not None:
        config.model_id = default.id
    elif not relaxed:
        raise ApiError(422, "assistant.no_chat_model", "请先在设置中为当前环境启用对话模型")


# ---------------------------------------------------------------------------
# Endpoints.
# ---------------------------------------------------------------------------


@router.get("/assistants/summary", response_model=AssistantSummaryResponse)
def assistants_summary(
    membership: Membership = Depends(require_tenant_permission("assistants:view")),
    db: Session = Depends(get_db),
) -> AssistantSummaryResponse:
    rows = db.execute(
        _visible_statement(
            select(Assistant.status, func.count())
            .where(Assistant.tenant_id == membership.tenant_id, Assistant.deleted_at.is_(None))
            .group_by(Assistant.status),
            membership,
            db,
        )
    ).all()
    counts = {status_: count for status_, count in rows}
    subscription = get_subscription(db, membership.tenant_id)
    db.commit()  # persists a lazily created subscription snapshot
    return AssistantSummaryResponse(
        total=sum(counts.values()),
        published=counts.get("published", 0),
        draft=counts.get("draft", 0),
        disabled=counts.get("disabled", 0),
        monthly_conversations=None,
        quota=AssistantQuotaResponse(
            used=_count_assistants(db, membership.tenant_id),
            limit=subscription.quotas.get("assistants"),
            policy=subscription.overage_policy.get("assistants", "hard"),
        ),
    )


@router.get("/assistants/model-options", response_model=AssistantModelOptionsResponse)
def assistant_model_options(
    membership: Membership = Depends(require_tenant_permission("assistants:view")),
    db: Session = Depends(get_db),
) -> AssistantModelOptionsResponse:
    models, environment = _enabled_chat_models(db, membership.tenant_id)
    default = next((model.id for model, is_default in models if is_default), None)
    return AssistantModelOptionsResponse(
        environment=environment,
        models=[
            AssistantModelOption(
                id=model.id, display_name=model.display_name, provider=model.provider, is_default=is_default
            )
            for model, is_default in models
        ],
        default_model_id=default,
    )


@router.get("/assistants", response_model=Page[AssistantResponse])
def list_assistants(
    q: str | None = Query(default=None, max_length=100),
    status_filter: str | None = Query(default=None, alias="status", pattern="^(draft|published|disabled)$"),
    organization_id: list[UUID] = Query(default_factory=list),
    sort: str = Query(default="updated_desc", pattern="^(updated_desc|created_desc|name_asc)$"),
    pagination: Pagination = Depends(get_pagination),
    membership: Membership = Depends(require_tenant_permission("assistants:view")),
    db: Session = Depends(get_db),
) -> Page[AssistantResponse]:
    statement = _visible_statement(
        select(Assistant).where(Assistant.tenant_id == membership.tenant_id, Assistant.deleted_at.is_(None)),
        membership,
        db,
    )
    if q and q.strip():
        pattern = f"%{q.strip().lower()}%"
        statement = statement.where(
            or_(func.lower(Assistant.name).like(pattern), func.lower(Assistant.description).like(pattern))
        )
    if status_filter:
        statement = statement.where(Assistant.status == status_filter)
    if organization_id:
        statement = statement.where(Assistant.organization_id.in_(organization_id))
    ordering = {
        "updated_desc": (Assistant.updated_at.desc(), Assistant.id),
        "created_desc": (Assistant.created_at.desc(), Assistant.id),
        "name_asc": (func.lower(Assistant.name), Assistant.id),
    }[sort]
    items, total = paginate(db, statement.order_by(*ordering), pagination)
    return Page[AssistantResponse](
        items=[_response(db, membership, item) for item in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.post("/assistants", response_model=AssistantResponse, status_code=status.HTTP_201_CREATED)
def create_assistant(
    payload: AssistantWriteRequest,
    membership: Membership = Depends(require_tenant_permission("assistants:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> AssistantResponse:
    role = _scoped_role(db, membership)
    if role is not None and role.scope == "assistant":
        raise ApiError(403, "auth.forbidden", "助理范围角色不能创建新助理")
    lock_tenant(db, membership.tenant_id)
    organization_id = payload.organization_id or membership.organization_id
    _validate_organization(db, membership, organization_id)
    _validate_visible_orgs(db, membership, payload.visible_org_ids)
    name = _resolve_name_and_required(db, membership, payload, exclude_id=None, relaxed=payload.draft)
    config = payload.config.model_copy(deep=True)
    _fill_default_model(db, membership, config, relaxed=payload.draft)
    _validate_config(db, membership, config)
    enforce_quota(db, membership.tenant_id, "assistants", message="已达套餐助理数上限，请联系平台升级")

    actor = _actor_name(db, membership)
    assistant = Assistant(
        tenant_id=membership.tenant_id,
        organization_id=organization_id,
        name=name,
        description=payload.description.strip(),
        category=payload.category,
        icon=payload.icon,
        color=payload.color,
        status="draft",
        visibility=payload.visibility,
        draft_config=config.model_dump(mode="json"),
        draft_version="0.1",
        owner_id=membership.user_id,
        created_by=actor,
        updated_by=actor,
    )
    db.add(assistant)
    db.flush()
    _set_visible_orgs(db, assistant, payload.visible_org_ids)
    _audit(db, membership, "assistant.draft_saved" if payload.draft else "assistant.created", assistant)
    db.commit()
    return _response(db, membership, assistant)


@router.get("/assistants/{assistant_id}", response_model=AssistantResponse)
def get_assistant(
    assistant_id: UUID,
    membership: Membership = Depends(require_tenant_permission("assistants:view")),
    db: Session = Depends(get_db),
) -> AssistantResponse:
    return _response(db, membership, _load(db, membership, assistant_id))


@router.put("/assistants/{assistant_id}", response_model=AssistantResponse)
def update_assistant(
    assistant_id: UUID,
    payload: AssistantWriteRequest,
    membership: Membership = Depends(require_tenant_permission("assistants:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> AssistantResponse:
    if payload.row_version is None:
        raise ApiError(422, "validation.invalid", "缺少 row_version")
    assistant = _load_for_write(db, membership, assistant_id)
    check_row_version(assistant, payload.row_version, db)
    if payload.organization_id is not None and payload.organization_id != assistant.organization_id:
        if membership.role != "tenant_admin":
            raise ApiError(403, "auth.forbidden", "只有企业管理员可以变更所属组织")
        _validate_organization(db, membership, payload.organization_id)
        assistant.organization_id = payload.organization_id
    _validate_visible_orgs(db, membership, payload.visible_org_ids)
    relaxed = payload.draft and assistant.status == "draft"
    name = _resolve_name_and_required(db, membership, payload, exclude_id=assistant.id, relaxed=relaxed)
    config = payload.config.model_copy(deep=True)
    _fill_default_model(db, membership, config, relaxed=relaxed)
    _validate_config(db, membership, config)

    assistant.name = name
    assistant.description = payload.description.strip()
    assistant.category = payload.category
    assistant.icon = payload.icon
    assistant.color = payload.color
    assistant.visibility = payload.visibility
    assistant.draft_config = config.model_dump(mode="json")
    _set_visible_orgs(db, assistant, payload.visible_org_ids)
    assistant.row_version += 1
    assistant.updated_by = _actor_name(db, membership)
    assistant.updated_at = utc_now()
    _audit(db, membership, "assistant.updated", assistant)
    db.commit()
    return _response(db, membership, assistant)


@router.post("/assistants/{assistant_id}/copy", response_model=AssistantResponse, status_code=status.HTTP_201_CREATED)
def copy_assistant(
    assistant_id: UUID,
    membership: Membership = Depends(require_tenant_permission("assistants:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> AssistantResponse:
    lock_tenant(db, membership.tenant_id)
    source = _load_for_write(db, membership, assistant_id, lock=False)
    role = _scoped_role(db, membership)
    if role is not None and role.scope == "assistant":
        raise ApiError(403, "auth.forbidden", "助理范围角色不能创建新助理")
    enforce_quota(db, membership.tenant_id, "assistants", message="已达套餐助理数上限，请联系平台升级")
    suffix = "（副本）"
    base = source.name[: 50 - len(suffix)] + suffix
    name, index = base, 1
    while db.scalar(
        select(Assistant.id).where(
            Assistant.tenant_id == membership.tenant_id,
            Assistant.deleted_at.is_(None),
            func.lower(Assistant.name) == name.lower(),
        )
    ):
        index += 1
        tail = f" {index}"
        name = source.name[: 50 - len(suffix) - len(tail)] + suffix + tail
    actor = _actor_name(db, membership)
    copy = Assistant(
        tenant_id=membership.tenant_id,
        organization_id=source.organization_id,
        name=name,
        description=source.description,
        category=source.category,
        icon=source.icon,
        color=source.color,
        status="draft",
        visibility=source.visibility,
        draft_config=dict(source.draft_config or {}),  # mounts and OC are copied; channels, history are not
        draft_version="0.1",
        template_id=source.template_id,
        owner_id=membership.user_id,
        created_by=actor,
        updated_by=actor,
    )
    db.add(copy)
    db.flush()
    _set_visible_orgs(db, copy, _visible_org_ids(db, source.id))
    _audit(db, membership, "assistant.copied", copy, source_id=str(source.id))
    db.commit()
    return _response(db, membership, copy)


def _change_status(
    db: Session, membership: Membership, assistant_id: UUID, *, from_status: str, to_status: str, action: str
) -> AssistantResponse:
    assistant = _load_for_write(db, membership, assistant_id)
    if assistant.status != from_status:
        raise ApiError(409, "assistant.invalid_transition", "当前状态不允许此操作")
    assistant.status = to_status
    assistant.row_version += 1
    assistant.updated_by = _actor_name(db, membership)
    assistant.updated_at = utc_now()
    _audit(db, membership, action, assistant)
    db.commit()
    return _response(db, membership, assistant)


@router.post("/assistants/{assistant_id}/disable", response_model=AssistantResponse)
def disable_assistant(
    assistant_id: UUID,
    membership: Membership = Depends(require_tenant_permission("assistants:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> AssistantResponse:
    return _change_status(
        db, membership, assistant_id, from_status="published", to_status="disabled", action="assistant.disabled"
    )


@router.post("/assistants/{assistant_id}/enable", response_model=AssistantResponse)
def enable_assistant(
    assistant_id: UUID,
    membership: Membership = Depends(require_tenant_permission("assistants:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> AssistantResponse:
    return _change_status(
        db, membership, assistant_id, from_status="disabled", to_status="published", action="assistant.enabled"
    )


@router.delete("/assistants/{assistant_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_assistant(
    assistant_id: UUID,
    membership: Membership = Depends(require_tenant_permission("assistants:delete")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    assistant = _load_for_write(db, membership, assistant_id)
    if assistant.status not in {"draft", "disabled"}:
        raise ApiError(409, "assistant.invalid_transition", "仅草稿或已停用的助理可以删除")
    for blocker in DELETE_BLOCKERS:
        reason = blocker(db, assistant)
        if reason:
            raise ApiError(409, "assistant.delete_blocked", reason)
    assistant.deleted_at = utc_now()
    assistant.row_version += 1
    # Assistant-scoped roles must not keep pointing at a deleted assistant.
    for role in db.scalars(
        select(TenantRole).where(TenantRole.tenant_id == membership.tenant_id, TenantRole.scope == "assistant")
    ):
        if str(assistant.id) in (role.scope_assistant_ids or []):
            role.scope_assistant_ids = [item for item in role.scope_assistant_ids if item != str(assistant.id)]
    _audit(db, membership, "assistant.deleted", assistant)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
