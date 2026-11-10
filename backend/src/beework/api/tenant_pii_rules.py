"""Tenant PII rule library (T-052, SET-050–054): the tenant layer of the platform → tenant → assistant inheritance."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from beework.api.conventions import ApiError, check_row_version
from beework.approvals import ensure_policies, pending_request, submit_change
from beework.auth import require_csrf
from beework.db import get_db
from beework.governance import tenant as service
from beework.governance.inheritance import EffectiveRule
from beework.models import Membership, Tenant
from beework.rbac import require_tenant_permission
from beework.schemas.tenant_pii import (
    ChainSummary,
    EffectiveRuleResponse,
    MutationResponse,
    TenantPiiState,
    TenantRuleWrite,
    TenantSettingsWrite,
)
from beework.schemas.versions import ApprovalInfo

router = APIRouter()


def _item(db: Session, tenant_id: UUID, effective: EffectiveRule) -> EffectiveRuleResponse:
    key = UUID(effective.key)
    pending = pending_request(db, tenant_id, service.OBJECT_TYPE, service.object_id_for(tenant_id, key))
    return EffectiveRuleResponse(
        key=key, name=effective.rule.name, match_type=effective.rule.match_type, pattern=effective.rule.pattern,
        stage=effective.rule.stage, action=effective.rule.action, block_message=effective.rule.block_message,
        enabled=effective.enabled, enforced=effective.enforced, label=effective.label,
        restorable=effective.override_id is not None, deletable=effective.label == "tenant",
        pending_approval_id=pending.id if pending else None,
    )


def _state(db: Session, membership: Membership, scope: str) -> TenantPiiState:
    tenant = db.get(Tenant, membership.tenant_id)
    settings_row = service.get_settings(db, membership.tenant_id)
    effective = service.tenant_effective(db, membership.tenant_id)
    if scope == "tenant":
        effective = [item for item in effective if item.label in ("tenant", "tenant_modified")]
    policy = ensure_policies(db, membership.tenant_id)[service.EVENT]
    return TenantPiiState(
        tenant_name=tenant.name if tenant else "",
        chain=ChainSummary(**service.summary(db, membership.tenant_id)),
        inherit_platform=service.inherits_platform(db, membership.tenant_id),
        settings_version=settings_row.row_version if settings_row else 0,
        items=[_item(db, membership.tenant_id, item) for item in effective],
        approval=ApprovalInfo(required=policy.enabled, levels=policy.levels, approver_role=policy.approver_role, pending_approval_id=None),
    )


def _submit(
    db: Session, membership: Membership, response: Response, *, operation: str, key: UUID | None,
    fields: dict[str, Any] | None, title: str, kind: str | None = None,
) -> MutationResponse:
    """Apply now when the “智能体规则变更” policy is off (SET-053), otherwise open an approval request."""
    # Validate before queueing so an approver is never asked to approve something that cannot be applied.
    service.precheck(db, membership.tenant_id, operation, key, fields)
    diff = service.diff_for(db, membership.tenant_id, operation, key, fields)
    result = submit_change(
        db, tenant_id=membership.tenant_id, event=service.EVENT, action=service.ACTION, object_type=service.OBJECT_TYPE,
        object_id=service.object_id_for(membership.tenant_id, key, operation), title=title[:200],
        payload={"operation": operation, "key": str(key) if key else None, "fields": fields}, change_diff=diff,
        checks=[{"label": "只影响本租户的 PII 规则", "level": "ok"}], requester_id=membership.user_id,
    )
    db.commit()
    kind = kind or {"create": "created", "update": "updated", "restore": "restored", "delete": "deleted", "settings": "settings"}[operation]
    if result.approval is not None:
        response.status_code = status.HTTP_202_ACCEPTED
        return MutationResponse(outcome="pending", kind=kind, approval_id=result.approval.id)
    return MutationResponse(outcome="applied", kind=kind)


@router.get("/tenant/pii-rules", response_model=TenantPiiState)
def get_tenant_pii_rules(
    scope: str = Query(default="all", pattern="^(all|tenant)$"),
    membership: Membership = Depends(require_tenant_permission("settings:view")),
    db: Session = Depends(get_db),
) -> TenantPiiState:
    state = _state(db, membership, scope)
    db.commit()  # persists lazily created default approval policies
    return state


@router.put("/tenant/pii-rules/settings", response_model=MutationResponse)
def update_inheritance(
    payload: TenantSettingsWrite,
    response: Response,
    membership: Membership = Depends(require_tenant_permission("settings:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> MutationResponse:
    row = service.get_settings(db, membership.tenant_id)
    check_row_version(row if row is not None else _Absent(), payload.row_version, db)
    return _submit(db, membership, response, operation="settings", key=None, fields={"inherit_platform": payload.inherit_platform}, title="租户 PII 规则 · 继承平台规则开关")


class _Absent:
    """Stand-in for a tenant that never saved the switch: version 0 (check_row_version only reads ``row_version``)."""

    row_version = 0


@router.post("/tenant/pii-rules", response_model=MutationResponse, status_code=status.HTTP_201_CREATED)
def create_tenant_rule(
    payload: TenantRuleWrite,
    response: Response,
    membership: Membership = Depends(require_tenant_permission("settings:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> MutationResponse:
    return _submit(db, membership, response, operation="create", key=None, fields=payload.model_dump(), title=f"租户规则变更 · 新增 {payload.name.strip()}")


@router.put("/tenant/pii-rules/{key}", response_model=MutationResponse)
def update_tenant_rule(
    key: UUID,
    payload: TenantRuleWrite,
    response: Response,
    membership: Membership = Depends(require_tenant_permission("settings:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> MutationResponse:
    """Edit a rule of the tenant, or an inherited one: that creates (or updates) the tenant's override of it."""
    platform, own = service._own_or_platform(db, membership.tenant_id, key)
    if platform is None and own is None:
        raise ApiError(404, "not_found", "未找到规则")
    creates_override = own is None and service.override_of(db, membership.tenant_id, key) is None
    return _submit(
        db, membership, response, operation="update", key=key, fields=payload.model_dump(),
        title=f"租户规则变更 · 修改 {payload.name.strip()}", kind="override_created" if creates_override else "updated",
    )


@router.post("/tenant/pii-rules/{key}/restore", response_model=MutationResponse)
def restore_inherited_rule(
    key: UUID,
    response: Response,
    membership: Membership = Depends(require_tenant_permission("settings:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> MutationResponse:
    """“恢复”: delete the tenant's override so the platform rule applies again."""
    existing = service.override_of(db, membership.tenant_id, key)
    if existing is None:
        raise ApiError(409, "tenant_pii.nothing_to_restore", "该规则没有租户级修改")
    return _submit(db, membership, response, operation="restore", key=key, fields=None, title=f"租户规则变更 · 恢复 {existing.name}")


@router.delete("/tenant/pii-rules/{key}", response_model=MutationResponse)
def delete_tenant_rule(
    key: UUID,
    response: Response,
    membership: Membership = Depends(require_tenant_permission("settings:delete")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> MutationResponse:
    _, own = service._own_or_platform(db, membership.tenant_id, key)
    if own is None:
        raise ApiError(404 if service._own_or_platform(db, membership.tenant_id, key)[0] is None else 409, "tenant_pii.not_deletable", "只能删除租户自建规则；继承的规则可以恢复或停用")
    return _submit(db, membership, response, operation="delete", key=key, fields=None, title=f"租户规则变更 · 删除 {own.name}")
