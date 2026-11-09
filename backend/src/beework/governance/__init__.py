"""Platform PII rules and security baseline (T-050, PRD 3.16.2–3.16.3).

The platform layer of the three-level PII inheritance (platform → tenant → assistant). Tenants (T-052) and assistants
(T-055) build on ``list_platform_pii_rules`` / ``baseline_rules_for_tenant``; the runtime reads the same functions.

High-risk changes — adding, switching off or retiring an *enforced* rule — wait for a second platform administrator
(PLT-071) while ``PLATFORM_RULE_REVIEW`` is on; everything else applies at once.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.config import settings
from beework.errors import ApiError
from beework.models import AuditEvent, Tenant, User
from beework.models.governance import PlatformChangeRequest, PlatformPiiRule, SecurityBaselineRule

MATCH_TYPES = ("regex", "keyword", "classifier", "llm_judge")
PII_STAGES = ("both", "input", "output")
BASELINE_STAGES = ("both", "input", "output", "data")
ACTIONS = ("mask", "block", "flag")
CATEGORIES = ("pii", "prompt_attack", "content_safety", "data_retention")
LEVELS = ("enforced", "recommended")
SCOPES = ("all", "enterprise", "region_cn", "industry_finance")
STATUSES = ("enabled", "draft")
RULESET_KINDS = ("all", "enforced", "rules")
REVIEW_TIMEOUT = timedelta(hours=72)
FINANCE_INDUSTRIES = frozenset({"banking", "insurance"})
CHINA_REGIONS = frozenset({"cn_shanghai"})
MAX_RETENTION_DAYS = 3650


def invalid(message: str) -> ApiError:
    return ApiError(422, "governance.invalid", message)


# ---------------------------------------------------------------------------
# Validation (PRD 5.7: names trimmed, regular expressions must compile).
# ---------------------------------------------------------------------------


def _clean_pattern(match_type: str, pattern: str) -> str:
    pattern = (pattern or "").strip()
    if match_type in ("regex", "keyword") and not pattern:
        raise invalid("请填写匹配内容")
    if match_type == "regex":
        try:
            re.compile(pattern)
        except re.error as exc:
            raise invalid(f"正则表达式无效：{exc}") from exc
    if match_type == "keyword":
        words = [word.strip() for word in re.split(r"[,\n，]", pattern) if word.strip()]
        if not words:
            raise invalid("请至少填写一个关键词")
        pattern = ",".join(dict.fromkeys(words))
    return pattern


def _choice(value: str, allowed: tuple[str, ...], label: str) -> str:
    if value not in allowed:
        raise invalid(f"{label}无效")
    return value


def _name(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise invalid("请填写规则名称")
    if len(value) > 80:
        raise invalid("规则名称不能超过 80 个字符")
    return value


def clean_pii_rule(data: dict[str, Any]) -> dict[str, Any]:
    match_type = _choice(data.get("match_type", "regex"), MATCH_TYPES, "匹配方式")
    action = _choice(data.get("action", "mask"), ACTIONS, "命中动作")
    message = (data.get("block_message") or "").strip()
    if len(message) > 300:
        raise invalid("拦截提示不能超过 300 个字符")
    return {
        "name": _name(data.get("name", "")),
        "match_type": match_type,
        "pattern": _clean_pattern(match_type, data.get("pattern", "")),
        "stage": _choice(data.get("stage", "both"), PII_STAGES, "检测阶段"),
        "action": action,
        "block_message": message if action == "block" else "",
        "enforced": bool(data.get("enforced", False)),
        "enabled": bool(data.get("enabled", True)),
    }


def clean_baseline_rule(db: Session, data: dict[str, Any]) -> dict[str, Any]:
    category = _choice(data.get("category", "pii"), CATEGORIES, "分类")
    stage = _choice(data.get("stage", "both"), BASELINE_STAGES, "检测阶段")
    if (category == "data_retention") != (stage == "data"):
        raise invalid("数据保留类规则的检测阶段只能是“数据”，其他分类不能选“数据”")
    action = _choice(data.get("action", "flag"), ACTIONS, "命中动作")
    match_type = _choice(data.get("match_type", "keyword"), MATCH_TYPES, "匹配方式")
    message = (data.get("block_message") or "").strip()
    if len(message) > 300:
        raise invalid("拦截提示不能超过 300 个字符")
    cleaned: dict[str, Any] = {
        "name": _name(data.get("name", "")),
        "category": category,
        "stage": stage,
        "level": _choice(data.get("level", "recommended"), LEVELS, "执行级别"),
        "scope": _choice(data.get("scope", "all"), SCOPES, "下发范围"),
        "status": _choice(data.get("status", "draft"), STATUSES, "状态"),
        "action": action,
        "match_type": match_type,
        "block_message": message if action == "block" else "",
        "pattern": "",
        "ruleset": None,
    }
    if category == "pii":
        # PII rows reference platform PII rules (PLT-143): there is no pattern of their own.
        ruleset = data.get("ruleset") or {"kind": "all", "rule_ids": []}
        kind = _choice(ruleset.get("kind", "all"), RULESET_KINDS, "规则集类型")
        ids: list[str] = []
        if kind == "rules":
            ids = [str(item) for item in ruleset.get("rule_ids") or []]
            known = {str(i) for i in db.scalars(select(PlatformPiiRule.id)) }
            if not ids or any(item not in known for item in ids):
                raise invalid("规则集引用的平台 PII 规则不存在")
        cleaned["ruleset"] = {"kind": kind, "rule_ids": ids}
        cleaned["match_type"] = "regex"
    elif category == "data_retention":
        raw = str(data.get("pattern", "")).strip()
        if not raw.isdigit() or not 1 <= int(raw) <= MAX_RETENTION_DAYS:
            raise invalid(f"数据保留类规则的匹配内容填保留天数（1–{MAX_RETENTION_DAYS}）")
        cleaned["pattern"] = str(int(raw))
        cleaned["match_type"] = "keyword"
        cleaned["action"] = "flag"
        cleaned["block_message"] = ""
    else:
        cleaned["pattern"] = _clean_pattern(match_type, data.get("pattern", ""))
    return cleaned


# ---------------------------------------------------------------------------
# High-risk detection (PLT-071).
# ---------------------------------------------------------------------------


def pii_change_needs_review(before: dict[str, Any] | None, after: dict[str, Any] | None) -> bool:
    """Adding, retiring or switching off an enforced PII rule (``before`` / ``after`` are field dicts or ``None``)."""
    was = bool(before and before["enforced"])
    now = bool(after and after["enforced"])
    if before is None:
        return now  # a new enforced rule
    if after is None:
        return was  # deleting an enforced rule
    if was != now:
        return True  # becoming enforced / retired from enforcement
    return was and before["enabled"] and not after["enabled"]  # an enforced rule switched off


def baseline_change_needs_review(before: dict[str, Any] | None, after: dict[str, Any] | None) -> bool:
    was = bool(before and before["level"] == "enforced")
    now = bool(after and after["level"] == "enforced")
    if before is None:
        return now
    if after is None:
        return was
    if was != now:
        return True
    return was and before["status"] == "enabled" and after["status"] != "enabled"


def review_enabled() -> bool:
    return settings.platform_rule_review


# ---------------------------------------------------------------------------
# Rows <-> dicts.
# ---------------------------------------------------------------------------

PII_FIELDS = ("name", "match_type", "pattern", "stage", "action", "block_message", "enforced", "enabled")
BASELINE_FIELDS = (
    "name", "category", "stage", "level", "scope", "status", "action", "match_type", "pattern", "block_message", "ruleset",
)


def pii_fields(rule: PlatformPiiRule) -> dict[str, Any]:
    return {key: getattr(rule, key) for key in PII_FIELDS}


def baseline_fields(rule: SecurityBaselineRule) -> dict[str, Any]:
    return {key: getattr(rule, key) for key in BASELINE_FIELDS}


def actor_name(db: Session, user_id: UUID) -> str:
    user = db.get(User, user_id)
    return user.display_name if user else ""


def ensure_unique_name(db: Session, model: type, name: str, exclude_id: UUID | None = None) -> None:
    lowered = name.lower()
    for row in db.execute(select(model.id, model.name)):
        if row.name.lower() == lowered and row.id != exclude_id:
            raise ApiError(409, "governance.duplicate_name", "规则名称已被使用")


# ---------------------------------------------------------------------------
# Applying changes.
# ---------------------------------------------------------------------------

# Called with (db, deleted_rule_id) when a platform PII rule is deleted, so that tenant / assistant overrides of it can
# become rules of their own layer (PLT-133). The inheritance engine (T-052) registers here.
PLATFORM_RULE_DELETE_HOOKS: list[Callable[[Session, UUID], None]] = []


def apply_pii_change(db: Session, operation: str, rule_id: UUID | None, fields: dict[str, Any] | None, actor: str) -> PlatformPiiRule | None:
    if operation == "create":
        assert fields is not None
        ensure_unique_name(db, PlatformPiiRule, fields["name"])
        last = db.scalar(select(PlatformPiiRule.sort_order).order_by(PlatformPiiRule.sort_order.desc()).limit(1)) or 0
        rule = PlatformPiiRule(**fields, sort_order=last + 1, updated_by=actor)
        db.add(rule)
        db.flush()
        return rule
    rule = db.get(PlatformPiiRule, rule_id)
    if rule is None:
        raise ApiError(404, "not_found", "未找到平台 PII 规则")
    if operation == "delete":
        for hook in PLATFORM_RULE_DELETE_HOOKS:
            hook(db, rule.id)
        _drop_ruleset_references(db, rule.id)
        db.delete(rule)
        return None
    assert fields is not None
    ensure_unique_name(db, PlatformPiiRule, fields["name"], exclude_id=rule.id)
    for key, value in fields.items():
        setattr(rule, key, value)
    rule.row_version += 1
    rule.updated_by = actor
    return rule


def _drop_ruleset_references(db: Session, rule_id: UUID) -> None:
    """Baseline rule sets that listed a deleted rule stop listing it."""
    for row in db.scalars(select(SecurityBaselineRule).where(SecurityBaselineRule.category == "pii")):
        ids = (row.ruleset or {}).get("rule_ids") or []
        if str(rule_id) in ids:
            row.ruleset = {**row.ruleset, "rule_ids": [item for item in ids if item != str(rule_id)]}
            row.row_version += 1


def apply_baseline_change(db: Session, operation: str, rule_id: UUID | None, fields: dict[str, Any] | None, actor: str) -> SecurityBaselineRule | None:
    if operation == "create":
        assert fields is not None
        ensure_unique_name(db, SecurityBaselineRule, fields["name"])
        last = db.scalar(select(SecurityBaselineRule.sort_order).order_by(SecurityBaselineRule.sort_order.desc()).limit(1)) or 0
        rule = SecurityBaselineRule(**fields, sort_order=last + 1, updated_by=actor)
        db.add(rule)
        db.flush()
        return rule
    rule = db.get(SecurityBaselineRule, rule_id)
    if rule is None:
        raise ApiError(404, "not_found", "未找到安全基线规则")
    if operation == "delete":
        db.delete(rule)
        return None
    assert fields is not None
    ensure_unique_name(db, SecurityBaselineRule, fields["name"], exclude_id=rule.id)
    for key, value in fields.items():
        setattr(rule, key, value)
    rule.row_version += 1
    rule.updated_by = actor
    return rule


AUDIT_PREFIX = {"pii_rule": "platform_pii_rule", "baseline_rule": "baseline_rule"}
OPERATION_VERB = {"create": "created", "update": "updated", "delete": "deleted"}


def record_audit(db: Session, user_id: UUID | None, kind: str, operation: str, name: str, **details: str) -> None:
    db.add(
        AuditEvent(
            tenant_id=None, actor_user_id=user_id, action=f"{AUDIT_PREFIX[kind]}.{OPERATION_VERB[operation]}",
            details={"name": name, **details},
        )
    )


@dataclass
class ChangeOutcome:
    applied: bool
    rule: Any = None
    request: PlatformChangeRequest | None = None


def request_or_apply(
    db: Session,
    *,
    user: User,
    kind: str,
    operation: str,
    rule: Any,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    reason: str,
    base_row_version: int | None,
) -> ChangeOutcome:
    """Apply the change now, or park it for a second administrator when it is high-risk."""
    needs_review = pii_change_needs_review(before, after) if kind == "pii_rule" else baseline_change_needs_review(before, after)
    name = (after or before or {}).get("name", "")
    if needs_review and review_enabled():
        expire_stale_changes(db)
        object_id = rule.id if rule is not None else None
        if object_id is not None and db.scalar(
            select(PlatformChangeRequest.id).where(
                PlatformChangeRequest.kind == kind, PlatformChangeRequest.object_id == object_id, PlatformChangeRequest.status == "pending"
            )
        ):
            raise ApiError(409, "governance.pending_exists", "该规则已有待复核的变更")
        request = PlatformChangeRequest(
            kind=kind, operation=operation, object_id=object_id, object_name=name, base_row_version=base_row_version,
            payload=after, summary=_summary(kind, operation, name), reason=(reason or "").strip()[:500],
            requested_by=user.id, expires_at=datetime.now(UTC) + REVIEW_TIMEOUT,
        )
        db.add(request)
        db.flush()
        db.add(
            AuditEvent(
                tenant_id=None, actor_user_id=user.id, action="platform_rule.change_requested",
                details={"name": name, "operation": operation, "kind": kind, "request_id": str(request.id)},
            )
        )
        return ChangeOutcome(False, request=request)
    actor = user.display_name
    applier = apply_pii_change if kind == "pii_rule" else apply_baseline_change
    result = applier(db, operation, rule.id if rule is not None else None, after, actor)
    record_audit(db, user.id, kind, operation, name, **({"reason": reason} if reason else {}))
    return ChangeOutcome(True, rule=result)


def _summary(kind: str, operation: str, name: str) -> str:
    noun = "平台 PII 规则" if kind == "pii_rule" else "安全基线规则"
    verb = {"create": "新增", "update": "修改（停用或下线强制）", "delete": "删除"}[operation]
    return f"{verb}{noun}：{name}"[:300]


# ---------------------------------------------------------------------------
# Two-person review.
# ---------------------------------------------------------------------------


def expire_stale_changes(db: Session) -> int:
    now = datetime.now(UTC)
    count = 0
    for request in db.scalars(select(PlatformChangeRequest).where(PlatformChangeRequest.status == "pending")):
        expires = request.expires_at if request.expires_at.tzinfo else request.expires_at.replace(tzinfo=UTC)
        if expires <= now:
            request.status = "expired"
            request.review_comment = "72 小时内未复核，已自动失效"
            request.reviewed_at = now
            db.add(
                AuditEvent(
                    tenant_id=None, actor_user_id=None, action="platform_rule.change_expired",
                    details={"name": request.object_name, "request_id": str(request.id)},
                )
            )
            count += 1
    return count


def load_pending_for_review(db: Session, request_id: UUID, reviewer: User) -> PlatformChangeRequest:
    expire_stale_changes(db)
    request = db.scalar(select(PlatformChangeRequest).where(PlatformChangeRequest.id == request_id).with_for_update())
    if request is None:
        raise ApiError(404, "not_found", "未找到待复核的变更")
    if request.status != "pending":
        db.commit()  # keep the expiry just written
        raise ApiError(409, "governance.request_closed", "该变更已处理或已超时")
    if request.requested_by == reviewer.id:
        raise ApiError(403, "governance.self_review", "复核人不能是发起人")
    return request


def approve_change(db: Session, request: PlatformChangeRequest, reviewer: User, comment: str) -> None:
    applier = apply_pii_change if request.kind == "pii_rule" else apply_baseline_change
    model = PlatformPiiRule if request.kind == "pii_rule" else SecurityBaselineRule
    if request.operation != "create":
        current = db.get(model, request.object_id)
        if current is None or current.row_version != request.base_row_version:
            raise ApiError(409, "governance.stale_request", "规则在发起后已被修改，请驳回后重新发起")
    applier(db, request.operation, request.object_id, request.payload, actor_name(db, request.requested_by))
    request.status = "succeeded"
    request.reviewed_by = reviewer.id
    request.review_comment = (comment or "").strip()[:500] or None
    request.reviewed_at = datetime.now(UTC)
    record_audit(
        db, reviewer.id, request.kind, request.operation, request.object_name,
        request_id=str(request.id), requested_by=str(request.requested_by),
    )
    db.add(
        AuditEvent(
            tenant_id=None, actor_user_id=reviewer.id, action="platform_rule.change_approved",
            details={"name": request.object_name, "request_id": str(request.id)},
        )
    )


def reject_change(db: Session, request: PlatformChangeRequest, reviewer: User, comment: str) -> None:
    comment = (comment or "").strip()
    if not comment:
        raise ApiError(422, "governance.comment_required", "驳回必须填写原因")
    request.status = "rejected"
    request.reviewed_by = reviewer.id
    request.review_comment = comment[:500]
    request.reviewed_at = datetime.now(UTC)
    db.add(
        AuditEvent(
            tenant_id=None, actor_user_id=reviewer.id, action="platform_rule.change_rejected",
            details={"name": request.object_name, "request_id": str(request.id)},
        )
    )


# ---------------------------------------------------------------------------
# Inheritance helpers for tenants, assistants and the runtime.
# ---------------------------------------------------------------------------

# A tenant's industry is a code of the platform industry dictionary (T-051). ``INDUSTRY_RESOLVERS`` lets a caller override
# it (tests use it to place a tenant in an industry without editing the row).
IndustryResolver = Callable[[Session, Tenant], str | None]
INDUSTRY_RESOLVERS: list[IndustryResolver] = []


def tenant_industry(db: Session, tenant: Tenant) -> str | None:
    for resolver in INDUSTRY_RESOLVERS:
        industry = resolver(db, tenant)
        if industry:
            return industry
    return tenant.industry


def scope_matches(db: Session, tenant: Tenant, scope: str) -> bool:
    """PLT-141: whether a baseline rule's distribution scope covers the tenant."""
    if scope == "all":
        return True
    if scope == "enterprise":
        return tenant.plan == "enterprise"
    if scope == "region_cn":
        return tenant.region in CHINA_REGIONS
    if scope == "industry_finance":
        return tenant_industry(db, tenant) in FINANCE_INDUSTRIES
    return False


