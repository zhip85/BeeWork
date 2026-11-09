"""Platform PII rules, the security baseline and the two-person review of enforced-rule changes (T-050)."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from beework import governance
from beework.api.conventions import ApiError, check_row_version, require_platform_admin
from beework.auth import require_csrf
from beework.db import get_db
from beework.models import User
from beework.models.governance import PlatformChangeRequest, PlatformPiiRule, SecurityBaselineRule
from beework.schemas.governance import (
    BaselineRuleListResponse,
    BaselineRuleResponse,
    BaselineRuleWrite,
    ChangeRequestResponse,
    PiiRuleListResponse,
    PiiRuleResponse,
    PiiRuleWrite,
    ReviewRequest,
)

router = APIRouter()


def _pending_ids(db: Session, kind: str) -> dict[UUID, UUID]:
    rows = db.execute(
        select(PlatformChangeRequest.object_id, PlatformChangeRequest.id).where(
            PlatformChangeRequest.kind == kind,
            PlatformChangeRequest.status == "pending",
            PlatformChangeRequest.object_id.is_not(None),
        )
    )
    return {object_id: request_id for object_id, request_id in rows}


def _pii_response(rule: PlatformPiiRule, pending: dict[UUID, UUID]) -> PiiRuleResponse:
    return PiiRuleResponse(
        id=rule.id, name=rule.name, match_type=rule.match_type, pattern=rule.pattern, stage=rule.stage, action=rule.action,
        block_message=rule.block_message, enforced=rule.enforced, enabled=rule.enabled, row_version=rule.row_version,
        updated_by=rule.updated_by, updated_at=rule.updated_at, pending_request_id=pending.get(rule.id),
    )


def _baseline_response(rule: SecurityBaselineRule, pending: dict[UUID, UUID]) -> BaselineRuleResponse:
    return BaselineRuleResponse(
        id=rule.id, name=rule.name, category=rule.category, stage=rule.stage, level=rule.level, scope=rule.scope,
        status=rule.status, action=rule.action, match_type=rule.match_type, pattern=rule.pattern,
        block_message=rule.block_message, ruleset=rule.ruleset, row_version=rule.row_version, updated_by=rule.updated_by,
        updated_at=rule.updated_at, pending_request_id=pending.get(rule.id),
    )


def request_response(db: Session, request: PlatformChangeRequest) -> ChangeRequestResponse:
    reviewer = db.get(User, request.reviewed_by) if request.reviewed_by else None
    return ChangeRequestResponse(
        id=request.id, kind=request.kind, operation=request.operation, object_id=request.object_id,
        object_name=request.object_name, summary=request.summary, reason=request.reason, status=request.status,
        requested_by_id=request.requested_by, requested_by_name=governance.actor_name(db, request.requested_by),
        reviewed_by_name=reviewer.display_name if reviewer else None, review_comment=request.review_comment,
        created_at=request.created_at, expires_at=request.expires_at, reviewed_at=request.reviewed_at,
    )


def _finish(db: Session, response: Response, outcome: governance.ChangeOutcome, ok_body):
    db.commit()
    if not outcome.applied:
        response.status_code = status.HTTP_202_ACCEPTED
        return request_response(db, outcome.request)
    return ok_body(outcome.rule)


# ---------------------------------------------------------------------------
# Platform PII rules (PLT-130–133).
# ---------------------------------------------------------------------------


@router.get("/platform/pii-rules", response_model=PiiRuleListResponse)
def list_pii_rules(_: User = Depends(require_platform_admin), db: Session = Depends(get_db)) -> PiiRuleListResponse:
    governance.expire_stale_changes(db)
    db.commit()
    pending = _pending_ids(db, "pii_rule")
    rules = governance.list_platform_pii_rules(db)
    return PiiRuleListResponse(
        items=[_pii_response(rule, pending) for rule in rules], total=len(rules),
        enforced=sum(1 for rule in rules if rule.enforced), enabled=sum(1 for rule in rules if rule.enabled),
    )


def _write_fields(payload: PiiRuleWrite) -> dict:
    return governance.clean_pii_rule(payload.model_dump(exclude={"row_version", "reason"}))


def _load_pii(db: Session, rule_id: UUID) -> PlatformPiiRule:
    rule = db.scalar(select(PlatformPiiRule).where(PlatformPiiRule.id == rule_id).with_for_update())
    if rule is None:
        raise ApiError(404, "not_found", "未找到平台 PII 规则")
    return rule


@router.post("/platform/pii-rules", response_model=PiiRuleResponse | ChangeRequestResponse, status_code=status.HTTP_201_CREATED)
def create_pii_rule(
    payload: PiiRuleWrite,
    response: Response,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    """201 with the rule, or 202 with a pending change when it adds an enforced rule and review is on (PLT-071)."""
    fields = _write_fields(payload)
    governance.ensure_unique_name(db, PlatformPiiRule, fields["name"])
    outcome = governance.request_or_apply(
        db, user=user, kind="pii_rule", operation="create", rule=None, before=None, after=fields,
        reason=payload.reason, base_row_version=None,
    )
    return _finish(db, response, outcome, lambda rule: _pii_response(rule, {}))


@router.put("/platform/pii-rules/{rule_id}", response_model=PiiRuleResponse | ChangeRequestResponse)
def update_pii_rule(
    rule_id: UUID,
    payload: PiiRuleWrite,
    response: Response,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    if payload.row_version is None:
        raise ApiError(422, "validation.invalid", "缺少 row_version")
    rule = _load_pii(db, rule_id)
    check_row_version(rule, payload.row_version, db)
    fields = _write_fields(payload)
    governance.ensure_unique_name(db, PlatformPiiRule, fields["name"], exclude_id=rule.id)
    outcome = governance.request_or_apply(
        db, user=user, kind="pii_rule", operation="update", rule=rule, before=governance.pii_fields(rule), after=fields,
        reason=payload.reason, base_row_version=rule.row_version,
    )
    return _finish(db, response, outcome, lambda updated: _pii_response(updated, {}))


@router.delete("/platform/pii-rules/{rule_id}", response_model=ChangeRequestResponse | None)
def delete_pii_rule(
    rule_id: UUID,
    response: Response,
    reason: str = Query(default="", max_length=500),
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    rule = _load_pii(db, rule_id)
    outcome = governance.request_or_apply(
        db, user=user, kind="pii_rule", operation="delete", rule=rule, before=governance.pii_fields(rule), after=None,
        reason=reason, base_row_version=rule.row_version,
    )
    db.commit()
    if not outcome.applied:
        response.status_code = status.HTTP_202_ACCEPTED
        return request_response(db, outcome.request)
    response.status_code = status.HTTP_204_NO_CONTENT
    return None


# ---------------------------------------------------------------------------
# Security baseline (PLT-140–144).
# ---------------------------------------------------------------------------


@router.get("/platform/baseline-rules", response_model=BaselineRuleListResponse)
def list_baseline_rules(
    status_filter: str | None = Query(default=None, alias="status", pattern="^(enabled|draft)$"),
    q: str | None = Query(default=None, max_length=100),
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> BaselineRuleListResponse:
    governance.expire_stale_changes(db)
    db.commit()
    pending = _pending_ids(db, "baseline_rule")
    counts = {"enabled": 0, "draft": 0}
    for state, count in db.execute(select(SecurityBaselineRule.status, func.count()).group_by(SecurityBaselineRule.status)):
        counts[state] = count
    counts["all"] = sum(counts.values())
    statement = select(SecurityBaselineRule).order_by(SecurityBaselineRule.sort_order, SecurityBaselineRule.created_at)
    if status_filter:
        statement = statement.where(SecurityBaselineRule.status == status_filter)
    if q and q.strip():
        pattern = f"%{q.strip().lower()}%"
        statement = statement.where(
            or_(func.lower(SecurityBaselineRule.name).like(pattern), func.lower(SecurityBaselineRule.pattern).like(pattern))
        )
    rows = list(db.scalars(statement))
    return BaselineRuleListResponse(items=[_baseline_response(row, pending) for row in rows], total=len(rows), counts=counts)


def _baseline_fields(db: Session, payload: BaselineRuleWrite) -> dict:
    data = payload.model_dump(exclude={"row_version", "reason"})
    if payload.ruleset is not None:
        data["ruleset"] = {"kind": payload.ruleset.kind, "rule_ids": [str(item) for item in payload.ruleset.rule_ids]}
    return governance.clean_baseline_rule(db, data)


def _load_baseline(db: Session, rule_id: UUID) -> SecurityBaselineRule:
    rule = db.scalar(select(SecurityBaselineRule).where(SecurityBaselineRule.id == rule_id).with_for_update())
    if rule is None:
        raise ApiError(404, "not_found", "未找到安全基线规则")
    return rule


@router.post("/platform/baseline-rules", response_model=BaselineRuleResponse | ChangeRequestResponse, status_code=status.HTTP_201_CREATED)
def create_baseline_rule(
    payload: BaselineRuleWrite,
    response: Response,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    fields = _baseline_fields(db, payload)
    governance.ensure_unique_name(db, SecurityBaselineRule, fields["name"])
    outcome = governance.request_or_apply(
        db, user=user, kind="baseline_rule", operation="create", rule=None, before=None, after=fields,
        reason=payload.reason, base_row_version=None,
    )
    return _finish(db, response, outcome, lambda rule: _baseline_response(rule, {}))


@router.put("/platform/baseline-rules/{rule_id}", response_model=BaselineRuleResponse | ChangeRequestResponse)
def update_baseline_rule(
    rule_id: UUID,
    payload: BaselineRuleWrite,
    response: Response,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    if payload.row_version is None:
        raise ApiError(422, "validation.invalid", "缺少 row_version")
    rule = _load_baseline(db, rule_id)
    check_row_version(rule, payload.row_version, db)
    fields = _baseline_fields(db, payload)
    governance.ensure_unique_name(db, SecurityBaselineRule, fields["name"], exclude_id=rule.id)
    outcome = governance.request_or_apply(
        db, user=user, kind="baseline_rule", operation="update", rule=rule, before=governance.baseline_fields(rule),
        after=fields, reason=payload.reason, base_row_version=rule.row_version,
    )
    return _finish(db, response, outcome, lambda updated: _baseline_response(updated, {}))


@router.delete("/platform/baseline-rules/{rule_id}", response_model=ChangeRequestResponse | None)
def delete_baseline_rule(
    rule_id: UUID,
    response: Response,
    reason: str = Query(default="", max_length=500),
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    rule = _load_baseline(db, rule_id)
    outcome = governance.request_or_apply(
        db, user=user, kind="baseline_rule", operation="delete", rule=rule, before=governance.baseline_fields(rule),
        after=None, reason=reason, base_row_version=rule.row_version,
    )
    db.commit()
    if not outcome.applied:
        response.status_code = status.HTTP_202_ACCEPTED
        return request_response(db, outcome.request)
    response.status_code = status.HTTP_204_NO_CONTENT
    return None


# ---------------------------------------------------------------------------
# Two-person review (PLT-071).
# ---------------------------------------------------------------------------


@router.get("/platform/rule-changes", response_model=list[ChangeRequestResponse])
def list_rule_changes(
    status_filter: str | None = Query(default=None, alias="status", pattern="^(pending|succeeded|rejected|expired)$"),
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> list[ChangeRequestResponse]:
    governance.expire_stale_changes(db)
    db.commit()
    statement = select(PlatformChangeRequest).order_by(PlatformChangeRequest.created_at.desc()).limit(200)
    if status_filter:
        statement = statement.where(PlatformChangeRequest.status == status_filter)
    return [request_response(db, request) for request in db.scalars(statement)]


@router.post("/platform/rule-changes/{request_id}/approve", response_model=ChangeRequestResponse)
def approve_rule_change(
    request_id: UUID,
    payload: ReviewRequest,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ChangeRequestResponse:
    request = governance.load_pending_for_review(db, request_id, user)
    governance.approve_change(db, request, user, payload.comment)
    db.commit()
    return request_response(db, request)


@router.post("/platform/rule-changes/{request_id}/reject", response_model=ChangeRequestResponse)
def reject_rule_change(
    request_id: UUID,
    payload: ReviewRequest,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ChangeRequestResponse:
    request = governance.load_pending_for_review(db, request_id, user)
    governance.reject_change(db, request, user, payload.comment)
    db.commit()
    return request_response(db, request)
