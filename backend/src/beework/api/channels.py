import secrets
import string
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from beework.api import assistants as assistants_api
from beework.api.assistants import _load as load_assistant
from beework.api.conventions import ApiError, check_row_version, require_platform_admin
from beework.approvals import pending_request, submit_change
from beework.auth import require_csrf
from beework.channel_adapters import adapter_for, public_base_url
from beework.channel_publish import (
    ACTION,
    assistant_of,
    build_snapshot,
    effective_experience,
    has_unpublished_changes,
    version_label,
)
from beework.channel_types import TYPE_CODES, get_type, list_types, plan_includes
from beework.config import settings
from beework.db import get_db
from beework.entitlements import environments_for
from beework.model_secrets import decrypt_model_api_key, encrypt_model_api_key
from beework.models import AuditEvent, Membership, Tenant, User, utc_now
from beework.models.channels import ChannelInstance, ChannelSnapshot, ChannelType
from beework.rbac import require_tenant_permission
from beework.schemas.channels import (
    ChannelList,
    ChannelResponse,
    ChannelTypeList,
    ChannelTypeResponse,
    ChannelTypeWrite,
    ChannelWrite,
    ExperienceOverrides,
    PendingChannelApproval,
    PublishChannelRequest,
    PublishPreview,
)

router = APIRouter()

ALPHABET = string.ascii_lowercase + string.digits


def _encrypt(new: dict[str, str], old: dict[str, str]) -> dict[str, str]:
    """Blank or missing keeps the stored secret; a value replaces it. Secrets are Fernet-encrypted and never returned."""
    merged = dict(old)
    for key, value in new.items():
        if value:
            merged[key] = encrypt_model_api_key(value)
    return merged


def _site_key() -> str:
    return "bw_site_" + "".join(secrets.choice(ALPHABET) for _ in range(20))


def embed_code(site_key: str, request: Request) -> str:
    base = (settings.public_api_url or str(request.base_url)).rstrip("/")
    return f'<script src="{base}/v1/widget.js" data-site-key="{site_key}" async></script>'


def _mask(value: str) -> str:
    return f"••••{value[-4:]}" if len(value) > 4 else "••••"


def _secret_masks(channel: ChannelInstance) -> dict[str, str]:
    masks: dict[str, str] = {}
    for key, token in (channel.secret_config or {}).items():
        try:
            masks[key] = _mask(decrypt_model_api_key(token))
        except Exception:  # noqa: BLE001 — an unreadable value is shown as set, never as its text
            masks[key] = "••••"
    return masks


def _defaults_of(db: Session, code: str) -> dict[str, Any]:
    view = get_type(db, code)
    return view.default_experience if view else {}