def list_platform_pii_rules(db: Session, *, enabled_only: bool = False) -> list[PlatformPiiRule]:
    statement = select(PlatformPiiRule).order_by(PlatformPiiRule.sort_order, PlatformPiiRule.created_at)
    if enabled_only:
        statement = statement.where(PlatformPiiRule.enabled.is_(True))
    return list(db.scalars(statement))


@dataclass
class EffectiveBaselineRule:
    id: UUID
    name: str
    category: str
    stage: str
    level: str
    action: str
    match_type: str
    pattern: str
    block_message: str
    pii_rules: list[PlatformPiiRule] = field(default_factory=list)  # for PII rule-set references


def resolve_ruleset(db: Session, ruleset: dict[str, Any] | None) -> list[PlatformPiiRule]:
    """The platform PII rules a PII baseline row stands for (enabled ones only)."""
    kind = (ruleset or {}).get("kind", "all")
    rules = list_platform_pii_rules(db, enabled_only=True)
    if kind == "enforced":
        return [rule for rule in rules if rule.enforced]
    if kind == "rules":
        wanted = set((ruleset or {}).get("rule_ids") or [])
        return [rule for rule in rules if str(rule.id) in wanted]
    return rules


def baseline_rules_for_tenant(db: Session, tenant: Tenant) -> list[EffectiveBaselineRule]:
    """Enabled baseline rules whose distribution scope covers the tenant (PLT-141); drafts are never distributed."""
    out: list[EffectiveBaselineRule] = []
    for row in db.scalars(
        select(SecurityBaselineRule).where(SecurityBaselineRule.status == "enabled").order_by(SecurityBaselineRule.sort_order)
    ):
        if not scope_matches(db, tenant, row.scope):
            continue
        out.append(
            EffectiveBaselineRule(
                id=row.id, name=row.name, category=row.category, stage=row.stage, level=row.level, action=row.action,
                match_type=row.match_type, pattern=row.pattern, block_message=row.block_message,
                pii_rules=resolve_ruleset(db, row.ruleset) if row.category == "pii" else [],
            )
        )
    return out


@dataclass(frozen=True)
class RetentionLimit:
    max_days: int | None  # enforced data-retention rules cap the tenant's retention (PLT-142)
    default_days: int | None  # recommended rules only suggest a default


def retention_limit_for_tenant(db: Session, tenant: Tenant | None) -> RetentionLimit:
    if tenant is None:
        return RetentionLimit(None, None)
    caps: list[int] = []
    defaults: list[int] = []
    for rule in baseline_rules_for_tenant(db, tenant):
        if rule.category != "data_retention" or not rule.pattern.isdigit():
            continue
        (caps if rule.level == "enforced" else defaults).append(int(rule.pattern))
    return RetentionLimit(min(caps) if caps else None, min(defaults) if defaults else None)
