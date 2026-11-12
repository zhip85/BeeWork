"""OC · L2 intent judgement (T-161): the targets, the rule test and the references behind the intent editor (OC-060 / OC-061, AST-065)."""

from uuid import UUID

import regex
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework import capability_registry as registry
from beework import intent_service
from beework.api.assistants import _load
from beework.db import get_db
from beework.models import Membership
from beework.models.capability_registry import CapabilityEntry
from beework.rbac import require_tenant_permission
from beework.runtime.intent import REGEX_TIMEOUT_SECONDS
from beework.runtime.oc import OCConfig, keywords_of
from beework.schemas.assistant_oc_routing import (
    IntentProblemEntry,
    IntentReferences,
    IntentStatus,
    IntentTargetEntry,
    IntentTargetList,
    RuleTestRequest,
    RuleTestResponse,
)

router = APIRouter()


def _reason(entry: CapabilityEntry, status: str) -> str | None:
    if status != "available":
        return "disabled"
    if entry.kind == "kb":
        return "kb"
    if entry.writes:
        return "write"
    return None if entry.direct_enabled else "not_direct"


def draft_intent(assistant) -> OCConfig:
    return OCConfig.model_validate((assistant.draft_config or {}).get("oc") or {})


def rule_problems(db: Session, assistant) -> list[IntentProblemEntry]:
    """An enabled rule whose target is no longer a directly callable capability of the agent's scope (RUL-011)."""
    intent = draft_intent(assistant).intent
    callable_ids = set(intent_service.load_scope(db, assistant.tenant_id, assistant.draft_config or {}).callable_by_id())
    return [
        IntentProblemEntry(code="route.rule_target_invalid", message=f"意图规则“{rule.name}”的目标已失效、未挂载或不再允许直达，请重新选择", rule_id=rule.id)
        for rule in intent.rules if rule.enabled and rule.target_entry_id not in callable_ids
    ]


@router.get("/assistants/{assistant_id}/oc/routing/targets", response_model=IntentTargetList)
def intent_targets(
    assistant_id: UUID,
    membership: Membership = Depends(require_tenant_permission("orchestration:view")),
    db: Session = Depends(get_db),
) -> IntentTargetList:
    """OC-060: the capabilities this agent can see — mounted and ticked, sub-agents only while collaboration is on — and which of them a
    priority rule may point at. The rest stay for L3 and show why (not enabled for direct calls, a write, a knowledge base)."""
    assistant = _load(db, membership, assistant_id)
    seen = registry.visible_entries(db, membership.tenant_id, assistant.draft_config or {}, only_available=False)
    items = []
    for item in seen:
        reason = _reason(item.entry, item.status)
        items.append(IntentTargetEntry(
            id=str(item.entry.id), kind=item.entry.kind, name=item.entry.name, description=item.entry.description,  # type: ignore[arg-type]
            status=item.status, callable=reason is None, reason=reason,  # type: ignore[arg-type]
        ))
    db.commit()  # a first read may have backfilled the registry
    return IntentTargetList(items=items)


@router.get("/assistants/{assistant_id}/oc/routing/status", response_model=IntentStatus)
def intent_status(
    assistant_id: UUID,
    membership: Membership = Depends(require_tenant_permission("orchestration:view")),
    db: Session = Depends(get_db),
) -> IntentStatus:
    """The saved draft against the current scope: which rules are “目标失效 / 待重选” (OC-060, 总览计数)."""
    assistant = _load(db, membership, assistant_id)
    problems = rule_problems(db, assistant)
    invalid = {p.rule_id for p in problems if p.rule_id}
    enabled = [r for r in draft_intent(assistant).intent.rules if r.enabled]
    scope = len(registry.visible_entries(db, membership.tenant_id, assistant.draft_config or {}))
    db.commit()
    return IntentStatus(problems=problems, valid_rules=len([r for r in enabled if r.id not in invalid]), invalid_rules=len(invalid), in_scope=scope)


@router.post("/assistants/{assistant_id}/oc/routing/rule-test", response_model=RuleTestResponse)
def test_rule(
    assistant_id: UUID,
    payload: RuleTestRequest,
    membership: Membership = Depends(require_tenant_permission("orchestration:view")),
    db: Session = Depends(get_db),
) -> RuleTestResponse:
    """OC-061: does this (unsaved) rule hit the sample question? The same matching as the runtime, so what the editor shows is what a
    message will do. Nothing is saved and no model is called."""
    _load(db, membership, assistant_id)
    pattern = payload.pattern.strip()
    if payload.match_type == "keyword":
        words = keywords_of(pattern)
        lowered = payload.question.lower()
        return RuleTestResponse(valid=bool(words), error=None if words else "请填写至少一个关键词", matched=any(w in lowered for w in words), keywords=words)
    try:
        regex.compile(pattern)
    except regex.error:
        return RuleTestResponse(valid=False, error="正则表达式不合法")
    try:
        matched = regex.search(pattern, payload.question, flags=regex.IGNORECASE, timeout=REGEX_TIMEOUT_SECONDS) is not None
    except TimeoutError:
        matched = False
    return RuleTestResponse(valid=True, matched=matched)


@router.get("/assistants/{assistant_id}/oc/routing/references", response_model=IntentReferences)
def intent_references(
    assistant_id: UUID,
    source_id: UUID = Query(),
    membership: Membership = Depends(require_tenant_permission("orchestration:view")),
    db: Session = Depends(get_db),
) -> IntentReferences:
    """AST-065: before a capability (an MCP connection, an API plugin, a sub-agent, a knowledge base …) is unmounted, which priority rules of
    the draft point at one of its entries."""
    assistant = _load(db, membership, assistant_id)
    rules = draft_intent(assistant).intent.rules
    targets = {str(r.target_entry_id) for r in rules if r.target_entry_id}
    owned = {
        str(entry_id) for entry_id in db.scalars(
            select(CapabilityEntry.id).where(
                CapabilityEntry.tenant_id == membership.tenant_id, CapabilityEntry.source_id == source_id, CapabilityEntry.id.in_([UUID(t) for t in targets if _is_uuid(t)]),
            )
        )
    } if targets else set()
    names = [r.name for r in rules if r.target_entry_id in owned]
    return IntentReferences(rules=names, total=len(names))


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True
