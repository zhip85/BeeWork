"""Channel experience editor endpoints (T-042, CHN-030–035): read the layers of a type, write the tenant, agent and channel layers.

The platform layer is edited through ``PATCH /platform/channel-types/{code}`` (``default_experience``).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from beework import experience as rules
from beework import experience_service as service
from beework.api.assistants import _audit as audit_assistant
from beework.api.assistants import _load, _load_for_write
from beework.api.channels import _load as load_channel
from beework.api.conventions import ApiError, check_row_version
from beework.auth import require_csrf
from beework.channel_types import TYPE_CODES, get_type
from beework.db import get_db
from beework.models import AuditEvent, Membership, utc_now
from beework.models.assistants import Assistant
from beework.models.channels import ChannelInstance
from beework.rbac import require_tenant_permission
from beework.schemas.experience import ExperienceField, ExperienceView, ExperienceWrite

router = APIRouter()


def _type_or_404(db: Session, code: str):
    kind = get_type(db, code) if code in TYPE_CODES else None
    if kind is None:
        raise ApiError(404, "not_found", "未找到指定渠道类型")
    return kind


def _view(db: Session, tenant_id: UUID, code: str, assistant: Assistant | None, channel: ChannelInstance | None) -> ExperienceView:
    kind = _type_or_404(db, code)
    platform = service.platform_layer(db, code)
    tenant = service.tenant_layer(db, tenant_id, code)
    mine = service.assistant_layer(assistant, code)
    instance = dict(channel.experience or {}) if channel is not None else {}
    values, sources = rules.resolve(
        code, platform=platform, tenant=tenant, assistant=mine, instance=instance,
        greeting=((assistant.draft_config or {}).get("greeting") or "") if assistant else "",
    )
    return ExperienceView(
        channel_type=code, channel_type_name=kind.name,
        fields=[ExperienceField(key=f.key, kind=f.kind, options=list(f.options)) for f in rules.fields_for(code)],
        layers={"platform": platform, "tenant": tenant, "assistant": mine, "instance": instance}, values=values, sources=sources,
        greeting=((assistant.draft_config or {}).get("greeting") or "") if assistant else "",
        row_version=channel.row_version if channel is not None else (assistant.row_version if assistant is not None else None),
    )


def _for_channel(db: Session, membership: Membership, channel_id: UUID) -> tuple[ChannelInstance, Assistant | None]:
    channel = load_channel(db, membership, channel_id)
    assistant = db.get(Assistant, channel.assistant_id)
    return channel, assistant if assistant is not None and assistant.tenant_id == membership.tenant_id and assistant.deleted_at is None else None


@router.get("/experience/{channel_type}", response_model=ExperienceView)
def read_experience(
    channel_type: str,
    assistant_id: UUID | None = Query(default=None, description="Show the agent's layer as well"),
    channel_id: UUID | None = Query(default=None, description="Show the channel's layer (its agent is used)"),
    membership: Membership = Depends(require_tenant_permission("channels:view")),
    db: Session = Depends(get_db),
) -> ExperienceView:
    """The layers of a type for the tenant, optionally narrowed to one agent or one channel."""
    channel = assistant = None
    if channel_id is not None:
        channel, assistant = _for_channel(db, membership, channel_id)
        if channel.type != channel_type:
            raise ApiError(409, "experience.type_mismatch", "该渠道不是这个类型")
    elif assistant_id is not None:
        assistant = _load(db, membership, assistant_id)
    return _view(db, membership.tenant_id, channel_type, assistant, channel)


@router.put("/channel-types/{channel_type}/tenant-experience", response_model=ExperienceView)
def save_tenant_experience(
    channel_type: str,
    payload: ExperienceWrite,
    membership: Membership = Depends(require_tenant_permission("channels:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ExperienceView:
    """The tenant's defaults for a type. Published channels keep their frozen values until they are published again (PLT-101)."""
    _type_or_404(db, channel_type)
    service.save_tenant_layer(db, membership.tenant_id, channel_type, payload.values, membership.user_id)
    db.add(AuditEvent(tenant_id=membership.tenant_id, actor_user_id=membership.user_id, action="channel_experience.tenant_saved", details={"type": channel_type}))
    db.commit()
    return _view(db, membership.tenant_id, channel_type, None, None)


@router.put("/assistants/{assistant_id}/channel-experience/{channel_type}", response_model=ExperienceView)
def save_assistant_experience(
    assistant_id: UUID,
    channel_type: str,
    payload: ExperienceWrite,
    membership: Membership = Depends(require_tenant_permission("channels:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ExperienceView:
    """The agent's channel settings for a type; they ride with the agent's draft and are published with it."""
    _type_or_404(db, channel_type)
    assistant = _load_for_write(db, membership, assistant_id)
    if payload.row_version is not None:
        check_row_version(assistant, payload.row_version, db)
    clean = rules.validate_layer(channel_type, payload.values)
    config = dict(assistant.draft_config or {})
    styles = {**(config.get("channel_experience") or {})}
    if clean:
        styles[channel_type] = clean
    else:
        styles.pop(channel_type, None)
    config["channel_experience"] = styles
    assistant.draft_config = config
    assistant.row_version += 1
    assistant.updated_at = utc_now()
    audit_assistant(db, membership, "assistant.channel_experience_saved", assistant)
    db.commit()
    return _view(db, membership.tenant_id, channel_type, assistant, None)


@router.put("/channels/{channel_id}/experience", response_model=ExperienceView)
def save_channel_experience(
    channel_id: UUID,
    payload: ExperienceWrite,
    membership: Membership = Depends(require_tenant_permission("channels:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ExperienceView:
    """The channel's own overrides (the “体验” action of the list). Visitors see the change after the next publish (CHN-035)."""
    channel = load_channel(db, membership, channel_id, lock=True)
    if payload.row_version is not None:
        check_row_version(channel, payload.row_version, db)
    channel.experience = rules.validate_layer(channel.type, payload.values, allow=("assistant_name",))
    channel.row_version += 1
    channel.updated_at = utc_now()
    db.add(AuditEvent(tenant_id=membership.tenant_id, actor_user_id=membership.user_id, action="channel.experience_saved", details={"channel_id": str(channel.id), "name": channel.name}))
    db.commit()
    assistant = db.get(Assistant, channel.assistant_id)
    return _view(db, membership.tenant_id, channel.type, assistant if assistant is not None and assistant.tenant_id == channel.tenant_id else None, channel)
