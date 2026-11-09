"""Tenant PII rule library (T-052, SET-050–054): the tenant layer of the PII inheritance.

Every change goes through ``submit_change`` with the “助理规则变更” policy: applied at once while the policy is off,
otherwise it becomes an approval request whose executor (``tenant.pii_rules``) writes the same change.
"""

from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework.approvals import ApplyContext, approval_action
from beework.errors import ApiError
from beework.governance import actor_name, clean_pii_rule, list_platform_pii_rules
from beework.governance.inheritance import (
    EffectiveRule,
    Rule,
    Switches,
    effective_count,
    effective_rules,
)
from beework.models import AuditEvent
from beework.models.governance import PlatformPiiRule
from beework.models.tenant_rules import TenantPiiRule, TenantPiiSettings

ACTION = "tenant.pii_rules"
OBJECT_TYPE = "tenant_pii_rule"
EVENT = "rule"  # approval policy event “助理规则变更”
MAX_TENANT_RULES = 200
FIELDS = ("name", "match_type", "pattern", "stage", "action", "block_message", "enabled")


def blocked(message: str) -> ApiError:
    return ApiError(409, "tenant_pii.blocked", message)


# ---------------------------------------------------------------------------
# Reading.
# ---------------------------------------------------------------------------


def to_rule(row: PlatformPiiRule | TenantPiiRule, layer: str) -> Rule:
    return Rule(
        id=str(row.id), layer=layer, name=row.name, match_type=row.match_type, pattern=row.pattern, stage=row.stage,
        action=row.action, block_message=row.block_message, enforced=bool(getattr(row, "enforced", False)), enabled=row.enabled,
        from_id=str(row.from_id) if getattr(row, "from_id", None) else None,
    )


def get_settings(db: Session, tenant_id: UUID) -> TenantPiiSettings | None:
    return db.get(TenantPiiSettings, tenant_id)


def inherits_platform(db: Session, tenant_id: UUID) -> bool:
    row = get_settings(db, tenant_id)
    return True if row is None else row.inherit_platform


def tenant_rows(db: Session, tenant_id: UUID) -> list[TenantPiiRule]:
    return list(db.scalars(select(TenantPiiRule).where(TenantPiiRule.tenant_id == tenant_id).order_by(TenantPiiRule.created_at, TenantPiiRule.id)))


def tenant_effective(db: Session, tenant_id: UUID) -> list[EffectiveRule]:
    """The rules in force for the tenant (what an assistant that references the global rules starts from)."""
    platform = [to_rule(rule, "platform") for rule in list_platform_pii_rules(db)]
    own = [to_rule(row, "tenant") for row in tenant_rows(db, tenant_id)]
    return effective_rules(platform, own, switches=Switches(inherit_platform=inherits_platform(db, tenant_id)))


def summary(db: Session, tenant_id: UUID) -> dict[str, int]:
    """The numbers of the inheritance chain header (SET-050)."""
    platform = list_platform_pii_rules(db)
    rows = tenant_rows(db, tenant_id)
    effective = tenant_effective(db, tenant_id)
    return {
        "platform_total": len(platform), "platform_enforced": sum(1 for rule in platform if rule.enforced),
        "inherited": sum(1 for item in effective if item.label != "tenant"), "tenant_level": sum(1 for row in rows if row.from_id is None),
        "overrides": sum(1 for row in rows if row.from_id is not None), "total": len(effective), "effective": effective_count(effective),
    }


# ---------------------------------------------------------------------------
# Writing (shared by the direct path and the approval executor).
# ---------------------------------------------------------------------------


def _clean(fields: dict[str, Any]) -> dict[str, Any]:
    cleaned = clean_pii_rule({**fields, "enforced": False})
    cleaned.pop("enforced")  # a tenant can neither make a rule enforced nor relax an enforced one
    return cleaned


