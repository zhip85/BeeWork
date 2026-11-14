"""The tenant component library and the platform's widget templates (T-043, CHN-040–046, PLT-110–114)."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from beework import component_service as service
from beework.api.conventions import ApiError, check_row_version, require_platform_admin
from beework.auth import require_csrf
from beework.db import get_db
from beework.models import AuditEvent, Membership, User, utc_now
from beework.models.components import TenantComponent, WidgetTemplate
from beework.rbac import require_tenant_permission
from beework.schemas.components import (
    ComponentList,
    ComponentResponse,
    ComponentWrite,
    SyncPreview,
    TemplateList,
    TemplateResponse,
    TemplateWrite,
    TypeDefaults,
)

router = APIRouter()


def _audit(db: Session, membership: Membership | None, user: User | None, action: str, **details: str) -> None:
    db.add(AuditEvent(
        tenant_id=membership.tenant_id if membership else None, actor_user_id=membership.user_id if membership else (user.id if user else None),
        action=action, details=details,
    ))


def _response(db: Session, item: TenantComponent, *, used: list[str] | None = None) -> ComponentResponse:
    template = db.get(WidgetTemplate, item.template_id) if item.template_id else None
    latest = template.version if template and template.status != "draft" and template.version != item.template_version else None
    used_by = service.channel_uses(db, item.tenant_id, item.id) if used is None else used
    return ComponentResponse(
        id=item.id, name_zh=item.name_zh, name_en=item.name_en, type=item.type, description=item.description, trigger_tag=item.trigger_tag,
        data_schema=item.schema_text, sample_data=item.sample_data, compatibility=service.full_compatibility(item.compatibility or {}),
        version=item.version, status=item.status, template_id=item.template_id, template_name=template.name_zh if template else None,
        template_version=item.template_version, template_latest=latest, used_by=used_by, row_version=item.row_version,
        can_delete=not used_by, updated_at=item.updated_at,
    )


def _template_response(template: WidgetTemplate, copies: int) -> TemplateResponse:
    return TemplateResponse(
        id=template.id, name_zh=template.name_zh, name_en=template.name_en, type=template.type, description=template.description,
        trigger_tag=template.trigger_tag, data_schema=template.schema_text, sample_data=template.sample_data,
        compatibility=service.full_compatibility(template.compatibility or {}), version=template.version, status=template.status,
        copies=copies, updated_at=template.updated_at,
    )


def _load(db: Session, membership: Membership, component_id: UUID, *, lock: bool = False) -> TenantComponent:
    statement = select(TenantComponent).where(TenantComponent.id == component_id, TenantComponent.tenant_id == membership.tenant_id)
    item = db.scalar(statement.with_for_update() if lock else statement)
    if item is None:
        raise ApiError(404, "not_found", "未找到指定组件")
    return item


def _check_tag_free(db: Session, tenant_id: UUID, tag: str, exclude: UUID | None = None) -> None:
    statement = select(TenantComponent.id).where(TenantComponent.tenant_id == tenant_id, TenantComponent.trigger_tag == tag)
    if exclude is not None:
        statement = statement.where(TenantComponent.id != exclude)
    if db.scalar(statement) is not None:
        raise ApiError(409, "component.tag_taken", f"触发标签 {tag} 已被本租户的其他组件使用")


def _apply(item: TenantComponent | WidgetTemplate, payload) -> bool:
    """Write the editable fields; returns whether the contract (tag, schema, sample, compatibility) changed."""
    tag = service.normalize_tag(payload.trigger_tag)
    compat = service.full_compatibility(payload.compatibility)
    changed = (item.trigger_tag, item.schema_text, item.sample_data, service.full_compatibility(item.compatibility or {})) != (tag, payload.data_schema, payload.sample_data, compat)
    item.name_zh, item.name_en, item.type, item.description = payload.name_zh, payload.name_en, payload.type, payload.description
    item.trigger_tag, item.schema_text, item.sample_data, item.compatibility = tag, payload.data_schema, payload.sample_data, compat
    return changed


# -- tenant library ----------------------------------------------------------------------------------------------


@router.get("/component-defaults", response_model=dict[str, TypeDefaults])
def component_defaults(_: Membership = Depends(require_tenant_permission("widgets:view"))) -> dict[str, TypeDefaults]:
    """What the editor resets to when the type changes (CHN-045)."""
    return {
        kind: TypeDefaults(trigger_tag=base["tag"], data_schema=service.pretty(base["schema"]), sample_data=service.pretty(base["sample"]), compatibility=dict(service.DEFAULT_COMPATIBILITY))
        for kind, base in service.TYPE_DEFAULTS.items()
    }


@router.get("/components", response_model=ComponentList)
def list_components(
    q: str | None = Query(default=None, max_length=80),
    channel_type: str | None = Query(default=None, max_length=16, description="Only components that can be shown on this channel type"),
    selectable: bool = Query(default=False, description="Hide disabled components"),
    membership: Membership = Depends(require_tenant_permission("widgets:view")),
    db: Session = Depends(get_db),
) -> ComponentList:
    statement = select(TenantComponent).where(TenantComponent.tenant_id == membership.tenant_id)
    if q and q.strip():
        statement = statement.where(func.lower(TenantComponent.name_zh).contains(q.strip().lower()))
    if selectable:
        statement = statement.where(TenantComponent.status != "disabled")
    items = list(db.scalars(statement.order_by(TenantComponent.created_at, TenantComponent.id)))
    if channel_type:
        items = [i for i in items if service.full_compatibility(i.compatibility or {}).get(channel_type) not in (None, "off")]
    return ComponentList(items=[_response(db, item) for item in items])


@router.post("/components", response_model=ComponentResponse, status_code=status.HTTP_201_CREATED)
def create_component(
    payload: ComponentWrite,
    membership: Membership = Depends(require_tenant_permission("widgets:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ComponentResponse:
    service.check_content(payload.data_schema, payload.sample_data, payload.compatibility)
    tag = service.normalize_tag(payload.trigger_tag)
    if not tag:
        raise ApiError(422, "component.tag_invalid", "触发标签不能为空")
    _check_tag_free(db, membership.tenant_id, tag)
    item = TenantComponent(tenant_id=membership.tenant_id, status=payload.status, created_by=membership.user_id, version="0.1", name_zh=payload.name_zh, trigger_tag=tag, type=payload.type, schema_text="{}")
    _apply(item, payload)
    db.add(item)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError(409, "component.tag_taken", f"触发标签 {tag} 已被本租户的其他组件使用") from exc
    _audit(db, membership, None, "component.created", component=item.name_zh, tag=tag)
    db.commit()
    return _response(db, item)


@router.get("/components/{component_id}", response_model=ComponentResponse)
def get_component(component_id: UUID, membership: Membership = Depends(require_tenant_permission("widgets:view")), db: Session = Depends(get_db)) -> ComponentResponse:
    return _response(db, _load(db, membership, component_id))


@router.put("/components/{component_id}", response_model=ComponentResponse)
def update_component(
    component_id: UUID,
    payload: ComponentWrite,
    membership: Membership = Depends(require_tenant_permission("widgets:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ComponentResponse:
    """Saving bumps the minor version when the contract changed. Channels keep what they published until they publish again (CHN-025)."""
    item = _load(db, membership, component_id, lock=True)
    if payload.row_version is not None:
        check_row_version(item, payload.row_version, db)
    service.check_content(payload.data_schema, payload.sample_data, payload.compatibility)
    tag = service.normalize_tag(payload.trigger_tag)
    if not tag:
        raise ApiError(422, "component.tag_invalid", "触发标签不能为空")
    _check_tag_free(db, membership.tenant_id, tag, exclude=item.id)
    if _apply(item, payload):
        item.version = service.bump_minor(item.version)
    item.status = payload.status
    item.row_version += 1
    item.updated_at = utc_now()
    _audit(db, membership, None, "component.updated", component=item.name_zh, version=item.version)
    db.commit()
    return _response(db, item)


@router.delete("/components/{component_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_component(
    component_id: UUID,
    membership: Membership = Depends(require_tenant_permission("widgets:delete")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    item = _load(db, membership, component_id, lock=True)
    used = service.channel_uses(db, membership.tenant_id, item.id)
    if used:
        raise ApiError(409, "component.in_use", f"组件已被已发布的渠道使用（{'、'.join(used)}），请先在这些渠道重新发布并取消选择，或改为停用")
    _audit(db, membership, None, "component.deleted", component=item.name_zh)
    db.delete(item)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/widget-templates", response_model=TemplateList)
def list_templates(membership: Membership = Depends(require_tenant_permission("widgets:view")), db: Session = Depends(get_db)) -> TemplateList:
    """The SaaS library as a tenant sees it: published templates only (PLT-113)."""
    service.ensure_default_templates(db)
    db.commit()
    templates = list(db.scalars(select(WidgetTemplate).where(WidgetTemplate.status == "published").order_by(WidgetTemplate.created_at, WidgetTemplate.trigger_tag)))
    counts = dict(db.execute(
        select(TenantComponent.template_id, func.count()).where(TenantComponent.tenant_id == membership.tenant_id, TenantComponent.template_id.is_not(None)).group_by(TenantComponent.template_id)
    ).all())
    return TemplateList(items=[_template_response(t, counts.get(t.id, 0)) for t in templates])


@router.post("/components/from-template/{template_id}", response_model=ComponentResponse, status_code=status.HTTP_201_CREATED)
def copy_from_template(
    template_id: UUID,
    membership: Membership = Depends(require_tenant_permission("widgets:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ComponentResponse:
    template = db.get(WidgetTemplate, template_id)
    if template is None or template.status == "draft":
        raise ApiError(404, "not_found", "未找到指定模板")
    item = service.copy_template(db, membership.tenant_id, template, membership.user_id)
    _audit(db, membership, None, "component.copied", component=item.name_zh, template=template.name_zh, version=template.version)
    db.commit()
    return _response(db, item)


def _synced_template(db: Session, item: TenantComponent) -> WidgetTemplate:
    template = db.get(WidgetTemplate, item.template_id) if item.template_id else None
    if template is None or template.status == "draft":
        raise ApiError(409, "component.no_template", "该组件不是模板的副本，或模板已不可用")
    return template


@router.get("/components/{component_id}/sync-preview", response_model=SyncPreview)
def sync_preview(component_id: UUID, membership: Membership = Depends(require_tenant_permission("widgets:view")), db: Session = Depends(get_db)) -> SyncPreview:
    item = _load(db, membership, component_id)
    template = _synced_template(db, item)
    return SyncPreview(from_version=item.version, to_version=template.version, changes=service.sync_diff(item, template))


@router.post("/components/{component_id}/sync", response_model=ComponentResponse)
def sync(
    component_id: UUID,
    membership: Membership = Depends(require_tenant_permission("widgets:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ComponentResponse:
    item = _load(db, membership, component_id, lock=True)
    template = _synced_template(db, item)
    service.sync_component(db, item, template)
    item.updated_at = utc_now()
    _audit(db, membership, None, "component.synced", component=item.name_zh, version=template.version)
    db.commit()
    return _response(db, item)


# -- platform templates ------------------------------------------------------------------------------------------


def _copies(db: Session, template_id: UUID) -> int:
    return db.scalar(select(func.count()).select_from(TenantComponent).where(TenantComponent.template_id == template_id)) or 0


@router.get("/platform/widget-templates", response_model=TemplateList)
def platform_templates(_: User = Depends(require_platform_admin), db: Session = Depends(get_db)) -> TemplateList:
    service.ensure_default_templates(db)
    db.commit()
    templates = list(db.scalars(select(WidgetTemplate).order_by(WidgetTemplate.created_at, WidgetTemplate.trigger_tag)))
    return TemplateList(items=[_template_response(t, _copies(db, t.id)) for t in templates])


def _template_write(db: Session, template: WidgetTemplate, payload: TemplateWrite, user: User, *, creating: bool) -> None:
    service.check_content(payload.data_schema, payload.sample_data, payload.compatibility)
    tag = service.normalize_tag(payload.trigger_tag)
    if not tag:
        raise ApiError(422, "component.tag_invalid", "触发标签不能为空")
    other = db.scalar(select(WidgetTemplate.id).where(WidgetTemplate.trigger_tag == tag, WidgetTemplate.id != template.id))
    if other is not None:
        raise ApiError(409, "component.tag_taken", f"触发标签 {tag} 已被其他模板使用")
    if not creating:
        before = (template.trigger_tag, template.schema_text, service.full_compatibility(template.compatibility or {}))
        after = (tag, payload.data_schema, service.full_compatibility(payload.compatibility))
        if before != after and service.version_key(payload.version) <= service.version_key(template.version):
            raise ApiError(422, "component.version_required", f"数据架构、触发标签或兼容矩阵变化时必须提高版本号（当前 {template.version}）")
    _apply(template, payload)
    template.version, template.status, template.updated_by = payload.version, payload.status, user.id
    template.updated_at = utc_now()


@router.post("/platform/widget-templates", response_model=TemplateResponse, status_code=status.HTTP_201_CREATED)
def create_template(payload: TemplateWrite, user: User = Depends(require_platform_admin), _: None = Depends(require_csrf), db: Session = Depends(get_db)) -> TemplateResponse:
    template = WidgetTemplate(name_zh=payload.name_zh, type=payload.type, trigger_tag="", schema_text="{}")
    _template_write(db, template, payload, user, creating=True)
    db.add(template)
    db.flush()
    _audit(db, None, user, "widget_template.created", template=template.name_zh, tag=template.trigger_tag)
    db.commit()
    return _template_response(template, 0)


@router.put("/platform/widget-templates/{template_id}", response_model=TemplateResponse)
def update_template(template_id: UUID, payload: TemplateWrite, user: User = Depends(require_platform_admin), _: None = Depends(require_csrf), db: Session = Depends(get_db)) -> TemplateResponse:
    template = db.get(WidgetTemplate, template_id)
    if template is None:
        raise ApiError(404, "not_found", "未找到指定模板")
    _template_write(db, template, payload, user, creating=False)
    _audit(db, None, user, "widget_template.updated", template=template.name_zh, version=template.version)
    db.commit()
    return _template_response(template, _copies(db, template.id))


@router.delete("/platform/widget-templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template(template_id: UUID, user: User = Depends(require_platform_admin), _: None = Depends(require_csrf), db: Session = Depends(get_db)) -> Response:
    template = db.get(WidgetTemplate, template_id)
    if template is None:
        raise ApiError(404, "not_found", "未找到指定模板")
    if _copies(db, template.id):
        raise ApiError(409, "component.template_has_copies", "已有租户复制了该模板，只能弃用，不能删除")
    _audit(db, None, user, "widget_template.deleted", template=template.name_zh)
    db.delete(template)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