def _response(db: Session, request: Request, channel: ChannelInstance) -> ChannelResponse:
    assistant = assistant_of(db, channel)
    pending = pending_request(db, channel.tenant_id, "channel", channel.id)
    kind = get_type(db, channel.type)
    base = public_base_url(str(request.base_url))
    published = bool(channel.published_snapshot)
    return ChannelResponse(
        id=channel.id, type=channel.type, type_name=kind.name if kind else channel.type, type_enabled=bool(kind and kind.enabled),
        config=dict(channel.config or {}), secret_masks=_secret_masks(channel), health_error=channel.health_error,
        health_checked_at=channel.health_checked_at, name=channel.name, assistant_id=channel.assistant_id,
        assistant_name=assistant.name if assistant else None, environment=channel.environment,
        allowed_origins=list(channel.allowed_origins or []),
        experience=ExperienceOverrides.model_validate(channel.experience or {}),
        effective_experience=effective_experience(channel.experience or {}, assistant, _defaults_of(db, channel.type)) if assistant else {},
        status=channel.status, site_key=channel.site_key, published_version=version_label(channel.version_seq),
        published_at=channel.published_at, has_unpublished_changes=has_unpublished_changes(channel),
        pending_approval_id=pending.id if pending else None, row_version=channel.row_version,
        created_at=channel.created_at, updated_at=channel.updated_at,
        embed_code=embed_code(channel.site_key, request) if published and channel.type == "website" else None,
        integration=adapter_for(channel.type).integration_snippet(channel, base) if published else None,
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


def _validate(db: Session, membership: Membership, payload: ChannelWrite, *, exclude: UUID | None = None, existing: ChannelInstance | None = None) -> None:
    kind = get_type(db, payload.type)
    if kind is None:
        raise ApiError(422, "channel.type_unknown", f"不支持的渠道类型：{payload.type}")
    if existing is None or existing.type != payload.type:  # a type switch counts as creating one of the new type
        if not kind.enabled:
            raise ApiError(403, "channel.type_disabled", f"平台已停用“{kind.name}”渠道类型，暂不能新建")
        if not plan_includes(db, membership.tenant_id, payload.type):
            raise ApiError(403, "channel.type_not_in_plan", f"当前套餐未包含“{kind.name}”渠道，请升级套餐")
    if existing is not None and existing.published_snapshot is not None and existing.type != payload.type:
        raise ApiError(409, "channel.type_locked", "已发布的渠道不能修改类型，请新建渠道")
    params = {p["key"]: p for p in kind.connection_params}
    for key in payload.config:
        if key not in params or params[key]["kind"] not in ("text",):
            raise ApiError(422, "channel.config_invalid", f"“{kind.name}”渠道没有连接参数 {key}")
    for key in payload.secrets:
        if key not in params or params[key]["kind"] != "secret":
            raise ApiError(422, "channel.config_invalid", f"“{kind.name}”渠道没有密钥参数 {key}")
    if payload.allowed_origins and "allowed_origins" not in params:
        raise ApiError(422, "channel.config_invalid", f"“{kind.name}”渠道不使用允许域名")
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


@router.get("/channel-types", response_model=ChannelTypeList)
def list_channel_types(
    membership: Membership = Depends(require_tenant_permission("channels:view")),
    db: Session = Depends(get_db),
) -> ChannelTypeList:
    """The five types with whether this tenant can create one now (CHN-050/051)."""
    items = []
    for kind in list_types(db):
        in_plan = plan_includes(db, membership.tenant_id, kind.code)
        reason = "disabled" if not kind.enabled else (None if in_plan else "plan")
        items.append(ChannelTypeResponse(
            code=kind.code, name=kind.name, version=kind.version, enabled=kind.enabled, available=reason is None,
            unavailable_reason=reason, connection_params=kind.connection_params, capabilities=kind.capabilities,
            default_experience=kind.default_experience,
        ))
    return ChannelTypeList(items=items)


def _type_response(kind, *, available: bool = True, reason: str | None = None) -> ChannelTypeResponse:
    return ChannelTypeResponse(
        code=kind.code, name=kind.name, version=kind.version, enabled=kind.enabled, available=available, unavailable_reason=reason,
        connection_params=kind.connection_params, capabilities=kind.capabilities, default_experience=kind.default_experience,
    )


@router.get("/platform/channel-types", response_model=ChannelTypeList)
def platform_channel_types(_: User = Depends(require_platform_admin), db: Session = Depends(get_db)) -> ChannelTypeList:
    return ChannelTypeList(items=[_type_response(kind, available=kind.enabled, reason=None if kind.enabled else "disabled") for kind in list_types(db)])


@router.patch("/platform/channel-types/{code}", response_model=ChannelTypeResponse)
def update_channel_type(
    code: str,
    payload: ChannelTypeWrite,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ChannelTypeResponse:
    """Enable / disable a type, its version and render-capability tags, its platform default experience (PLT-101/102).
    Disabling only stops new channels of that type; existing instances and their published snapshots are untouched."""
    if code not in TYPE_CODES:
        raise ApiError(404, "not_found", "未找到指定渠道类型")
    kind = get_type(db, code)
    row = db.get(ChannelType, code)
    if row is None:
        row = ChannelType(code=code, name=kind.name, version=kind.version, enabled=kind.enabled, connection_params=[], capabilities=kind.capabilities, default_experience={})
        db.add(row)
    changed = payload.model_dump(exclude_unset=True, exclude_none=True)
    if "enabled" in changed:
        row.enabled = changed["enabled"]
    if "version" in changed:
        row.version = changed["version"]
    if "capabilities" in changed:
        row.capabilities = [c.strip() for c in changed["capabilities"] if c.strip()]
    if "default_experience" in changed:
        row.default_experience = dict(changed["default_experience"])
    row.updated_at = utc_now()
    db.add(AuditEvent(tenant_id=None, actor_user_id=user.id, action="channel_type.updated", details={"type": code, "fields": ",".join(sorted(changed))}))
    db.commit()
    return _type_response(get_type(db, code))


@router.get("/channels", response_model=ChannelList)
def list_channels(
    request: Request,
    assistant_id: UUID | None = None,
    type: str | None = Query(default=None, max_length=16),
    q: str | None = Query(default=None, max_length=50),
    membership: Membership = Depends(require_tenant_permission("channels:view")),
    db: Session = Depends(get_db),
) -> ChannelList:
    statement = select(ChannelInstance).where(ChannelInstance.tenant_id == membership.tenant_id)
    if assistant_id:
        statement = statement.where(ChannelInstance.assistant_id == assistant_id)
    if type:
        statement = statement.where(ChannelInstance.type == type)
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
        tenant_id=membership.tenant_id, type=payload.type, name=payload.name, assistant_id=payload.assistant_id,
        environment=payload.environment, allowed_origins=payload.allowed_origins,
        experience=payload.experience.model_dump(), site_key=_site_key(), created_by=str(membership.user_id),
        config=payload.config, secret_config=_encrypt(payload.secrets, {}),
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
    _validate(db, membership, payload, exclude=channel.id, existing=channel)
    if channel.type != payload.type:  # a draft switched type: what belonged to the old type goes (CHN-010)
        channel.type, channel.secret_config, channel.experience = payload.type, {}, {}
    channel.name, channel.assistant_id, channel.environment = payload.name, payload.assistant_id, payload.environment
    channel.allowed_origins = payload.allowed_origins
    channel.config = payload.config
    channel.secret_config = _encrypt(payload.secrets, channel.secret_config or {})
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
    db.execute(delete(ChannelSnapshot).where(ChannelSnapshot.channel_id == channel.id, ChannelSnapshot.tenant_id == channel.tenant_id))
    db.delete(channel)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _diff(channel: ChannelInstance, snapshot: dict[str, Any], overrides: dict[str, Any]) -> list[dict[str, Any]]:
    live = channel.published_snapshot or {}
    rows = [("name", "名称"), ("environment", "环境"), ("allowed_origins", "允许域名"), ("type", "渠道类型")]
    changes = [
        {"section": "channel", "field": label, "from": live.get(key), "to": snapshot[key]}
        for key, label in rows
        if live.get(key, "website" if key == "type" else None) != snapshot[key]
    ]
    for key, value in snapshot.get("config", {}).items():
        before = (live.get("config") or {}).get(key)
        if before != value:
            changes.append({"section": "channel", "field": key, "from": before, "to": value})
    for key, label in (("primary_color", "主色"), ("assistant_name", "智能体名称"), ("welcome", "欢迎语")):
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
        raise ApiError(409, "channel.publish_blocked", "绑定的智能体已被删除，请改绑其他智能体")
    problems = _connection_problems(channel)
    if problems:
        raise ApiError(422, "channel.incomplete", "；".join(problems))
    if assistant.status == "disabled":
        raise ApiError(409, "channel.publish_blocked", "绑定的智能体已停用")
    if channel.environment == "production" and assistant.live_version_id is None:
        raise ApiError(409, "channel.publish_blocked", "生产环境的渠道需要先发布智能体（开发/预发环境使用最新草稿）")
    snapshot = build_snapshot(channel, assistant, _defaults_of(db, channel.type))
    overrides = dict(channel.experience or {})
    result = submit_change(
        db, tenant_id=membership.tenant_id, event="channel", action=ACTION, object_type="channel", object_id=channel.id,
        title=f"发布渠道 {channel.name} {version_label(channel.version_seq + 1)}",
        payload={"snapshot": snapshot, "overrides": overrides}, change_diff=_diff(channel, snapshot, overrides),
        description=f"{get_type(db, channel.type).name}渠道，绑定智能体“{assistant.name}”", requester_id=membership.user_id,
        organization_id=assistant.organization_id, owner_user_id=assistant.owner_id, replace_pending=payload.replace_pending,
    )
    db.commit()
    if result.approval is not None:
        response.status_code = status.HTTP_202_ACCEPTED
        return PendingChannelApproval(approval_id=result.approval.id)
    db.refresh(channel)
    return _response(db, request, channel)


def _connection_problems(channel: ChannelInstance) -> list[str]:
    """What is missing before this channel can be published (CHN-024: connection complete)."""
    secrets = {key: "set" for key in (channel.secret_config or {})}
    config = {**(channel.config or {}), "allowed_origins": list(channel.allowed_origins or [])}
    return adapter_for(channel.type).validate_connection(config, secrets)


@router.get("/channels/{channel_id}/publish-preview", response_model=PublishPreview)
def publish_preview(
    channel_id: UUID,
    membership: Membership = Depends(require_tenant_permission("channels:publish")),
    db: Session = Depends(get_db),
) -> PublishPreview:
    """The publish dialog (CHN-020–023): what changes against the live version and what blocks publishing now.

    The component list, the injected prompt and the trial run belong to the component library (T-043) and the OC component
    orchestration (T-059); until they exist those parts are empty and say so."""
    channel = _load(db, membership, channel_id)
    assistant = assistant_of(db, channel)
    problems = _connection_problems(channel)
    changes: list[dict[str, Any]] = []
    if assistant is None:
        problems.append("绑定的智能体已被删除，请改绑其他智能体")
    else:
        if assistant.status == "disabled":
            problems.append("绑定的智能体已停用")
        if channel.environment == "production" and assistant.live_version_id is None:
            problems.append("生产环境的渠道需要先发布智能体")
        snapshot = build_snapshot(channel, assistant, _defaults_of(db, channel.type))
        changes = _diff(channel, snapshot, dict(channel.experience or {}))
    return PublishPreview(
        version_from=version_label(channel.version_seq), version_to=version_label(channel.version_seq + 1),
        first_publish=channel.published_snapshot is None, changes=changes, problems=problems,
        components=[], components_note="未选择组件，智能体将以纯文本回复。（组件库在 T-043 提供）",
        injection_prompt=None, trial=[], trial_note="暂无可试跑组件。",
    )


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
    lambda db, assistant: "该智能体仍被渠道绑定，请先删除相关渠道"
    if db.scalar(select(ChannelInstance.id).where(ChannelInstance.assistant_id == assistant.id, ChannelInstance.tenant_id == assistant.tenant_id).limit(1))
    else None
)
