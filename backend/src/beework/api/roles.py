import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework.api.common import (
    lock_tenant,
)
from beework.auth import (
    require_csrf,
    require_tenant_roles,
)
from beework.db import get_db
from beework.models import (
    AuditEvent,
    Membership,
    Organization,
    TenantRole,
)
from beework.rbac import (
    ASSISTANT_SCOPE_MODULES,
    BUILTIN_PERMISSIONS,
    PERMISSION_CATALOG,
    ROLE_LABELS,
    organization_is_within_scope,
    validate_permissions,
)
from beework.schemas import (
    CreateRoleRequest,
    PermissionCatalogResponse,
    RoleListResponse,
    RoleResponse,
    UpdateRoleRequest,
)

router = APIRouter()


@router.get("/roles/catalog", response_model=PermissionCatalogResponse)
def get_permission_catalog(
    _: Membership = Depends(require_tenant_roles("tenant_admin")),
) -> PermissionCatalogResponse:
    return PermissionCatalogResponse(
        modules=[
            {"key": key, "label": module["label"], "actions": module["actions"]}
            for key, module in PERMISSION_CATALOG.items()
        ],
        action_labels={
            "view": "查看",
            "edit": "编辑",
            "publish": "发布",
            "delete": "删除",
            "test": "测试",
            "intervene": "介入",
            "export": "导出",
            "approve": "审批",
        },
    )


@router.get("/roles", response_model=RoleListResponse)
def list_roles(
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    db: Session = Depends(get_db),
) -> RoleListResponse:
    roles: list[RoleResponse] = []
    for role_key, role_name in ROLE_LABELS.items():
        member_count = db.scalar(
            select(func.count())
            .select_from(Membership)
            .where(
                Membership.tenant_id == membership.tenant_id,
                Membership.role == role_key,
                Membership.custom_role_id.is_(None),
            )
        ) or 0
        permissions: dict[str, list[str]] = {}
        for permission in BUILTIN_PERMISSIONS[role_key]:
            module, action = permission.split(":", 1)
            permissions.setdefault(module, []).append(action)
        roles.append(
            RoleResponse(
                key=role_key,
                id=None,
                name=role_name,
                description="系统内置角色",
                scope="tenant",
                scope_organization_id=None,
                permissions=permissions,
                is_active=True,
                is_builtin=True,
                member_count=member_count,
            )
        )

    custom_roles = list(
        db.scalars(
            select(TenantRole)
            .where(TenantRole.tenant_id == membership.tenant_id)
            .order_by(TenantRole.created_at, TenantRole.name)
        )
    )
    for role in custom_roles:
        member_count = db.scalar(
            select(func.count())
            .select_from(Membership)
            .where(
                Membership.tenant_id == membership.tenant_id,
                Membership.custom_role_id == role.id,
            )
        ) or 0
        roles.append(
            RoleResponse(
                key=f"custom:{role.id}",
                id=role.id,
                name=role.name,
                description=role.description,
                scope=role.scope,
                scope_organization_id=role.scope_organization_id,
                scope_assistant_ids=[UUID(item) for item in role.scope_assistant_ids or []],
                permissions=role.permissions,
                is_active=role.is_active,
                is_builtin=False,
                member_count=member_count,
            )
        )
    return RoleListResponse(roles=roles)


@router.post(
    "/roles",
    response_model=RoleResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_role(
    payload: CreateRoleRequest,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> RoleResponse:
    lock_tenant(db, membership.tenant_id)
    _validate_role_scope(
        db,
        membership.tenant_id,
        payload.scope,
        payload.scope_organization_id,
        payload.scope_assistant_ids,
        payload.permissions,
    )
    if db.scalar(
        select(TenantRole.id).where(
            TenantRole.tenant_id == membership.tenant_id,
            TenantRole.name == payload.name,
        )
    ):
        raise HTTPException(status_code=409, detail="角色名称已存在")
    role = TenantRole(
        tenant_id=membership.tenant_id,
        name=payload.name,
        description=payload.description.strip(),
        scope=payload.scope,
        scope_organization_id=payload.scope_organization_id,
        scope_assistant_ids=[str(item) for item in payload.scope_assistant_ids],
        permissions=validate_permissions(payload.permissions),
        is_active=payload.is_active,
        created_by=membership.user_id,
    )
    db.add(role)
    db.flush()
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            action="role.created",
            details={
                "role_id": str(role.id),
                "name": role.name,
                "scope": role.scope,
                "permissions": json.dumps(role.permissions, ensure_ascii=False, sort_keys=True),
            },
        )
    )
    db.commit()
    return RoleResponse(
        key=f"custom:{role.id}",
        id=role.id,
        name=role.name,
        description=role.description,
        scope=role.scope,
        scope_organization_id=role.scope_organization_id,
        scope_assistant_ids=[UUID(item) for item in role.scope_assistant_ids or []],
        permissions=role.permissions,
        is_active=role.is_active,
        is_builtin=False,
        member_count=0,
    )


