import secrets
import string
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework.api import assistants as assistants_api
from beework.api.assistants import _load as load_assistant
from beework.api.conventions import ApiError, check_row_version
from beework.approvals import pending_request, submit_change
from beework.auth import require_csrf
from beework.channel_publish import (
    ACTION,
    assistant_of,
    build_snapshot,
    effective_experience,
    has_unpublished_changes,
    version_label,
)
from beework.config import settings
from beework.db import get_db
from beework.entitlements import environments_for
from beework.models import AuditEvent, Membership, Tenant, utc_now
from beework.models.channels import ChannelInstance
from beework.rbac import require_tenant_permission
from beework.schemas.channels import (
    ChannelList,
    ChannelResponse,
    ChannelWrite,
    ExperienceOverrides,
    PendingChannelApproval,
    PublishChannelRequest,
)

router = APIRouter()

ALPHABET = string.ascii_lowercase + string.digits


def _site_key() -> str:
    return "bw_site_" + "".join(secrets.choice(ALPHABET) for _ in range(20))


def embed_code(site_key: str, request: Request) -> str:
    base = (settings.public_api_url or str(request.base_url)).rstrip("/")
    return f'<script src="{base}/v1/widget.js" data-site-key="{site_key}" async></script>'


def _response(db: Session, request: Request, channel: ChannelInstance) -> ChannelResponse:
    assistant = assistant_of(db, channel)
    pending = pending_request(db, channel.tenant_id, "channel", channel.id)
    return ChannelResponse(
        id=channel.id, type=channel.type, name=channel.name, assistant_id=channel.assistant_id,
        assistant_name=assistant.name if assistant else None, environment=channel.environment,
        allowed_origins=list(channel.allowed_origins or []),
        experience=ExperienceOverrides.model_validate(channel.experience or {}),
        effective_experience=effective_experience(channel.experience or {}, assistant) if assistant else {},
        status=channel.status, site_key=channel.site_key, published_version=version_label(channel.version_seq),
        published_at=channel.published_at, has_unpublished_changes=has_unpublished_changes(channel),
        pending_approval_id=pending.id if pending else None, row_version=channel.row_version,
        created_at=channel.created_at, updated_at=channel.updated_at,
        embed_code=embed_code(channel.site_key, request) if channel.published_snapshot else None,
    )


def _load(db: Session, membership: Membership, channel_id: UUID, *, lock: bool = False) -> ChannelInstance:
    statement = select(ChannelInstance).where(ChannelInstance.id == channel_id, ChannelInstance.tenant_id == membership.tenant_id)
    channel = db.scalar(statement.with_for_update() if lock else statement)
    if channel is None:
        raise ApiError(404, "not_found", "未找到指定渠道")
    return channel


def _audit(db: Session, membership: Membership, action: str, channel: ChannelInstance, **extra: str) -> None:
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id, actor_user_id=membership.user_id, action=action,
            details={"channel_id": str(channel.id), "name": channel.name, **extra},
        )
    )


def _validate(db: Session, membership: Membership, payload: ChannelWrite, *, exclude: UUID | None = None) -> None:
    load_assistant(db, membership, payload.assistant_id)  # 404 for other tenants' / invisible assistants
    tenant = db.get(Tenant, membership.tenant_id)
    if tenant is None or payload.environment not in environments_for(db, tenant.id):
        raise ApiError(403, "environment.unavailable", "当前套餐仅提供生产环境，开发与预发环境需企业版")
    statement = select(ChannelInstance.id).where(
        ChannelInstance.tenant_id == membership.tenant_id, func.lower(ChannelInstance.name) == payload.name.lower()
    )
    if exclude is not None:
        statement = statement.where(ChannelInstance.id != exclude)
    if db.scalar(statement) is not None:
        raise ApiError(409, "channel.name_taken", "渠道名称已被使用")


