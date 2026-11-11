"""AI portal — application management for administrators (T-140, 演进 PRD 10.1–10.3)."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from beework import portal_service
from beework.api import assistant_checks
from beework.api import assistants as assistants_api
from beework.api.assistants import _actor_name
from beework.api.conventions import ApiError, check_row_version, normalize_name
from beework.auth import get_tenant_membership, require_csrf
from beework.db import get_db
from beework.models import AuditEvent, Membership, Organization, TenantRole, utc_now
from beework.models.assistants import Assistant
from beework.models.portal import PortalApp, PortalAppOrganization, PortalSettings
from beework.rbac import flatten_permissions, permissions_for_role, require_tenant_permission
from beework.schemas.assistant_checks import DependencyEntry
from beework.schemas.portal import (
    DelistRequest,
    ListRequest,
    PortalAppResponse,
    PortalAppWriteRequest,
    PortalMeConfig,
    PortalSeatRequest,
    PortalSeatState,
    PortalSettingsResponse,
    PortalSettingsUpdate,
    PortalSummary,
    SourceEntry,
    SourcesResponse,
)

router = APIRouter()

NAME_MAX = 50
DESCRIPTION_MAX = 200
CATEGORY_MAX = 24


def _can(db: Session, membership: Membership, permission: str) -> bool:
    if membership.role == "tenant_admin":
        return True
    if membership.custom_role_id is not None:
        role = db.scalar(
            select(TenantRole).where(
                TenantRole.id == membership.custom_role_id, TenantRole.tenant_id == membership.tenant_id,
                TenantRole.is_active.is_(True),
            )
        )
        return role is not None and permission in flatten_permissions(role.permissions)
    return permission in permissions_for_role(membership.role)


def _load(db: Session, membership: Membership, app_id: UUID) -> PortalApp:
    app = db.get(PortalApp, app_id)
    if app is None or app.deleted_at is not None or app.tenant_id != membership.tenant_id:
        raise ApiError(404, "not_found", "未找到指定门户应用")
    return app


def _audit(db: Session, membership: Membership, action: str, app: PortalApp, **extra: str) -> None:
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id, actor_user_id=membership.user_id, action=action,
            details={"app_id": str(app.id), "name": app.name, **extra},
        )
    )


def _response(db: Session, membership: Membership, app: PortalApp) -> PortalAppResponse:
    state, source_name, _modes = portal_service.resolve_source(db, app)
    org_ids = portal_service.app_organization_ids(db, app)
    names = (
        [o.name for o in db.scalars(select(Organization).where(Organization.id.in_(org_ids)).order_by(Organization.name))]
        if org_ids else []
    )
    base = PortalAppResponse.model_validate(app)
    return base.model_copy(
        update={
            "source_name": source_name, "source_state": state, "organization_ids": org_ids, "organization_names": names,
            "can_edit": _can(db, membership, "portal:edit"), "can_publish": _can(db, membership, "portal:publish"),
        }
    )


def _ensure_unique_name(db: Session, tenant_id: UUID, name: str, exclude_id: UUID | None = None) -> None:
    statement = select(PortalApp.id).where(
        PortalApp.tenant_id == tenant_id, PortalApp.deleted_at.is_(None), func.lower(PortalApp.name) == name.lower()
    )
    if exclude_id is not None:
        statement = statement.where(PortalApp.id != exclude_id)
    if db.scalar(statement.limit(1)) is not None:
        raise ApiError(409, "portal.name_taken", "已有同名的门户应用")


def _validate_fields(payload: PortalAppWriteRequest) -> tuple[str, str, str]:
    name = normalize_name(payload.name, NAME_MAX)
    description = payload.description.strip()
    if len(description) > DESCRIPTION_MAX:
        raise ApiError(400, "validation.invalid_description", f"描述不能超过 {DESCRIPTION_MAX} 字")
    category = payload.category.strip()
    if len(category) > CATEGORY_MAX:
        raise ApiError(400, "validation.invalid_category", f"分类不能超过 {CATEGORY_MAX} 字")
    return name, description, category


def _set_organizations(db: Session, app: PortalApp, audience: str, organization_ids: list[UUID]) -> None:
    ids = list(dict.fromkeys(organization_ids)) if audience == "orgs" else []
    if ids:
        existing = set(db.scalars(select(Organization.id).where(Organization.tenant_id == app.tenant_id, Organization.id.in_(ids))))
        if existing != set(ids):
            raise ApiError(404, "not_found", "未找到指定组织")
    db.execute(delete(PortalAppOrganization).where(PortalAppOrganization.app_id == app.id))
    for org_id in ids:
        db.add(PortalAppOrganization(app_id=app.id, organization_id=org_id, tenant_id=app.tenant_id))


def _check_mode(app_source_type: str, source_id: UUID | None, mode: str, db: Session, tenant_id: UUID) -> None:
    if mode != "form":
        return
    resolver = portal_service.SOURCE_RESOLVERS.get(app_source_type)
    info = resolver(db, tenant_id, [source_id]).get(source_id) if resolver and source_id else None
    if info is None or "form" not in info.modes:
        raise ApiError(422, "portal.mode_invalid", "表单模式只能用于工作流类来源")


@router.get("/portal/summary", response_model=PortalSummary)
def portal_summary(
    membership: Membership = Depends(require_tenant_permission("portal:view")),
    db: Session = Depends(get_db),
) -> PortalSummary:
    rows = db.execute(
        select(PortalApp.status, func.count())
        .where(PortalApp.tenant_id == membership.tenant_id, PortalApp.deleted_at.is_(None))
        .group_by(PortalApp.status)
    ).all()
    counts = dict(rows)
    return PortalSummary(listed=counts.get("listed", 0), draft=counts.get("draft", 0), delisted=counts.get("delisted", 0))


@router.get("/portal/app-sources", response_model=SourcesResponse)
def app_sources(
    q: str | None = Query(default=None, max_length=100),
    membership: Membership = Depends(require_tenant_permission("portal:edit")),
    db: Session = Depends(get_db),
) -> SourcesResponse:
    """What can be put in the portal now: published agents and enabled flow connections (演进 PRD 10.2)."""
    used = {
        (source_type, source_id)
        for source_type, source_id in db.execute(
            select(PortalApp.source_type, PortalApp.source_id).where(
                PortalApp.tenant_id == membership.tenant_id, PortalApp.deleted_at.is_(None), PortalApp.source_id.is_not(None)
            )
        )
    }
    items: list[SourceEntry] = []
    for source_type, lister in portal_service.SOURCE_LISTERS.items():
        for source_id, info in lister(db, membership.tenant_id, q):
            items.append(
                SourceEntry(
                    source_type=source_type, source_id=source_id, name=info.name, description=info.description,  # type: ignore[arg-type]
                    modes=info.modes, in_use=(source_type, source_id) in used,  # type: ignore[arg-type]
                )
            )
    return SourcesResponse(items=items)


# ---------------------------------------------------------------------------
# Portal settings (T-141, 演进 PRD 10.4).
# ---------------------------------------------------------------------------


def _settings_response(db: Session, membership: Membership) -> PortalSettingsResponse:
    body, row = portal_service.load_settings(db, membership.tenant_id)
    return PortalSettingsResponse(
        **body.model_dump(), row_version=row.row_version if row else 0, updated_at=row.updated_at if row else None,
        updated_by=row.updated_by if row else None, can_edit=_can(db, membership, "portal:edit"),
    )


@router.get("/portal/settings", response_model=PortalSettingsResponse)
def get_settings(
    membership: Membership = Depends(require_tenant_permission("portal:view")),
    db: Session = Depends(get_db),
) -> PortalSettingsResponse:
    return _settings_response(db, membership)


@router.put("/portal/settings", response_model=PortalSettingsResponse)
def put_settings(
    payload: PortalSettingsUpdate,
    membership: Membership = Depends(require_tenant_permission("portal:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> PortalSettingsResponse:
    """Saves the whole settings object. The audit record names the changed settings with their old and new values —
    short switches and enums only, never free text."""
    row = db.get(PortalSettings, membership.tenant_id)
    before, _row = portal_service.load_settings(db, membership.tenant_id)
    if payload.row_version is None:
        raise ApiError(422, "validation.invalid", "缺少 row_version")
    if payload.row_version != (row.row_version if row else 0):  # 0 = this tenant has never saved settings
        who = f"{row.updated_by} " if row and row.updated_by else "其他人"
        raise ApiError(409, "conflict.stale", f"该对象已被 {who}修改，请刷新后重试")
    values = {name: getattr(payload, name) for name in portal_service.SETTING_FIELDS}
    values["portal_name"], values["input_placeholder"] = values["portal_name"].strip(), values["input_placeholder"].strip()
    actor = _actor_name(db, membership)
    if row is None:
        row = PortalSettings(tenant_id=membership.tenant_id, row_version=1, updated_by=actor, **values)
        db.add(row)
    else:
        for name, value in values.items():
            setattr(row, name, value)
        row.row_version += 1
        row.updated_by = actor
    changed = {
        name: f"{getattr(before, name)}→{value}"
        for name, value in values.items() if getattr(before, name) != value and name not in ("portal_name", "input_placeholder")
    }
    changed.update({name: "changed" for name in ("portal_name", "input_placeholder") if getattr(before, name) != values[name]})
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id, actor_user_id=membership.user_id, action="portal_settings.updated",
            details={"changed": ", ".join(sorted(changed)) or "none", **{k: v for k, v in changed.items() if v != "changed"}},
        )
    )
    db.commit()
    return _settings_response(db, membership)


@router.get("/portal/me/config", response_model=PortalMeConfig)
def my_config(
    membership: Membership = Depends(get_tenant_membership),
    db: Session = Depends(get_db),
) -> PortalMeConfig:
    """What the employee side reads: brand and switches, none of the administrative fields."""
    body, _row = portal_service.load_settings(db, membership.tenant_id)
    data = body.model_dump(exclude={"default_audience", "portal_name"})
    return PortalMeConfig(
        **data, portal_name=portal_service.portal_name(db, membership.tenant_id, body),
        has_console=_can(db, membership, "dashboard:view"),
    )


def _seat_state(db: Session, membership: Membership, state: str, source: str | None, auto: bool = False) -> PortalSeatState:
    body, _row = portal_service.load_settings(db, membership.tenant_id)
    return PortalSeatState(
        state=state, source=source, auto_assigned=auto, can_request=state == "unassigned",  # type: ignore[arg-type]
        portal_name=portal_service.portal_name(db, membership.tenant_id, body),
    )


@router.get("/portal/me/seat", response_model=PortalSeatState)
def my_seat(membership: Membership = Depends(get_tenant_membership), db: Session = Depends(get_db)) -> PortalSeatState:
    """Entering the portal: identity is already verified (the session); this answers “may you use it?”, records the sign-in
    for idle reclaim, and on a first visit hands out a seat when the tenant turned auto-assignment on (T-151)."""
    from beework import portal_seat_service

    state, source, auto = portal_seat_service.open_session(db, membership)
    result = _seat_state(db, membership, state, source, auto)
    db.commit()
    return result


@router.post("/portal/me/seat/request", response_model=PortalSeatState)
def request_my_seat(
    payload: PortalSeatRequest,
    membership: Membership = Depends(get_tenant_membership),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> PortalSeatState:
    from beework import portal_seat_service

    state = portal_seat_service.request_seat(db, membership, payload.reason)
    result = _seat_state(db, membership, state, None)
    db.commit()
    return result


@router.get("/portal/apps", response_model=list[PortalAppResponse])
def list_apps(
    q: str | None = Query(default=None, max_length=100),
    app_status: str | None = Query(default=None, alias="status", pattern="^(draft|listed|delisted)$"),
    membership: Membership = Depends(require_tenant_permission("portal:view")),
    db: Session = Depends(get_db),
) -> list[PortalAppResponse]:
    statement = select(PortalApp).where(PortalApp.tenant_id == membership.tenant_id, PortalApp.deleted_at.is_(None))
    if app_status:
        statement = statement.where(PortalApp.status == app_status)
    if q and q.strip():
        statement = statement.where(func.lower(PortalApp.name).like(f"%{q.strip().lower()}%"))
    apps = db.scalars(statement.order_by(PortalApp.featured.desc(), PortalApp.updated_at.desc(), PortalApp.id)).all()
    return [_response(db, membership, app) for app in apps]


@router.post("/portal/apps", response_model=PortalAppResponse, status_code=status.HTTP_201_CREATED)
def create_app(
    payload: PortalAppWriteRequest,
    membership: Membership = Depends(require_tenant_permission("portal:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> PortalAppResponse:
    if payload.source_type is None or payload.source_id is None:
        raise ApiError(422, "portal.source_required", "请选择来源")
    name, description, category = _validate_fields(payload)
    resolver = portal_service.SOURCE_RESOLVERS.get(payload.source_type)
    info = resolver(db, membership.tenant_id, [payload.source_id]).get(payload.source_id) if resolver else None
    if info is None:
        raise ApiError(404, "not_found", "未找到指定来源")
    if info.state != "available":
        raise ApiError(422, "portal.source_unavailable", "来源还没有发布或已停用，不能添加为门户应用")
    _ensure_unique_name(db, membership.tenant_id, name)
    _check_mode(payload.source_type, payload.source_id, payload.mode, db, membership.tenant_id)
    actor = _actor_name(db, membership)
    app = PortalApp(
        tenant_id=membership.tenant_id, source_type=payload.source_type, source_id=payload.source_id, name=name,
        description=description, category=category, audience=payload.audience, mode=payload.mode, icon=payload.icon,
        featured=payload.featured, status="draft", created_by=actor, updated_by=actor,
    )
    db.add(app)
    db.flush()
    _set_organizations(db, app, payload.audience, payload.organization_ids)
    _audit(db, membership, "portal_app.created", app)
    db.commit()
    return _response(db, membership, app)


@router.get("/portal/apps/{app_id}", response_model=PortalAppResponse)
def get_app(
    app_id: UUID,
    membership: Membership = Depends(require_tenant_permission("portal:view")),
    db: Session = Depends(get_db),
) -> PortalAppResponse:
    return _response(db, membership, _load(db, membership, app_id))


@router.put("/portal/apps/{app_id}", response_model=PortalAppResponse)
def update_app(
    app_id: UUID,
    payload: PortalAppWriteRequest,
    membership: Membership = Depends(require_tenant_permission("portal:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> PortalAppResponse:
    app = _load(db, membership, app_id)
    if payload.row_version is None:
        raise ApiError(422, "validation.invalid", "缺少 row_version")
    check_row_version(app, payload.row_version, db)
    changes_source = (payload.source_type not in (None, app.source_type)) or (payload.source_id not in (None, app.source_id))
    if changes_source:
        # 演进 PRD 10.2: a listed app never silently starts pointing at another service. Delist it first; a draft or
        # delisted app may still be repointed.
        if app.status == "listed":
            raise ApiError(409, "portal.source_locked", "已上架的应用不能更换来源，请先下架")
        resolver = portal_service.SOURCE_RESOLVERS.get(payload.source_type or app.source_type)
        new_id = payload.source_id or app.source_id
        info = resolver(db, membership.tenant_id, [new_id]).get(new_id) if resolver and new_id else None
        if info is None or info.state != "available":
            raise ApiError(422, "portal.source_unavailable", "来源还没有发布或已停用，不能添加为门户应用")
        app.source_type, app.source_id = payload.source_type or app.source_type, new_id
    name, description, category = _validate_fields(payload)
    _ensure_unique_name(db, membership.tenant_id, name, exclude_id=app.id)
    _check_mode(app.source_type, app.source_id, payload.mode, db, membership.tenant_id)
    app.name, app.description, app.category = name, description, category
    app.audience, app.mode, app.icon, app.featured = payload.audience, payload.mode, payload.icon, payload.featured
    _set_organizations(db, app, payload.audience, payload.organization_ids)
    app.row_version += 1
    app.updated_by = _actor_name(db, membership)
    _audit(db, membership, "portal_app.updated", app)
    db.commit()
    return _response(db, membership, app)


@router.post("/portal/apps/{app_id}/list", response_model=PortalAppResponse)
def list_app(
    app_id: UUID,
    payload: ListRequest | None = None,
    membership: Membership = Depends(require_tenant_permission("portal:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> PortalAppResponse:
    app = _load(db, membership, app_id)
    if payload is not None and payload.row_version is not None:
        check_row_version(app, payload.row_version, db)
    if app.status == "listed":
        raise ApiError(409, "portal.already_listed", "该应用已经上架")
    problems = portal_service.listing_problems(db, app)
    if problems:
        raise ApiError(422, "portal.listing_blocked", "；".join(message for _code, message in problems))
    actor = _actor_name(db, membership)
    app.status, app.listed_at, app.listed_by = "listed", utc_now(), actor
    app.delisted_at = app.delisted_reason = None
    app.row_version += 1
    app.updated_by = actor
    _audit(db, membership, "portal_app.listed", app)
    db.commit()
    return _response(db, membership, app)


@router.post("/portal/apps/{app_id}/delist", response_model=PortalAppResponse)
def delist_app(
    app_id: UUID,
    payload: DelistRequest | None = None,
    membership: Membership = Depends(require_tenant_permission("portal:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> PortalAppResponse:
    """Employees stop seeing it and can no longer start calls at once; the configuration is kept (演进 PRD 10.3)."""
    app = _load(db, membership, app_id)
    payload = payload or DelistRequest()
    if payload.row_version is not None:
        check_row_version(app, payload.row_version, db)
    if app.status != "listed":
        raise ApiError(409, "portal.not_listed", "只有已上架的应用可以下架")
    app.status, app.delisted_at, app.delisted_reason = "delisted", utc_now(), payload.reason.strip() or None
    app.row_version += 1
    app.updated_by = _actor_name(db, membership)
    _audit(db, membership, "portal_app.delisted", app, reason=payload.reason.strip()[:100])
    db.commit()
    return _response(db, membership, app)


@router.delete("/portal/apps/{app_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_app(
    app_id: UUID,
    membership: Membership = Depends(require_tenant_permission("portal:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    app = _load(db, membership, app_id)
    if app.status == "listed":
        raise ApiError(409, "portal.listed", "已上架的应用需先下架再删除")
    app.deleted_at = utc_now()
    app.updated_by = _actor_name(db, membership)
    _audit(db, membership, "portal_app.deleted", app)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# What a portal app means for the things it points at.
# ---------------------------------------------------------------------------


def _agent_dependencies(db: Session, assistant: Assistant) -> list[DependencyEntry]:
    """The agent's dependencies tab (AST-070) lists the portal apps built on it."""
    apps = db.scalars(
        select(PortalApp).where(
            PortalApp.tenant_id == assistant.tenant_id, PortalApp.deleted_at.is_(None),
            PortalApp.source_type == "agent", PortalApp.source_id == assistant.id,
        ).order_by(PortalApp.name)
    )
    return [DependencyEntry(kind="portal_app", id=app.id, name=app.name, status=app.status) for app in apps]


assistant_checks.DEPENDENCY_PROVIDERS.append(_agent_dependencies)


# RUL-013 / 演进 PRD 15.2: an agent a listed portal app is built on cannot be deleted.
assistants_api.DELETE_BLOCKERS.append(
    lambda db, assistant: "该智能体被已上架的门户应用使用，请先下架相关应用"
    if db.scalar(
        select(PortalApp.id).where(
            PortalApp.tenant_id == assistant.tenant_id, PortalApp.deleted_at.is_(None), PortalApp.status == "listed",
            PortalApp.source_type == "agent", PortalApp.source_id == assistant.id,
        ).limit(1)
    )
    else None
)