@router.put("/roles/{role_id}", response_model=RoleResponse)
def update_role(
    role_id: UUID,
    payload: UpdateRoleRequest,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> RoleResponse:
    lock_tenant(db, membership.tenant_id)
    role = db.scalar(
        select(TenantRole).where(
            TenantRole.id == role_id,
            TenantRole.tenant_id == membership.tenant_id,
        )
    )
    if role is None:
        raise HTTPException(status_code=404, detail="未找到自定义角色")
    _validate_role_scope(
        db,
        membership.tenant_id,
        payload.scope,
        payload.scope_organization_id,
        payload.scope_assistant_ids,
        payload.permissions,
    )
    duplicate = db.scalar(
        select(TenantRole.id).where(
            TenantRole.tenant_id == membership.tenant_id,
            TenantRole.name == payload.name,
            TenantRole.id != role.id,
        )
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="角色名称已存在")
    member_ids = list(
        db.scalars(
            select(Membership.organization_id).where(
                Membership.tenant_id == membership.tenant_id,
                Membership.custom_role_id == role.id,
            )
        )
    )
    if payload.scope == "organization" and any(
        not organization_is_within_scope(
            db,
            membership.tenant_id,
            organization_id,
            payload.scope_organization_id,
        )
        for organization_id in member_ids
    ):
        raise HTTPException(status_code=409, detail="角色范围不能排除已分配成员的所属部门")
    if not payload.is_active and member_ids:
        raise HTTPException(status_code=409, detail="请先将成员改派到其他角色，再停用此角色")

    previous_name = role.name
    previous_scope = role.scope
    previous_permissions = role.permissions
    previous_is_active = role.is_active
    role.name = payload.name
    role.description = payload.description.strip()
    role.scope = payload.scope
    role.scope_organization_id = payload.scope_organization_id
    role.scope_assistant_ids = [str(item) for item in payload.scope_assistant_ids]
    role.permissions = validate_permissions(payload.permissions)
    role.is_active = payload.is_active
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            action="role.updated",
            details={
                "role_id": str(role.id),
                "from": previous_name,
                "to": role.name,
                "scope_from": previous_scope,
                "scope_to": role.scope,
                "active_from": str(previous_is_active),
                "active_to": str(role.is_active),
                "permissions_from": json.dumps(
                    previous_permissions, ensure_ascii=False, sort_keys=True
                ),
                "permissions_to": json.dumps(
                    role.permissions, ensure_ascii=False, sort_keys=True
                ),
            },
        )
    )
    db.commit()
    return RoleResponse(
        key=f"custom:{role.id}",
        id=role.id,
        name=role.name,
        description=role.description,
        scope=role.scope,
        scope_organization_id=role.scope_organization_id,
        scope_assistant_ids=[UUID(item) for item in role.scope_assistant_ids or []],
        permissions=role.permissions,
        is_active=role.is_active,
        is_builtin=False,
        member_count=0,
    )


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_role(
    role_id: UUID,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    lock_tenant(db, membership.tenant_id)
    role = db.scalar(
        select(TenantRole).where(
            TenantRole.id == role_id,
            TenantRole.tenant_id == membership.tenant_id,
        )
    )
    if role is None:
        raise HTTPException(status_code=404, detail="未找到自定义角色")
    assigned = db.scalar(
        select(func.count())
        .select_from(Membership)
        .where(
            Membership.tenant_id == membership.tenant_id,
            Membership.custom_role_id == role.id,
        )
    ) or 0
    if assigned:
        raise HTTPException(status_code=409, detail="请先将成员改派到其他角色，再删除此角色")
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            action="role.deleted",
            details={"role_id": str(role.id), "name": role.name},
        )
    )
    db.delete(role)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

def _validate_role_scope(
    db: Session,
    tenant_id: UUID,
    scope: str,
    scope_organization_id: UUID | None,
    scope_assistant_ids: list[UUID] | None = None,
    permissions: dict[str, list[str]] | None = None,
) -> None:
    if scope == "tenant":
        if scope_organization_id is not None:
            raise HTTPException(status_code=422, detail="租户范围角色不能指定组织")
        return
    if scope == "assistant":
        # Imported here: models.assistants pulls in api.conventions, which imports this package.
        from beework.models.assistants import Assistant

        extra_modules = sorted(set(permissions or {}) - ASSISTANT_SCOPE_MODULES)
        if extra_modules:
            raise HTTPException(
                status_code=422,
                detail=f"助理范围角色只能配置工作台与助理权限，不能包含：{'、'.join(extra_modules)}",
            )
        found = set(
            db.scalars(
                select(Assistant.id).where(
                    Assistant.tenant_id == tenant_id,
                    Assistant.id.in_(scope_assistant_ids or []),
                    Assistant.deleted_at.is_(None),
                )
            )
        )
        if found != set(scope_assistant_ids or []):
            raise HTTPException(status_code=404, detail="角色范围中的助理不属于当前企业")
        return
    if scope != "organization" or scope_organization_id is None:
        raise HTTPException(status_code=422, detail="组织范围角色必须指定组织")
    organization = db.scalar(
        select(Organization.id).where(
            Organization.id == scope_organization_id,
            Organization.tenant_id == tenant_id,
        )
    )
    if organization is None:
        raise HTTPException(status_code=404, detail="角色范围组织不属于当前企业")
