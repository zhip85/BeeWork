"""Tenant lifecycle access rules (PRD 3.12.6, 5.1, PER-004)."""

from typing import Literal

TENANT_STATUSES = (
    "trial",
    "active",
    "grace",
    "read_only",
    "suspended",
    "pending_deletion",
    "deleted",
)

TenantAccessMode = Literal["full", "read_only"]

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Environments a tenant can configure; non-enterprise plans only get production (RUL-004).
ENVIRONMENTS = ("development", "staging", "production")


def tenant_access_mode(tenant_status: str, member_role: str) -> TenantAccessMode | None:
    """Return how a member may use a tenant in the given lifecycle status.

    Read-only tenants keep view and export for every role; a suspended tenant is
    reachable only by its tenant administrators, who may view and export.
    """
    if tenant_status in {"trial", "active", "grace"}:
        return "full"
    if tenant_status == "read_only":
        return "read_only"
    if tenant_status == "suspended" and member_role == "tenant_admin":
        return "read_only"
    return None


def environments_for_plan(plan: str) -> tuple[str, ...]:
    return ENVIRONMENTS if plan == "enterprise" else ("production",)
