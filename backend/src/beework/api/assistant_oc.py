"""OC page backend (T-054, OC-001–007, 3.6.2, 3.2.6): read / save the draft OC and submit it for approval."""

from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework import assistant_oc
from beework.api.assistants import (
    _actor_name,
    _audit,
    _can_edit,
    _load,
    _load_for_write,
    _visible_statement,
)
from beework.api.conventions import ApiError, check_row_version
from beework.approvals import ensure_policies, pending_request, submit_change
from beework.assistant_versions import diff_snapshots, latest_version, live_version, version_label
from beework.auth import require_csrf
from beework.db import get_db
from beework.models import Membership, utc_now
from beework.models.assistants import Assistant
from beework.rbac import require_tenant_permission
from beework.schemas.assistant_oc import (
    OCAssistant,
    OCAssistantOption,
    OCAssistantOptions,
    OCSaveRequest,
    OCStageSummary,
    OCStateResponse,
    OCSubmitRequest,
)
from beework.schemas.versions import ApprovalInfo, PendingApproval, VersionDetail

router = APIRouter()


def _state(db: Session, membership: Membership, assistant: Assistant) -> OCStateResponse:
    live = live_version(db, assistant)
    draft = assistant_oc.draft_oc(assistant)
    live_config = assistant_oc.live_oc(live.snapshot if live else None)
    diff = diff_snapshots(live.snapshot, assistant_oc.with_oc(live.snapshot, draft)) if live else []
    policy = ensure_policies(db, assistant.tenant_id)[assistant_oc.EVENT]
    pending = pending_request(db, assistant.tenant_id, assistant_oc.OBJECT_TYPE, assistant.id)
    return OCStateResponse(
        assistant=OCAssistant(
            id=assistant.id, name=assistant.name, icon=assistant.icon, color=assistant.color, status=assistant.status,
            draft_version=assistant.draft_version, live_version=live.version if live else None,
            can_edit=_can_edit(membership, assistant, db),
        ),
        oc=draft, live_oc=live_config, row_version=assistant.row_version, updated_at=assistant.updated_at,
        has_changes=bool(diff), diff=diff,
        stages=[OCStageSummary(**row) for row in assistant_oc.summaries(db, assistant, draft)],
        pending_approval_id=pending.id if pending else None,
        approval=ApprovalInfo(
            required=policy.enabled, levels=policy.levels, approver_role=policy.approver_role,
            pending_approval_id=pending.id if pending else None,
        ),
    )


@router.get("/oc/assistants", response_model=OCAssistantOptions)
def list_oc_assistants(
    membership: Membership = Depends(require_tenant_permission("orchestration:view")),
    db: Session = Depends(get_db),
) -> OCAssistantOptions:
    """The assistant selector of the OC page (OC-005): every assistant the caller can see."""
    rows = db.scalars(
        _visible_statement(
            select(Assistant).where(Assistant.tenant_id == membership.tenant_id, Assistant.deleted_at.is_(None)),
            membership, db,
        ).order_by(Assistant.name, Assistant.id)
    ).all()
    items = []
    for assistant in rows:
        live = live_version(db, assistant)
        items.append(OCAssistantOption(
            id=assistant.id, name=assistant.name, icon=assistant.icon, color=assistant.color,
            status=assistant.status, version=live.version if live else None,
        ))
    return OCAssistantOptions(items=items)


@router.get("/assistants/{assistant_id}/oc", response_model=OCStateResponse)
def get_oc(
    assistant_id: UUID,
    membership: Membership = Depends(require_tenant_permission("orchestration:view")),
    db: Session = Depends(get_db),
) -> OCStateResponse:
    assistant = _load(db, membership, assistant_id)
    state = _state(db, membership, assistant)
    db.commit()  # persists lazily created default approval policies
    return state


def _check_rewrite_model(db: Session, membership: Membership, oc) -> None:
    """OC-041: the rewrite model must be a chat model the tenant has enabled in the current environment."""
    model_id = oc.rewrite.model_id
    if not model_id:
        return
    from beework.api.assistants import _enabled_chat_models

    models, _ = _enabled_chat_models(db, membership.tenant_id)
    if model_id not in {str(model.id) for model, _ in models}:
        raise ApiError(422, "rewrite.model_unavailable", "改写模型未在当前环境启用，请在设置中启用对话模型")


@router.put("/assistants/{assistant_id}/oc", response_model=OCStateResponse)
def save_oc(
    assistant_id: UUID,
    payload: OCSaveRequest,
    membership: Membership = Depends(require_tenant_permission("orchestration:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> OCStateResponse:
    """OC-003: save the OC configuration as the assistant's draft. Nothing live changes."""
    assistant = _load_for_write(db, membership, assistant_id)
    check_row_version(assistant, payload.row_version, db)
    _check_rewrite_model(db, membership, payload.oc)
    config = dict(assistant.draft_config or {})
    config["oc"] = payload.oc.model_dump(mode="json")
    assistant.draft_config = config
    assistant.row_version += 1
    assistant.updated_by = _actor_name(db, membership)
    assistant.updated_at = utc_now()
    _audit(db, membership, "assistant.oc_saved", assistant)
    db.commit()
    return _state(db, membership, assistant)


@router.post("/assistants/{assistant_id}/oc/submit", response_model=VersionDetail | PendingApproval, status_code=status.HTTP_201_CREATED)
def submit_oc(
    assistant_id: UUID,
    payload: OCSubmitRequest,
    response: Response,
    membership: Membership = Depends(require_tenant_permission("orchestration:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> VersionDetail | PendingApproval:
    """OC-004: 201 with the new version when the “智能体规则变更” policy is off, 202 with the pending approval otherwise.
    The new version is the live snapshot with only its OC replaced."""
    from beework.api.assistant_versions import _version_response

    assistant = _load_for_write(db, membership, assistant_id)
    if assistant.status == "disabled":
        raise ApiError(409, "assistant.invalid_transition", "已停用的智能体需先启用才能提交 OC 变更")
    live = live_version(db, assistant)
    if live is None:
        raise ApiError(409, "assistant.not_published", "请先发布智能体，再提交 OC 变更")
    draft = assistant_oc.draft_oc(assistant)
    diff = diff_snapshots(live.snapshot, assistant_oc.with_oc(live.snapshot, draft))
    if not diff:
        raise ApiError(409, "assistant.no_changes", "OC 草稿与线上版本没有差异")
    latest = latest_version(db, assistant.id)
    next_label = version_label((latest.version_seq if latest else 0) + 1)
    result = submit_change(
        db,
        tenant_id=assistant.tenant_id,
        event=assistant_oc.EVENT,
        action=assistant_oc.ACTION,
        object_type=assistant_oc.OBJECT_TYPE,
        object_id=assistant.id,
        title=f"OC 变更 · {assistant.name}",
        payload={"oc": draft.model_dump(mode="json"), "change_note": payload.change_note},
        change_diff=diff,
        checks=[{"label": "只合并 OC，不含提示词、模型等其他草稿修改", "level": "ok"}],
        description=payload.change_note,
        requester_id=membership.user_id,
        organization_id=assistant.organization_id,
        owner_user_id=assistant.owner_id,
        replace_pending=payload.replace_pending,
    )
    db.commit()
    if result.approval is None:
        db.refresh(assistant)
        return _version_response(latest_version(db, assistant.id))
    response.status_code = status.HTTP_202_ACCEPTED
    return PendingApproval(approval_id=result.approval.id, next_version=next_label)
