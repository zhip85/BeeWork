from typing import TypedDict

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.models import Membership


class PermissionModule(TypedDict):
    label: str
    actions: list[str]

ROLE_LABELS = {
    "tenant_admin": "租户管理员",
    "builder": "构建者",
    "operator": "运营",
    "viewer": "查看者",
    "auditor": "审计员",
}

PERMISSION_CATALOG: dict[str, PermissionModule] = {
    "dashboard": {"label": "工作台", "actions": ["view"]},
    "assistants": {"label": "助理", "actions": ["view", "edit", "publish", "delete", "test"]},
    "agents": {"label": "智能体", "actions": ["view", "edit", "publish", "delete"]},
    "knowledge": {"label": "知识库与文档", "actions": ["view", "edit", "publish", "delete"]},
    "faq": {"label": "FAQ", "actions": ["view", "edit", "publish", "delete"]},
    "capabilities": {"label": "能力", "actions": ["view", "edit", "publish", "delete"]},
    "orchestration": {"label": "编排 OC", "actions": ["view", "edit", "publish"]},
    "channels": {"label": "渠道", "actions": ["view", "edit", "publish", "delete"]},
    "widgets": {"label": "组件库", "actions": ["view", "edit", "delete"]},
    "conversations": {"label": "实时会话", "actions": ["view", "intervene", "export"]},
    "handoff": {"label": "转人工", "actions": ["view", "edit"]},
    "annotations": {"label": "会话标注", "actions": ["view", "edit", "delete"]},
    "evaluations": {"label": "评测", "actions": ["view", "edit"]},
    "traces": {"label": "调用追踪", "actions": ["view"]},
    "approvals": {"label": "审批中心", "actions": ["view", "approve"]},
    "settings": {"label": "设置", "actions": ["view", "edit", "delete"]},
    "models": {"label": "模型与环境 / BYOK", "actions": ["view", "edit", "delete"]},
    "organizations": {"label": "组织架构", "actions": ["view"]},
    "audit": {"label": "审计日志", "actions": ["view", "export"]},
}

ALL_PERMISSIONS = frozenset(
    f"{module}:{action}"
    for module, definition in PERMISSION_CATALOG.items()
    for action in definition["actions"]
)


# Assistant-scoped roles may only hold (and use) permissions of these modules.
ASSISTANT_SCOPE_MODULES = frozenset({"dashboard", "assistants"})


def _permissions(*items: str) -> frozenset[str]:
    return frozenset(items)


BUILTIN_PERMISSIONS: dict[str, frozenset[str]] = {
    "tenant_admin": ALL_PERMISSIONS,
    "builder": _permissions(
        "dashboard:view",
        "assistants:view", "assistants:edit", "assistants:publish", "assistants:delete", "assistants:test",
        "agents:view", "agents:edit", "agents:publish", "agents:delete",
        "knowledge:view", "knowledge:edit", "knowledge:publish", "knowledge:delete",
        "faq:view", "faq:edit", "faq:publish", "faq:delete",
        "capabilities:view", "capabilities:edit", "capabilities:publish", "capabilities:delete",
        "orchestration:view", "orchestration:edit", "orchestration:publish",
        "channels:view", "channels:edit", "channels:publish", "channels:delete",
        "widgets:view", "widgets:edit", "widgets:delete",
        "conversations:view",
        "handoff:view",
        "annotations:view", "annotations:edit",
        "evaluations:view", "evaluations:edit",
        "traces:view",
        "approvals:view",
    ),
    "operator": _permissions(
        "dashboard:view",
        "assistants:view", "assistants:test",
        "agents:view", "knowledge:view",
        "faq:view", "faq:edit",
        "capabilities:view", "orchestration:view", "channels:view", "widgets:view",
        "conversations:view", "conversations:intervene", "conversations:export",
        "handoff:view", "handoff:edit",
        "annotations:view", "annotations:edit",
        "evaluations:view", "evaluations:edit",
        "traces:view",
        "approvals:view",
    ),
    "viewer": _permissions(
        "dashboard:view", "assistants:view", "agents:view", "knowledge:view", "faq:view",
        "capabilities:view", "orchestration:view", "channels:view", "widgets:view",
        "conversations:view", "handoff:view", "annotations:view", "evaluations:view",
        "traces:view", "approvals:view",
    ),
    "auditor": _permissions(
        "dashboard:view", "assistants:view", "agents:view", "knowledge:view", "faq:view",
        "capabilities:view", "orchestration:view", "channels:view", "widgets:view",
        "conversations:view", "handoff:view", "annotations:view", "evaluations:view",
        "traces:view", "approvals:view",
        "settings:view", "audit:view", "audit:export",
    ),
}


