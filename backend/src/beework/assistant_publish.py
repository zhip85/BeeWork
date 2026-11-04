"""Approval action ``assistant.publish`` (T-024): applies an approved (or policy-exempt) publish or rollback.

The snapshot was frozen when the request was submitted, so what goes live is exactly what the reviewers saw,
even if the draft changed while the request was pending.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select

from beework.approvals import ApplyContext, approval_action
from beework.errors import ApiError
from beework.models import User
from beework.models.assistants import Assistant
from beework.notifications import notify

ACTION = "assistant.publish"


def blocked(message: str) -> ApiError:
    return ApiError(409, "assistant.publish_blocked", message)


@approval_action(ACTION)
def apply_publish(db, context: ApplyContext) -> None:
    # Imported lazily: the API package imports this module while it is still initialising.
    from beework.api.assistants import MODEL_ENVIRONMENT, _enabled_chat_models
    from beework.assistant_checks import check_assistant
    from beework.assistant_versions import execute_publish

    payload: dict[str, Any] = context.payload
    snapshot = payload["snapshot"]
    assistant = db.scalar(
        select(Assistant)
        .where(
            Assistant.id == context.object_id,
            Assistant.tenant_id == context.tenant_id,
            Assistant.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if assistant is None:
        raise blocked("助理已被删除，无法发布")
    if assistant.status == "disabled":
        raise blocked("助理已停用，请启用后重试")
    models, _ = _enabled_chat_models(db, assistant.tenant_id)
    issues = check_assistant(
        db, assistant, enabled_model_ids={str(m.id) for m, _ in models}, environment=MODEL_ENVIRONMENT,
        config=snapshot.get("config"), name=snapshot.get("name"), description=snapshot.get("description"),
    )
    blockers = [issue.message for issue in issues if issue.severity == "blocker"]
    if blockers:
        raise blocked("；".join(blockers))
    requester = db.get(User, context.requester_id) if context.requester_id else None
    version = execute_publish(
        db,
        assistant,
        snapshot,
        change_note=payload.get("change_note", ""),
        kind=payload.get("kind", "publish"),
        actor_user_id=context.requester_id,
        actor_name=requester.display_name if requester else "",
        source_version_id=UUID(payload["source_version_id"]) if payload.get("source_version_id") else None,
        approval_id=context.approval_id,
    )
    recipients = [r for r in (assistant.owner_id, context.requester_id) if r is not None]
    if recipients:
        notify(
            db, tenant_id=assistant.tenant_id, recipient_ids=recipients, type="assistant.published",
            title=f"助理“{assistant.name}”已发布 v{version.version}", link=f"/assistants/{assistant.id}/versions",
            params={"name": assistant.name, "version": version.version},
        )
