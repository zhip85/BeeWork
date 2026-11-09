"""Tenant data & privacy policy (SET-070): retention period, PII redaction in logs, export permission."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.entitlements import entitlement_value
from beework.governance import retention_limit_for_tenant
from beework.models import Tenant
from beework.models.conversations import TenantCompliancePolicy

RETENTION_CHOICES = (30, 90, 180, 365)
DEFAULT_RETENTION_DAYS = 180


@dataclass(frozen=True)
class CompliancePolicy:
    retention_days: int
    redact_pii: bool
    allow_export: bool
    row_version: int
    max_retention_days: int | None  # plan cap (log_retention_days); None = no cap


def max_retention_days(db: Session, tenant_id: UUID) -> int | None:
    """Retention can never exceed the plan's log retention (a "custom" plan has no cap) nor an enforced baseline rule (PLT-142)."""
    cap = entitlement_value(db, tenant_id, "log_retention_days")
    cap = cap if isinstance(cap, int) and not isinstance(cap, bool) else None
    baseline = retention_limit_for_tenant(db, db.get(Tenant, tenant_id)).max_days
    caps = [value for value in (cap, baseline) if value is not None]
    return min(caps) if caps else None


def allowed_retention_choices(cap: int | None) -> list[int]:
    return [days for days in RETENTION_CHOICES if cap is None or days <= cap]


def get_policy(db: Session, tenant_id: UUID) -> CompliancePolicy:
    """The tenant's stored policy, or defaults clamped to the plan's retention cap. Never writes."""
    cap = max_retention_days(db, tenant_id)
    row = db.scalar(select(TenantCompliancePolicy).where(TenantCompliancePolicy.tenant_id == tenant_id))
    choices = allowed_retention_choices(cap) or [min(RETENTION_CHOICES)]
    if row is None:
        # A recommended baseline rule suggests the default (PLT-142).
        target = retention_limit_for_tenant(db, db.get(Tenant, tenant_id)).default_days or DEFAULT_RETENTION_DAYS
        retention = max(days for days in choices if days <= target) if any(
            days <= target for days in choices
        ) else choices[0]
        return CompliancePolicy(retention, True, True, 0, cap)
    # A plan downgrade can leave a stored value above the new cap; the cap wins.
    retention = row.retention_days if cap is None else min(row.retention_days, cap)
    return CompliancePolicy(retention, row.redact_pii, row.allow_export, row.row_version, cap)