@router.get("/channels", response_model=ChannelList)
def list_channels(
    request: Request,
    assistant_id: UUID | None = None,
    q: str | None = Query(default=None, max_length=50),
    membership: Membership = Depends(require_tenant_permission("channels:view")),
    db: Session = Depends(get_db),
) -> ChannelList:
    statement = select(ChannelInstance).where(ChannelInstance.tenant_id == membership.tenant_id)
    if assistant_id:
        statement = statement.where(ChannelInstance.assistant_id == assistant_id)
    if q and q.strip():
        statement = statement.where(func.lower(ChannelInstance.name).contains(q.strip().lower()))
    channels = db.scalars(statement.order_by(ChannelInstance.created_at.desc()))
    return ChannelList(items=[_response(db, request, c) for c in channels])


@router.post("/channels", response_model=ChannelResponse, status_code=status.HTTP_201_CREATED)
def create_channel(
    payload: ChannelWrite,
    request: Request,
    membership: Membership = Depends(require_tenant_permission("channels:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ChannelResponse:
    _validate(db, membership, payload)
    channel = ChannelInstance(
        tenant_id=membership.tenant_id, type="website", name=payload.name, assistant_id=payload.assistant_id,
        environment=payload.environment, allowed_origins=payload.allowed_origins,
        experience=payload.experience.model_dump(), site_key=_site_key(), created_by=str(membership.user_id),
    )
    db.add(channel)
    db.flush()
    _audit(db, membership, "channel.created", channel)
    db.commit()
    return _response(db, request, channel)


@router.get("/channels/{channel_id}", response_model=ChannelResponse)
def get_channel(
    channel_id: UUID,
    request: Request,
    membership: Membership = Depends(require_tenant_permission("channels:view")),
    db: Session = Depends(get_db),
) -> ChannelResponse:
    return _response(db, request, _load(db, membership, channel_id))


@router.put("/channels/{channel_id}", response_model=ChannelResponse)
def update_channel(
    channel_id: UUID,
    payload: ChannelWrite,
    request: Request,
    membership: Membership = Depends(require_tenant_permission("channels:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ChannelResponse:
    """Edits the draft only; what visitors get changes when the channel is published again (RUL-003)."""
    channel = _load(db, membership, channel_id, lock=True)
    if payload.row_version is not None:
        check_row_version(channel, payload.row_version, db)
    _validate(db, membership, payload, exclude=channel.id)
    channel.name, channel.assistant_id, channel.environment = payload.name, payload.assistant_id, payload.environment
    channel.allowed_origins = payload.allowed_origins
    channel.experience = payload.experience.model_dump()
    channel.row_version += 1
    channel.updated_at = utc_now()
    _audit(db, membership, "channel.updated", channel)
    db.commit()
    return _response(db, request, channel)


@router.delete("/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_channel(
    channel_id: UUID,
    membership: Membership = Depends(require_tenant_permission("channels:delete")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    """The channel stops serving at once; its snapshot goes with it."""
    channel = _load(db, membership, channel_id, lock=True)
    _audit(db, membership, "channel.deleted", channel)
    db.delete(channel)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _diff(channel: ChannelInstance, snapshot: dict[str, Any], overrides: dict[str, Any]) -> list[dict[str, Any]]:
    live = channel.published_snapshot or {}
    rows = [("name", "名称"), ("environment", "环境"), ("allowed_origins", "允许域名")]
    changes = [
        {"section": "channel", "field": label, "from": live.get(key), "to": snapshot[key]}
        for key, label in rows
        if live.get(key) != snapshot[key]
    ]
    for key, label in (("primary_color", "主色"), ("assistant_name", "助手名称"), ("welcome", "欢迎语")):
        before = (live.get("experience") or {}).get(key)
        if before != snapshot["experience"][key]:
            changes.append({"section": "channel", "field": label, "from": before, "to": snapshot["experience"][key]})
    return changes


@router.post("/channels/{channel_id}/publish", response_model=ChannelResponse | PendingChannelApproval)
def publish_channel(
    channel_id: UUID,
    payload: PublishChannelRequest,
    request: Request,
    response: Response,
    membership: Membership = Depends(require_tenant_permission("channels:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ChannelResponse | PendingChannelApproval:
    """Freezes the snapshot. Applied at once when the policy is off (200), otherwise a pending approval (202)."""
    channel = _load(db, membership, channel_id, lock=True)
    assistant = assistant_of(db, channel)
    if assistant is None:
        raise ApiError(409, "channel.publish_blocked", "绑定的助理已被删除，请改绑其他助理")
    if not channel.allowed_origins:
        raise ApiError(422, "channel.incomplete", "请至少填写一个允许嵌入的域名")
    if assistant.status == "disabled":
        raise ApiError(409, "channel.publish_blocked", "绑定的助理已停用")
    if channel.environment == "production" and assistant.live_version_id is None:
        raise ApiError(409, "channel.publish_blocked", "生产环境的渠道需要先发布助理（开发/预发环境使用最新草稿）")
    snapshot = build_snapshot(channel, assistant)
    overrides = dict(channel.experience or {})
    result = submit_change(
        db, tenant_id=membership.tenant_id, event="channel", action=ACTION, object_type="channel", object_id=channel.id,
        title=f"发布渠道 {channel.name} {version_label(channel.version_seq + 1)}",
        payload={"snapshot": snapshot, "overrides": overrides}, change_diff=_diff(channel, snapshot, overrides),
        description=f"网站渠道，绑定助理“{assistant.name}”", requester_id=membership.user_id,
        organization_id=assistant.organization_id, owner_user_id=assistant.owner_id, replace_pending=payload.replace_pending,
    )
    db.commit()
    if result.approval is not None:
        response.status_code = status.HTTP_202_ACCEPTED
        return PendingChannelApproval(approval_id=result.approval.id)
    db.refresh(channel)
    return _response(db, request, channel)


def _toggle(db: Session, request: Request, membership: Membership, channel_id: UUID, enable: bool) -> ChannelResponse:
    channel = _load(db, membership, channel_id, lock=True)
    if enable:
        if channel.published_snapshot is None:
            raise ApiError(409, "channel.not_published", "渠道还没有发布过，请先发布")
        if channel.status == "active":
            return _response(db, request, channel)
        channel.status = "active"
    else:
        if channel.status == "disabled":
            return _response(db, request, channel)
        channel.status = "disabled"
    channel.row_version += 1
    _audit(db, membership, "channel.enabled" if enable else "channel.disabled", channel)
    db.commit()
    return _response(db, request, channel)


@router.post("/channels/{channel_id}/enable", response_model=ChannelResponse)
def enable_channel(
    channel_id: UUID, request: Request,
    membership: Membership = Depends(require_tenant_permission("channels:publish")),
    _: None = Depends(require_csrf), db: Session = Depends(get_db),
) -> ChannelResponse:
    return _toggle(db, request, membership, channel_id, True)


@router.post("/channels/{channel_id}/disable", response_model=ChannelResponse)
def disable_channel(
    channel_id: UUID, request: Request,
    membership: Membership = Depends(require_tenant_permission("channels:publish")),
    _: None = Depends(require_csrf), db: Session = Depends(get_db),
) -> ChannelResponse:
    return _toggle(db, request, membership, channel_id, False)


@router.post("/channels/{channel_id}/rotate-key", response_model=ChannelResponse)
def rotate_site_key(
    channel_id: UUID,
    request: Request,
    membership: Membership = Depends(require_tenant_permission("channels:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ChannelResponse:
    """Resets the SDK key: embeds using the old key stop working until the page is updated."""
    channel = _load(db, membership, channel_id, lock=True)
    channel.site_key = _site_key()
    channel.row_version += 1
    _audit(db, membership, "channel.key_rotated", channel)
    db.commit()
    return _response(db, request, channel)


# An assistant that still has channels cannot be deleted (AST-010).
assistants_api.DELETE_BLOCKERS.append(
    lambda db, assistant: "该助理仍被渠道绑定，请先删除相关渠道"
    if db.scalar(select(ChannelInstance.id).where(ChannelInstance.assistant_id == assistant.id, ChannelInstance.tenant_id == assistant.tenant_id).limit(1))
    else None
)