def _ensure_unique_name(db: Session, tenant_id: UUID, name: str, exclude_id: UUID | None = None, platform_id: UUID | None = None) -> None:
    lowered = name.lower()
    for row in tenant_rows(db, tenant_id):
        if row.name.lower() == lowered and row.id != exclude_id:
            raise ApiError(409, "tenant_pii.duplicate_name", "规则名称已被使用")
    for rule in list_platform_pii_rules(db):
        if rule.name.lower() == lowered and rule.id != platform_id:
            raise ApiError(409, "tenant_pii.duplicate_name", "规则名称与平台规则重复")


def _audit(db: Session, tenant_id: UUID, user_id: UUID | None, action: str, name: str, **details: str) -> None:
    db.add(AuditEvent(tenant_id=tenant_id, actor_user_id=user_id, action=action, details={"name": name, **details}))


def _own_or_platform(db: Session, tenant_id: UUID, key: UUID) -> tuple[PlatformPiiRule | None, TenantPiiRule | None]:
    """The platform rule and / or the tenant row behind a key (a platform rule id or one of the tenant's own rule ids)."""
    platform = db.get(PlatformPiiRule, key)
    row = db.scalar(select(TenantPiiRule).where(TenantPiiRule.tenant_id == tenant_id, TenantPiiRule.id == key, TenantPiiRule.from_id.is_(None)))
    return platform, row


def override_of(db: Session, tenant_id: UUID, platform_id: UUID) -> TenantPiiRule | None:
    return db.scalar(select(TenantPiiRule).where(TenantPiiRule.tenant_id == tenant_id, TenantPiiRule.from_id == platform_id))


def apply_change(db: Session, tenant_id: UUID, user_id: UUID | None, operation: str, key: UUID | None, fields: dict[str, Any] | None) -> str:
    """Write one change; returns what happened (``created`` / ``override_created`` / ``updated`` / ``restored`` / ``deleted`` / ``settings``)."""
    actor = actor_name(db, user_id) if user_id else ""
    if operation == "settings":
        row = get_settings(db, tenant_id)
        value = bool((fields or {}).get("inherit_platform", True))
        if row is None:
            db.add(TenantPiiSettings(tenant_id=tenant_id, inherit_platform=value, updated_by=actor))
        else:
            row.inherit_platform, row.row_version, row.updated_by = value, row.row_version + 1, actor
        _audit(db, tenant_id, user_id, "tenant_pii_settings.updated", "继承平台 PII 规则", inherit_platform=str(value))
        return "settings"

    if operation == "create":
        assert fields is not None
        cleaned = _clean(fields)
        _ensure_unique_name(db, tenant_id, cleaned["name"])
        count = db.scalar(select(func.count()).select_from(TenantPiiRule).where(TenantPiiRule.tenant_id == tenant_id)) or 0
        if count >= MAX_TENANT_RULES:
            raise ApiError(422, "tenant_pii.too_many", f"租户规则最多 {MAX_TENANT_RULES} 条")
        db.add(TenantPiiRule(tenant_id=tenant_id, from_id=None, updated_by=actor, **cleaned))
        _audit(db, tenant_id, user_id, "tenant_pii_rule.created", cleaned["name"])
        return "created"

    assert key is not None
    platform, own = _own_or_platform(db, tenant_id, key)
    if operation == "update":
        assert fields is not None
        cleaned = _clean(fields)
        if own is not None:
            _ensure_unique_name(db, tenant_id, cleaned["name"], exclude_id=own.id)
            for field_name, value in cleaned.items():
                setattr(own, field_name, value)
            own.row_version += 1
            own.updated_by = actor
            _audit(db, tenant_id, user_id, "tenant_pii_rule.updated", cleaned["name"])
            return "updated"
        if platform is None:
            raise ApiError(404, "not_found", "未找到规则")
        existing = override_of(db, tenant_id, platform.id)
        _ensure_unique_name(db, tenant_id, cleaned["name"], exclude_id=existing.id if existing else None, platform_id=platform.id)
        if existing is not None:
            for field_name, value in cleaned.items():
                setattr(existing, field_name, value)
            existing.row_version += 1
            existing.updated_by = actor
            _audit(db, tenant_id, user_id, "tenant_pii_rule.updated", cleaned["name"], overrides=platform.name)
            return "updated"
        db.add(TenantPiiRule(tenant_id=tenant_id, from_id=platform.id, updated_by=actor, **cleaned))
        _audit(db, tenant_id, user_id, "tenant_pii_rule.overridden", cleaned["name"], overrides=platform.name)
        return "override_created"

    if operation == "restore":
        existing = override_of(db, tenant_id, key)
        if existing is None:
            raise ApiError(409, "tenant_pii.nothing_to_restore", "该规则没有租户级修改")
        _audit(db, tenant_id, user_id, "tenant_pii_rule.restored", existing.name)
        db.delete(existing)
        return "restored"

    if operation == "delete":
        if own is None:
            raise ApiError(409, "tenant_pii.not_deletable", "只能删除租户自建规则；继承的规则可以恢复或停用")
        _audit(db, tenant_id, user_id, "tenant_pii_rule.deleted", own.name)
        db.delete(own)
        return "deleted"
    raise ValueError(f"Unknown operation: {operation}")


