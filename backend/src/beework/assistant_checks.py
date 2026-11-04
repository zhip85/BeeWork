"""Assistant configuration check (T-021): the rules behind ``GET /assistants/{id}/checks`` and publish blockers."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.capabilities import resolve_mounts
from beework.models import TenantModelEnvironment
from beework.models.assistants import Assistant
from beework.schemas.assistant_checks import CheckIssue

UNNAMED = "未命名助理"


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
        issues.append(CheckIssue(code="name_missing", severity="blocker", message="请先为助理命名", target="basic"))
    if not description:
        issues.append(CheckIssue(code="description_missing", severity="blocker", message="请填写使用场景 / 简介", target="basic"))

    model_id = config.get("model_id")
    if not model_id:
        issues.append(CheckIssue(code="runtime.model_unavailable", severity="blocker", message="助理尚未选择模型", target="basic"))
    elif str(model_id) not in enabled_model_ids:
        issues.append(CheckIssue(code="runtime.model_unavailable", severity="blocker", message="所选模型未在当前环境启用", target="basic"))

    if not (config.get("prompt") or "").strip():
        issues.append(CheckIssue(code="runtime.prompt_missing", severity="blocker", message="助理尚未填写提示词", target="basic"))

    env = db.scalar(
        select(TenantModelEnvironment).where(
            TenantModelEnvironment.tenant_id == assistant.tenant_id,
            TenantModelEnvironment.environment == environment,
        )
    )
    if env is None or env.gateway_sync_status != "synced":
        issues.append(CheckIssue(code="runtime.gateway_not_ready", severity="blocker", message="该环境尚未同步到 AI 网关", target="environment"))

    for mount in resolve_mounts(db, assistant.tenant_id, config.get("mounts") or {}):
        if mount.state != "available":
            label = mount.name or str(mount.id)
            state = "已停用" if mount.state == "disabled" else "已删除或不可用"
            issues.append(
                CheckIssue(code="mount.invalid", severity="high", message=f"挂载的能力“{label}”{state}", target="capabilities")
            )

    if not (config.get("greeting") or "").strip():
        issues.append(CheckIssue(code="greeting_empty", severity="info", message="未设置开场白", target="basic"))
    return issues
