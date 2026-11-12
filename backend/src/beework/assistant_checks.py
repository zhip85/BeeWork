"""Assistant configuration check (T-021): the rules behind ``GET /assistants/{id}/checks`` and publish blockers."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.agent_teams import REASON_TEXT
from beework.capabilities import resolve_mounts
from beework.gateway_sync import environment_ready
from beework.models import TenantModelEnvironment
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

    model_id = config.get("model_id")
    if not model_id:
        issues.append(CheckIssue(code="runtime.model_unavailable", severity="blocker", message="智能体尚未选择模型", target="basic"))
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

    issues.extend(_intent_issues(db, assistant, config))

    if not (config.get("greeting") or "").strip():
        issues.append(CheckIssue(code="greeting_empty", severity="info", message="未设置开场白", target="basic"))
    return issues


def _intent_issues(db: Session, assistant: Assistant, config: dict[str, Any]) -> list[CheckIssue]:
    """T-161: a priority rule whose target is no longer a directly callable capability of the agent's scope would be skipped at run
    time; say so before publishing. Only while intent judgement and the priority rules are switched on (they are what runs)."""
    from beework import intent_service
    from beework.runtime.oc import OCConfig

    intent = OCConfig.model_validate(config.get("oc") or {}).intent
    if not (intent.enabled and intent.priority_rules_enabled):
        return []
    callable_ids = set(intent_service.load_scope(db, assistant.tenant_id, config).callable_by_id())
    return [
        CheckIssue(code="route.rule_target_invalid", severity="high", message=f"意图规则“{rule.name}”的目标已失效、未挂载或不再允许直达，运行时将被跳过", target="orchestration")
        for rule in intent.rules if rule.enabled and rule.target_entry_id not in callable_ids
    ]
