"""OC changes of an assistant (T-054, OC-003 / OC-004).

The OC configuration lives in ``draft_config.oc``; the live one is the ``oc`` of the live version's snapshot. Submitting
an OC change creates a new version that is the *current live snapshot with only its ``oc`` replaced*, so an OC approval
never publishes prompt, model or mount edits that nobody approved. The approval action re-reads the live snapshot when
it is applied, so a publish that happened while the request was pending is kept.
"""

import copy
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.approvals import ApplyContext, approval_action
from beework.errors import ApiError
from beework.models import User
from beework.models.assistants import Assistant
from beework.notifications import notify
from beework.runtime.oc import OCConfig

ACTION = "assistant.oc_change"
OBJECT_TYPE = "assistant_oc"  # distinct from "assistant" so an OC request never shows the assistant as “审批中”
EVENT = "rule"  # approval policy event “智能体规则变更”

STAGES = ("pii", "memory", "glossary", "rewrite", "routing", "components")
STAGE_GROUPS = {
    "pii": "input", "memory": "context", "glossary": "context", "rewrite": "context", "routing": "execute", "components": "output",
}

# Stage tasks (T-055 – T-059) register a function returning the one-line summary of their stage (PRD 3.6.2).
# A stage without one is shown as “即将开放”.
StageSummary = Callable[[Session, Assistant, OCConfig], str]
STAGE_SUMMARIES: dict[str, StageSummary] = {}


def register_summary(stage: str, summary: StageSummary) -> None:
    if stage not in STAGES:
        raise ValueError(f"Unknown OC stage: {stage}")
    STAGE_SUMMARIES[stage] = summary


def summaries(db: Session, assistant: Assistant, oc: OCConfig) -> list[dict[str, Any]]:
    out = []
    for index, stage in enumerate(STAGES, start=1):
        provider = STAGE_SUMMARIES.get(stage)
        out.append({
            "stage": stage, "index": index, "group": STAGE_GROUPS[stage],
            "available": provider is not None, "summary": provider(db, assistant, oc) if provider else None,
        })
    return out


def blocked(message: str) -> ApiError:
    return ApiError(409, "assistant.oc_blocked", message)


def draft_oc(assistant: Assistant) -> OCConfig:
    return OCConfig.model_validate((assistant.draft_config or {}).get("oc") or {})


def live_oc(live_snapshot: dict[str, Any] | None) -> OCConfig | None:
    if live_snapshot is None:
        return None
    return OCConfig.model_validate((live_snapshot.get("config") or {}).get("oc") or {})


def with_oc(live_snapshot: dict[str, Any], oc: OCConfig) -> dict[str, Any]:
    """The live snapshot with only its OC replaced."""
    snapshot = copy.deepcopy(live_snapshot)
    value = oc.model_dump(mode="json")
    snapshot.setdefault("config", {})["oc"] = value
    snapshot.setdefault("dsl", {})["oc"] = copy.deepcopy(value)
    return snapshot


@approval_action(ACTION)
def apply_oc_change(db, context: ApplyContext) -> None:
    # Imported lazily: the API package imports this module while it is still initialising.
    from beework.assistant_versions import execute_publish, live_version

    assistant = db.scalar(
        select(Assistant)
        .where(
            Assistant.id == context.object_id, Assistant.tenant_id == context.tenant_id, Assistant.deleted_at.is_(None)
        )
        .with_for_update()
    )
    if assistant is None:
        raise blocked("智能体已被删除，无法生效")
    if assistant.status == "disabled":
        raise blocked("智能体已停用，请启用后重试")
    live = live_version(db, assistant)
    if live is None:
        raise blocked("智能体尚未发布，请先发布智能体")
    payload: dict[str, Any] = context.payload
    oc = OCConfig.model_validate(payload["oc"])
    requester = db.get(User, context.requester_id) if context.requester_id else None
    version = execute_publish(
        db, assistant, with_oc(live.snapshot, oc),
        change_note=payload.get("change_note", ""), kind="oc_change",
        actor_user_id=context.requester_id, actor_name=requester.display_name if requester else "",
        approval_id=context.approval_id,
    )
    recipients = [r for r in (assistant.owner_id, context.requester_id) if r is not None]
    if recipients:
        notify(
            db, tenant_id=assistant.tenant_id, recipient_ids=recipients, type="assistant.published",
            title=f"智能体“{assistant.name}”的 OC 变更已生效 v{version.version}", link=f"/assistants/{assistant.id}/versions",
            params={"name": assistant.name, "version": version.version},
        )


def assistant_id_of(value: str | UUID) -> UUID:
    return value if isinstance(value, UUID) else UUID(value)


def _pii_summary(db: Session, assistant: Assistant, oc: OCConfig) -> str:
    from beework.governance.runtime_policy import overview_count

    count = overview_count(db, assistant.tenant_id, oc.pii)
    return f"{count} 条生效规则 · PII {'开启' if oc.pii.enabled else '关闭'} · 平台强制基线不受开关影响"


register_summary("pii", _pii_summary)


def _glossary_summary(db: Session, assistant: Assistant, oc: OCConfig) -> str:
    from beework.governance.glossary_runtime import assistant_view

    _, chain = assistant_view(db, assistant.tenant_id, oc.glossary)
    return f"平台 → 租户 → 智能体三级继承 · {chain['effective']} 条生效"


def _rewrite_summary(db: Session, assistant: Assistant, oc: OCConfig) -> str:
    from beework.runtime.rewrite import active_steps

    config = oc.rewrite
    if not config.enabled:
        return "已关闭"
    return f"已启用 · {len(active_steps(config))} 种策略"


def _routing_summary(db: Session, assistant: Assistant, oc: OCConfig) -> str:
    config = oc.intent
    if not config.enabled:
        return "已关闭 · 由智能体自行判断"
    rules = len([r for r in config.rules if r.enabled]) if config.priority_rules_enabled else 0
    return f"已启用 · 置信度阈值 {config.threshold:g} · {rules} 条优先规则"


register_summary("routing", _routing_summary)


def _memory_summary(db: Session, assistant: Assistant, oc: OCConfig) -> str:
    from beework.memory import memory_tier

    short, long = oc.memory.short_term, oc.memory.long_term
    window = f"短期 {short.window_rounds} 轮 / {short.max_tokens} Token" if short.enabled else "短期记忆关闭"
    if memory_tier(db, assistant.tenant_id) == "short":
        return f"{window} · 长期记忆需专业版及以上"
    if not long.enabled:
        return f"{window} · 长期记忆关闭"
    files = [name for name, file in (("画像", long.user_profile), ("用户记忆", long.user_memory), ("工具", long.tools), ("会话", long.session)) if file.enabled]
    return f"{window} · 长期记忆：{'、'.join(files) or '全部关闭'}"


register_summary("memory", _memory_summary)
register_summary("glossary", _glossary_summary)
register_summary("rewrite", _rewrite_summary)