def precheck(db: Session, tenant_id: UUID, operation: str, key: UUID | None, fields: dict[str, Any] | None) -> None:
    """Validate a change before it is queued, so an approver is never asked to approve something that cannot apply."""
    if operation not in ("create", "update"):
        return
    cleaned = _clean(fields or {})
    if operation == "create":
        _ensure_unique_name(db, tenant_id, cleaned["name"])
        return
    assert key is not None
    platform, own = _own_or_platform(db, tenant_id, key)
    if own is not None:
        _ensure_unique_name(db, tenant_id, cleaned["name"], exclude_id=own.id)
    elif platform is not None:
        existing = override_of(db, tenant_id, platform.id)
        _ensure_unique_name(db, tenant_id, cleaned["name"], exclude_id=existing.id if existing else None, platform_id=platform.id)


def object_id_for(tenant_id: UUID, key: UUID | None) -> UUID:
    """One pending approval per rule: the approval object is derived from the tenant and the rule key."""
    return uuid5(tenant_id, str(key) if key else "settings-or-new")


@approval_action(ACTION)
def apply_tenant_pii_change(db: Session, context: ApplyContext) -> None:
    payload = context.payload
    key = UUID(payload["key"]) if payload.get("key") else None
    try:
        apply_change(db, context.tenant_id, context.requester_id, payload["operation"], key, payload.get("fields"))
    except ApiError as exc:
        if context.approval_id is None:
            raise  # applied directly: the caller gets the precise error
        raise blocked(exc.detail) from exc


def diff_for(db: Session, tenant_id: UUID, operation: str, key: UUID | None, fields: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Field-level before / after of a change, shown to the approver (3.9.1)."""
    if operation == "settings":
        return [{"section": "tenant_pii.settings", "field": "inherit_platform", "from": inherits_platform(db, tenant_id), "to": (fields or {}).get("inherit_platform")}]
    before: dict[str, Any] = {}
    if key is not None:
        platform, own = _own_or_platform(db, tenant_id, key)
        current = own or (override_of(db, tenant_id, key) if platform else None) or platform
        if current is not None:
            before = {name: getattr(current, name) for name in FIELDS}
    if operation in ("delete", "restore"):
        return [{"section": "tenant_pii.rule", "field": "name", "from": before.get("name"), "to": None}]
    after = _clean(fields or {})
    return [
        {"section": "tenant_pii.rule", "field": name, "from": before.get(name), "to": after[name]}
        for name in FIELDS if before.get(name) != after[name]
    ] or [{"section": "tenant_pii.rule", "field": "name", "from": None, "to": after["name"]}]


# PLT-133: deleting a platform rule turns the tenants' overrides of it into rules of their own layer.
def _convert_overrides(db: Session, deleted_platform_id: UUID) -> None:
    for row in db.scalars(select(TenantPiiRule).where(TenantPiiRule.from_id == deleted_platform_id)):
        row.from_id = None


def register_hooks() -> None:
    from beework.governance import PLATFORM_RULE_DELETE_HOOKS

    if _convert_overrides not in PLATFORM_RULE_DELETE_HOOKS:
        PLATFORM_RULE_DELETE_HOOKS.append(_convert_overrides)


register_hooks()
