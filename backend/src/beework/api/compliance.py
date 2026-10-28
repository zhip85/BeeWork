from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.api.common import lock_tenant
from beework.api.conventions import ApiError, check_row_version
from beework.auth import require_csrf
from beework.compliance import allowed_retention_choices, get_policy
from beework.db import get_db
from beework.models import AuditEvent, Membership, User
from beework.models.conversations import TenantCompliancePolicy
from beework.rbac import require_tenant_permission
from beework.schemas.conversations import CompliancePolicyResponse, CompliancePolicyWriteRequest

router = APIRouter()


def _response(db: Session, membership: Membership) -> CompliancePolicyResponse:
    policy = get_policy(db, membership.tenant_id)
    return CompliancePolicyResponse(
        retention_days=policy.retention_days,
        redact_pii=policy.redact_pii,
        allow_export=policy.allow_export,
        row_version=policy.row_version,
        retention_choices=allowed_retention_choices(policy.max_retention_days),
        max_retention_days=policy.max_retention_days,
    )


@router.get("/tenant/compliance", response_model=CompliancePolicyResponse)
def get_compliance(
    membership: Membership = Depends(require_tenant_permission("settings:view")),
    db: Session = Depends(get_db),
) -> CompliancePolicyResponse:
    response = _response(db, membership)
    db.commit()  # persists a lazily created subscription snapshot
    return response


@router.put("/tenant/compliance", response_model=CompliancePolicyResponse)
def update_compliance(
    payload: CompliancePolicyWriteRequest,
    membership: Membership = Depends(require_tenant_permission("settings:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> CompliancePolicyResponse:
    lock_tenant(db, membership.tenant_id)
    current = get_policy(db, membership.tenant_id)
    choices = allowed_retention_choices(current.max_retention_days)
    if payload.retention_days not in choices:
        raise ApiError(
            422,
            "compliance.retention_invalid",
            "保留期只能选择 " + "、".join(str(days) for days in choices) + " 天（不得超过当前套餐的日志保留期）",
        )
    row = db.scalar(select(TenantCompliancePolicy).where(TenantCompliancePolicy.tenant_id == membership.tenant_id))
    actor = db.get(User, membership.user_id)
    before = {
        "retention_days": str(current.retention_days),
        "redact_pii": str(current.redact_pii),
        "allow_export": str(current.allow_export),
    }
    if row is None:
        if payload.row_version != 0:
            raise ApiError(409, "conflict.stale", "该对象已被修改，请刷新后重试")
        row = TenantCompliancePolicy(tenant_id=membership.tenant_id)
        db.add(row)
    else:
        check_row_version(row, payload.row_version, db)
        row.row_version += 1
    row.retention_days = payload.retention_days
    row.redact_pii = payload.redact_pii
    row.allow_export = payload.allow_export
    row.updated_by = actor.display_name if actor else None
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            action="compliance.updated",
            details={
                **{f"from_{key}": value for key, value in before.items()},
                "retention_days": str(payload.retention_days),
                "redact_pii": str(payload.redact_pii),
                "allow_export": str(payload.allow_export),
            },
        )
    )
    db.commit()
    return _response(db, membership)
