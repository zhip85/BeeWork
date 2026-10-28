from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.api.assistants import (
    MODEL_ENVIRONMENT,
    UNNAMED,
    _actor_name,
    _audit,
    _enabled_chat_models,
    _ensure_name_free,
    _load,
    _load_for_write,
    _set_visible_orgs,
    _visible_org_ids,
)
from beework.api.conventions import ApiError, check_row_version
from beework.assistant_versions import (
    build_snapshot,
    diff_snapshots,
    execute_publish,
    latest_version,
    live_version,
    version_label,
)
from beework.auth import require_csrf
from beework.db import get_db
from beework.models import Membership, TenantModelEnvironment, utc_now
from beework.models.assistants import Assistant, AssistantVersion
from beework.rbac import require_tenant_permission
from beework.runtime.dsl import dsl_from_draft
from beework.runtime.errors import RuntimeFailure
from beework.schemas.assistants import AssistantResponse
from beework.schemas.versions import (
    CompareResponse,
    PublishBlocker,
    PublishPreview,
    PublishRequest,
    RestoreRequest,
    RollbackRequest,
    VersionDetail,
    VersionListResponse,
    VersionSummary,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _summary(version: AssistantVersion, live_id: UUID | None) -> VersionSummary:
    return VersionSummary(
        id=version.id,
        version=version.version,
        version_seq=version.version_seq,
        kind=version.kind,
        source_version_id=version.source_version_id,
        change_note=version.change_note,
        published_by=version.published_by,
        published_at=version.published_at,
        approval_id=version.approval_id,
        is_live=version.id == live_id,
    )


def _version_of(db: Session, assistant: Assistant, version_id: UUID) -> AssistantVersion:
    version = db.scalar(
        select(AssistantVersion).where(
            AssistantVersion.id == version_id,
            AssistantVersion.assistant_id == assistant.id,
            AssistantVersion.tenant_id == assistant.tenant_id,
        )
    )
    if version is None:
        raise ApiError(404, "not_found", "未找到指定版本")
    return version


def draft_snapshot(db: Session, assistant: Assistant) -> dict[str, Any]:
    """Snapshot of the current draft; raises RuntimeFailure when it cannot be turned into a runtime DSL."""
    dsl = dsl_from_draft(assistant)
    return build_snapshot(assistant, _visible_org_ids(db, assistant.id), dsl.model_dump(mode="json"))


def publish_blockers(db: Session, assistant: Assistant, snapshot: dict[str, Any] | None, error: RuntimeFailure | None) -> list[PublishBlocker]:
    """Minimal pre-publish checks (T-021 replaces them with the full configuration check)."""
    blockers: list[PublishBlocker] = []
    name = (snapshot or {}).get("name", assistant.name)
    if not name or name.startswith(UNNAMED):
        blockers.append(PublishBlocker(code="name_missing", message="请先为助理命名"))
    if not (snapshot or {}).get("description", assistant.description):
        blockers.append(PublishBlocker(code="description_missing", message="请填写使用场景 / 简介"))
    if error is not None:
        blockers.append(PublishBlocker(code=error.code, message=error.message))
        return blockers
    models, _ = _enabled_chat_models(db, assistant.tenant_id)
    if str((snapshot or {}).get("model_id")) not in {str(model.id) for model, _ in models}:
        blockers.append(PublishBlocker(code="runtime.model_unavailable", message="所选模型未在当前环境启用"))
    environment = db.scalar(
        select(TenantModelEnvironment).where(
            TenantModelEnvironment.tenant_id == assistant.tenant_id,
            TenantModelEnvironment.environment == MODEL_ENVIRONMENT,
        )
    )
    if environment is None or environment.gateway_sync_status != "synced":
        blockers.append(PublishBlocker(code="runtime.gateway_not_ready", message="该环境尚未同步到 AI 网关"))
    return blockers


def _snapshot_blockers(db: Session, assistant: Assistant, snapshot: dict[str, Any]) -> list[PublishBlocker]:
    return publish_blockers(db, assistant, snapshot, None)


def _preview(db: Session, assistant: Assistant) -> tuple[PublishPreview, dict[str, Any] | None]:
    snapshot, error = None, None
    try:
        snapshot = draft_snapshot(db, assistant)
    except RuntimeFailure as exc:
        error = exc
    live = live_version(db, assistant)
    diff = diff_snapshots(live.snapshot if live else None, snapshot) if snapshot else []
    blockers = publish_blockers(db, assistant, snapshot, error)
    next_seq = (latest_version(db, assistant.id).version_seq if latest_version(db, assistant.id) else 0) + 1
    has_changes = live is None or bool(diff)
    return (
        PublishPreview(
            next_version=version_label(next_seq),
            blockers=blockers,
            has_changes=has_changes,
            diff=diff,
            can_publish=not blockers and has_changes and assistant.status != "disabled",
        ),
        snapshot,
    )


def _assistant_response(db: Session, membership: Membership, assistant: Assistant) -> AssistantResponse:
    from beework.api.assistants import _response

    return _response(db, membership, assistant)


# ---------------------------------------------------------------------------
# Endpoints.
# ---------------------------------------------------------------------------


@router.get("/assistants/{assistant_id}/publish-preview", response_model=PublishPreview)
def publish_preview(
    assistant_id: UUID,
    membership: Membership = Depends(require_tenant_permission("assistants:publish")),
    db: Session = Depends(get_db),
) -> PublishPreview:
    assistant = _load(db, membership, assistant_id)
    return _preview(db, assistant)[0]


@router.post("/assistants/{assistant_id}/publish", response_model=VersionDetail, status_code=status.HTTP_201_CREATED)
def publish_assistant(
    assistant_id: UUID,
    payload: PublishRequest,
    membership: Membership = Depends(require_tenant_permission("assistants:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> VersionDetail:
    assistant = _load_for_write(db, membership, assistant_id)
    if assistant.status == "disabled":
        raise ApiError(409, "assistant.invalid_transition", "已停用的助理需先启用才能发布")
    preview, snapshot = _preview(db, assistant)
    if preview.blockers:
        raise ApiError(409, "assistant.publish_blocked", "；".join(b.message for b in preview.blockers))
    if not preview.has_changes:
        raise ApiError(409, "assistant.no_changes", "草稿与线上版本没有差异")
    # Without an approval flow (T-022) the publish takes effect immediately.
    version = execute_publish(
        db,
        assistant,
        snapshot,
        change_note=payload.change_note,
        kind="publish",
        actor_user_id=membership.user_id,
        actor_name=_actor_name(db, membership),
    )
    db.commit()
    return VersionDetail(**_summary(version, version.id).model_dump(), snapshot=version.snapshot, change_diff=version.change_diff)


@router.get("/assistants/{assistant_id}/versions", response_model=VersionListResponse)
def list_versions(
    assistant_id: UUID,
    membership: Membership = Depends(require_tenant_permission("assistants:view")),
    db: Session = Depends(get_db),
) -> VersionListResponse:
    assistant = _load(db, membership, assistant_id)
    versions = list(
        db.scalars(
            select(AssistantVersion)
            .where(AssistantVersion.assistant_id == assistant.id, AssistantVersion.tenant_id == assistant.tenant_id)
            .order_by(AssistantVersion.version_seq.desc())
        )
    )
    return VersionListResponse(
        items=[_summary(version, assistant.live_version_id) for version in versions],
        live_version_id=assistant.live_version_id,
        draft_version=assistant.draft_version,
    )


@router.get("/assistants/{assistant_id}/versions/compare", response_model=CompareResponse)
def compare_versions(
    assistant_id: UUID,
    from_ref: str = Query(alias="from", min_length=1, max_length=40),
    to_ref: str = Query(alias="to", min_length=1, max_length=40),
    membership: Membership = Depends(require_tenant_permission("assistants:view")),
    db: Session = Depends(get_db),
) -> CompareResponse:
    assistant = _load(db, membership, assistant_id)

    def resolve(ref: str) -> dict[str, Any]:
        if ref == "draft":
            try:
                return draft_snapshot(db, assistant)
            except RuntimeFailure:
                # An incomplete draft still compares: show it without the derived runtime part.
                return build_snapshot(assistant, _visible_org_ids(db, assistant.id), {})
        try:
            return _version_of(db, assistant, UUID(ref)).snapshot
        except ValueError:
            raise ApiError(422, "validation.invalid", "版本标识无效") from None

    return CompareResponse(from_ref=from_ref, to_ref=to_ref, changes=diff_snapshots(resolve(from_ref), resolve(to_ref)))


@router.get("/assistants/{assistant_id}/versions/{version_id}", response_model=VersionDetail)
def get_version(
    assistant_id: UUID,
    version_id: UUID,
    membership: Membership = Depends(require_tenant_permission("assistants:view")),
    db: Session = Depends(get_db),
) -> VersionDetail:
    assistant = _load(db, membership, assistant_id)
    version = _version_of(db, assistant, version_id)
    return VersionDetail(
        **_summary(version, assistant.live_version_id).model_dump(), snapshot=version.snapshot, change_diff=version.change_diff
    )


@router.post("/assistants/{assistant_id}/versions/{version_id}/restore", response_model=AssistantResponse)
def restore_version(
    assistant_id: UUID,
    version_id: UUID,
    payload: RestoreRequest,
    membership: Membership = Depends(require_tenant_permission("assistants:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> AssistantResponse:
    """Overwrite the draft with a historical snapshot. It does not go live until published again."""
    assistant = _load_for_write(db, membership, assistant_id)
    check_row_version(assistant, payload.row_version, db)
    snapshot = _version_of(db, assistant, version_id).snapshot
    _ensure_name_free(db, assistant.tenant_id, snapshot["name"], exclude_id=assistant.id)
    assistant.name = snapshot["name"]
    assistant.description = snapshot["description"]
    assistant.category = snapshot["category"]
    assistant.icon = snapshot.get("icon")
    assistant.color = snapshot.get("color")
    assistant.visibility = snapshot["visibility"]
    assistant.draft_config = dict(snapshot["config"])
    _set_visible_orgs(db, assistant, [UUID(item) for item in snapshot.get("visible_org_ids", [])])
    assistant.row_version += 1
    assistant.updated_by = _actor_name(db, membership)
    assistant.updated_at = utc_now()
    _audit(db, membership, "assistant.version_restored", assistant, version_id=str(version_id))
    db.commit()
    return _assistant_response(db, membership, assistant)


@router.post("/assistants/{assistant_id}/versions/{version_id}/rollback", response_model=VersionDetail, status_code=status.HTTP_201_CREATED)
def rollback_to_version(
    assistant_id: UUID,
    version_id: UUID,
    payload: RollbackRequest,
    membership: Membership = Depends(require_tenant_permission("assistants:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> VersionDetail:
    """Create a new version from a historical snapshot and make it live (the draft is left alone)."""
    assistant = _load_for_write(db, membership, assistant_id)
    if assistant.status == "disabled":
        raise ApiError(409, "assistant.invalid_transition", "已停用的助理需先启用才能回滚")
    target = _version_of(db, assistant, version_id)
    if target.id == assistant.live_version_id:
        raise ApiError(409, "assistant.no_changes", "该版本已经是线上版本")
    blockers = _snapshot_blockers(db, assistant, target.snapshot)
    if blockers:
        raise ApiError(409, "assistant.publish_blocked", "；".join(b.message for b in blockers))
    note = payload.change_note.strip() or f"回滚到 v{target.version}"
    version = execute_publish(
        db,
        assistant,
        dict(target.snapshot),
        change_note=note,
        kind="rollback",
        actor_user_id=membership.user_id,
        actor_name=_actor_name(db, membership),
        source_version_id=target.id,
    )
    db.commit()
    return VersionDetail(**_summary(version, version.id).model_dump(), snapshot=version.snapshot, change_diff=version.change_diff)