def validate_permissions(permissions: dict[str, list[str]]) -> dict[str, list[str]]:
    normalized: dict[str, list[str]] = {}
    for module, actions in permissions.items():
        definition = PERMISSION_CATALOG.get(module)
        if definition is None:
            raise ValueError(f"未知权限模块：{module}")
        allowed_actions = set(definition["actions"])
        if not isinstance(actions, list) or any(
            not isinstance(action, str) or action not in allowed_actions
            for action in actions
        ):
            raise ValueError(f"{module} 包含不支持的操作权限")
        if actions:
            normalized[module] = sorted(set(actions))
    return normalized


def flatten_permissions(permissions: dict[str, list[str]]) -> frozenset[str]:
    return frozenset(
        f"{module}:{action}"
        for module, actions in permissions.items()
        for action in actions
    )


def permissions_for_role(role: str) -> frozenset[str]:
    return BUILTIN_PERMISSIONS.get(role, frozenset())


def require_tenant_permission(permission: str):
    from beework.auth import get_tenant_membership
    from beework.db import get_db
    from beework.models import TenantRole

    def dependency(
        membership: Membership = Depends(get_tenant_membership),
        db: Session = Depends(get_db),
    ) -> Membership:
        if membership.role == "tenant_admin":
            return membership
        if membership.custom_role_id is not None:
            custom_role = db.scalar(
                select(TenantRole).where(
                    TenantRole.id == membership.custom_role_id,
                    TenantRole.tenant_id == membership.tenant_id,
                    TenantRole.is_active.is_(True),
                )
            )
            if custom_role and permission in flatten_permissions(custom_role.permissions):
                if custom_role.scope == "assistant" and (
                    permission.split(":", 1)[0] not in ASSISTANT_SCOPE_MODULES
                ):
                    raise HTTPException(status_code=403, detail="助理范围角色只能访问其指定的助理")
                if custom_role.scope == "organization":
                    ensure_organization_scope(
                        membership,
                        membership.organization_id,
                        db,
                        role=custom_role,
                    )
                return membership
        elif permission in permissions_for_role(membership.role):
            return membership
        raise HTTPException(status_code=403, detail="当前角色无权执行此操作")

    return dependency


def ensure_organization_scope(
    membership,
    target_organization_id,
    db: Session,
    role=None,
) -> None:
    from beework.models import Organization, TenantRole

    if role is None:
        if membership.role == "tenant_admin" or membership.custom_role_id is None:
            return
        role = db.scalar(
            select(TenantRole).where(
                TenantRole.id == membership.custom_role_id,
                TenantRole.tenant_id == membership.tenant_id,
                TenantRole.is_active.is_(True),
            )
        )
    if role is None:
        raise HTTPException(status_code=403, detail="当前角色已停用")
    if role.scope == "tenant":
        return
    if role.scope != "organization" or role.scope_organization_id is None:
        raise HTTPException(status_code=403, detail="当前角色没有可用的数据范围")

    allowed_organization_id = role.scope_organization_id
    current_id = target_organization_id
    while current_id is not None:
        if current_id == allowed_organization_id:
            return
        current = db.scalar(
            select(Organization).where(
                Organization.id == current_id,
                Organization.tenant_id == membership.tenant_id,
            )
        )
        if current is None:
            break
        current_id = current.parent_id
    raise HTTPException(status_code=403, detail="当前角色无权访问此组织范围")


def organization_is_within_scope(
    db: Session,
    tenant_id,
    target_organization_id,
    scope_organization_id,
) -> bool:
    from beework.models import Organization

    current_id = target_organization_id
    while current_id is not None:
        if current_id == scope_organization_id:
            return True
        current = db.scalar(
            select(Organization).where(
                Organization.id == current_id,
                Organization.tenant_id == tenant_id,
            )
        )
        if current is None:
            return False
        current_id = current.parent_id
    return False


def organization_subtree(db: Session, tenant_id, root_id) -> list:
    """``root_id`` plus all descendant organization ids within the tenant."""
    from beework.models import Organization

    allowed_ids = [root_id]
    frontier = [root_id]
    while frontier:
        children = db.execute(
            select(Organization.id).where(
                Organization.tenant_id == tenant_id,
                Organization.parent_id.in_(frontier),
            )
        ).scalars().all()
        frontier = [org_id for org_id in children if org_id not in allowed_ids]
        allowed_ids.extend(frontier)
    return allowed_ids
