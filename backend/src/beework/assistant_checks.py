"""Assistant configuration check (T-021): the rules behind ``GET /assistants/{id}/checks`` and publish blockers."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework import model_catalog
from beework.agent_teams import REASON_TEXT
from beework.capabilities import resolve_mounts
from beework.gateway_sync import environment_ready
from beework.models import PlatformModel, TenantModelEnvironment
from beework.models.assistants import Assistant
from beework.schemas.assistant_checks import CheckIssue

UNNAMED = "未命名智能体"


def check_assistant(
    db: Session,
    assistant: Assistant,
    *,
    enabled_model_ids: set[str],
    environment: str,
    config: dict[str, Any] | None = None,
    name: str | None = None,
    description: str | None = None,
) -> list[CheckIssue]:
    """Run every rule against the draft (or against a frozen snapshot when ``config``/``name`` are given)."""
    config = assistant.draft_config or {} if config is None else config
    name = assistant.name if name is None else name
    description = assistant.description if description is None else description
    issues: list[CheckIssue] = []

    if not name or name.startswith(UNNAMED):
        issues.append(CheckIssue(code="name_missing", severity="blocker", message="请先为智能体命名", target="basic"))
    if not description:
        issues.append(CheckIssue(code="description_missing", severity="blocker", message="请填写使用场景 / 简介", target="basic"))

    from beework.runtime.oc import OCConfig

    oc = OCConfig.model_validate(config.get("oc") or {})
    model_id = config.get("model_id")
    # one catalog read for the main, rewrite and intent models (PLT-090)
    states = model_catalog.model_states(db, [m for m in (model_id, oc.rewrite.model_id, oc.intent.model_id) if m])
    if not model_id:
        issues.append(CheckIssue(code="runtime.model_unavailable", severity="blocker", message="智能体尚未选择模型", target="basic"))
    else:
        # PLT-090 + review fix 7: the catalog state is the single source — one issue per fault.
        # A disabled (or deleted) model is reported by the catalog rules below; the environment
        # allowlist rule only applies to a model that is still serving in the catalog.
        state = states.get(_uuid(model_id)) or model_catalog.ModelState(unavailable=True)
        if state.unavailable and state.fallback_name and _fallback_usable(db, assistant.tenant_id, state):
            issues.append(CheckIssue(code="runtime.model_disabled_fallback", severity="high", message=f"模型已停用，正在使用降级模型 {state.fallback_name}", target="basic"))
        elif state.unavailable:
            issues.append(CheckIssue(code="runtime.model_disabled", severity="blocker", message="所选模型已停用或已删除，且没有可用的降级模型，请更换模型", target="basic"))
        elif str(model_id) not in enabled_model_ids:
            issues.append(CheckIssue(code="runtime.model_unavailable", severity="blocker", message="所选模型未在当前环境启用", target="basic"))

    fallback_id = config.get("fallback_model_id")
    if fallback_id and str(fallback_id) not in enabled_model_ids:
        issues.append(CheckIssue(code="runtime.fallback_model_unavailable", severity="high", message="备选模型未在当前环境启用，主模型出错时无法切换", target="basic"))

    if not (config.get("prompt") or "").strip():
        issues.append(CheckIssue(code="runtime.prompt_missing", severity="blocker", message="智能体尚未填写提示词", target="basic"))

    env = db.scalar(
        select(TenantModelEnvironment).where(
            TenantModelEnvironment.tenant_id == assistant.tenant_id,
            TenantModelEnvironment.environment == environment,
        )
    )
    if not environment_ready(env):
        issues.append(CheckIssue(code="runtime.gateway_not_ready", severity="blocker", message="该环境尚未同步到 AI 网关", target="environment"))

    settings = config.get("mount_settings") or {}
    team = bool((config.get("multi_agent") or {}).get("enabled"))
    for mount in resolve_mounts(db, assistant.tenant_id, config.get("mounts") or {}):
        label = mount.name or str(mount.id)
        if mount.kind == "agent":
            # A delegated-to agent that stopped being a valid leaf blocks publishing (演进 PRD 6.7, 15.2).
            if not team:
                issues.append(CheckIssue(code="agent.team_disabled", severity="blocker", message="已挂载智能体，但未开启多智能体协作", target="capabilities"))
            if mount.state != "available":
                reason = REASON_TEXT.get(str(mount.meta.get("reason")), "已删除或不可用")
                issues.append(CheckIssue(code="agent.invalid", severity="blocker", message=f"挂载的智能体“{label}”{reason}，请移除或修复后再发布", target="capabilities"))
            if not str((settings.get(str(mount.id)) or {}).get("delegate") or "").strip():
                issues.append(CheckIssue(code="agent.delegate_missing", severity="blocker", message=f"请为挂载的智能体“{label}”填写委派描述（何时交给它）", target="capabilities"))
            continue
        if mount.state != "available":
            state = "已停用" if mount.state == "disabled" else "已删除或不可用"
            issues.append(
                CheckIssue(code="mount.invalid", severity="high", message=f"挂载的能力“{label}”{state}", target="capabilities")
            )

    issues.extend(_oc_model_issues(db, assistant.tenant_id, oc, states))
    issues.extend(_intent_issues(db, assistant, config, oc))

    if not (config.get("greeting") or "").strip():
        issues.append(CheckIssue(code="greeting_empty", severity="info", message="未设置开场白", target="basic"))
    return issues


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _fallback_usable(db: Session, tenant_id: UUID, state: "model_catalog.ModelState") -> bool:
    """The model that takes over a disabled one only serves this tenant if its plan / allowlist entitles it (its gateway scope)."""
    target = db.get(PlatformModel, state.fallback_id) if state.fallback_id else None
    return target is not None and model_catalog.tenant_may_use(db, tenant_id, target)


def _oc_model_issues(db: Session, tenant_id: UUID, oc: Any, states: dict[UUID, "model_catalog.ModelState"]) -> list[CheckIssue]:
    """T-014 review fix 5: the rewrite and intent-classifier models resolve through the catalog at
    run time just like the main model, so a disabled or deleted one is flagged the same way — only
    while the feature that uses it is switched on."""
    issues: list[CheckIssue] = []
    for label, code, enabled, model_id in (
        ("改写", "runtime.rewrite_model_disabled", oc.rewrite.enabled, oc.rewrite.model_id),
        ("意图", "runtime.intent_model_disabled", oc.intent.enabled, oc.intent.model_id),
    ):
        if not enabled or not model_id:
            continue
        state = states.get(_uuid(model_id)) or model_catalog.ModelState(unavailable=True)
        if state.unavailable and state.fallback_name and _fallback_usable(db, tenant_id, state):
            issues.append(CheckIssue(
                code=code, severity="high",
                message=f"{label}模型已停用，正在使用降级模型 {state.fallback_name}", target="orchestration",
            ))
        elif state.unavailable:
            issues.append(CheckIssue(
                code=code, severity="blocker",
                message=f"{label}模型已停用或已删除，请更换模型", target="orchestration",
            ))
    return issues


def _intent_issues(db: Session, assistant: Assistant, config: dict[str, Any], oc: Any) -> list[CheckIssue]:
    """T-161: a priority rule whose target is no longer a directly callable capability of the agent's scope would be skipped at run
    time; say so before publishing. Only while intent judgement and the priority rules are switched on (they are what runs)."""
    from beework import intent_service

    intent = oc.intent
    if not (intent.enabled and intent.priority_rules_enabled):
        return []
    callable_ids = set(intent_service.load_scope(db, assistant.tenant_id, config).callable_by_id())
    return [
        CheckIssue(code="route.rule_target_invalid", severity="high", message=f"意图规则“{rule.name}”的目标已失效、未挂载或不再允许直达，运行时将被跳过", target="orchestration")
        for rule in intent.rules if rule.enabled and rule.target_entry_id not in callable_ids
    ]
