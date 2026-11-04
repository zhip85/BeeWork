"""Website channel rules shared by the console API, the publish action and the widget (T-040).

RUL-003: visitors only ever see ``published_snapshot``; edits change the draft columns and nothing live until
the channel is published again (first publish goes through the “接入新的外部渠道” approval policy).
"""

import re
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import select

from beework.approvals import ApplyContext, approval_action
from beework.errors import ApiError
from beework.models.assistants import Assistant
from beework.models.channels import ChannelInstance
from beework.notifications import notify

ACTION = "channel.publish"
DEFAULT_COLOR = "#6558d9"
DEFAULT_WELCOME = "您好，请问有什么可以帮您？"
MAX_ORIGINS = 20
COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})


def normalize_origin(value: str) -> str:
    """An allowed origin is ``scheme://host[:port]`` exactly as a browser sends it; https only, except local hosts."""
    parts = urlsplit(value.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host or parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
        raise ValueError(f"“{value}”不是有效的网站地址，应形如 https://www.example.com")
    if parts.scheme == "http" and host not in LOCAL_HOSTS:
        raise ValueError(f"“{value}”必须使用 https（仅 localhost 可以用 http）")
    host_part = f"[{host}]" if ":" in host else host
    port = f":{parts.port}" if parts.port and (parts.scheme, parts.port) not in (("http", 80), ("https", 443)) else ""
    return f"{parts.scheme}://{host_part}{port}"


def normalize_origins(values: list[str]) -> list[str]:
    if len(values) > MAX_ORIGINS:
        raise ValueError(f"最多允许 {MAX_ORIGINS} 个域名")
    return list(dict.fromkeys(normalize_origin(v) for v in values))


def effective_experience(experience: dict[str, Any], assistant: Assistant) -> dict[str, str]:
    """Inheritance for the website type: channel overrides → assistant (name, greeting) → platform defaults."""
    greeting = ((assistant.draft_config or {}).get("greeting") or "").strip()
    return {
        "primary_color": experience.get("primary_color") or DEFAULT_COLOR,
        "assistant_name": experience.get("assistant_name") or assistant.name,
        "welcome": experience.get("welcome") or greeting or DEFAULT_WELCOME,
    }


def build_snapshot(channel: ChannelInstance, assistant: Assistant) -> dict[str, Any]:
    return {
        "channel_id": str(channel.id),
        "name": channel.name,
        "assistant_id": str(assistant.id),
        "environment": channel.environment,
        "allowed_origins": list(channel.allowed_origins or []),
        "experience": effective_experience(channel.experience or {}, assistant),
    }


def _published_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": snapshot.get("name"), "assistant_id": snapshot.get("assistant_id"), "environment": snapshot.get("environment"),
        "allowed_origins": snapshot.get("allowed_origins"), "overrides": snapshot.get("overrides", {}),
    }


def has_unpublished_changes(channel: ChannelInstance) -> bool:
    """CHN-002: the draft differs from what is live (the list shows “有未发布修改”)."""
    if channel.published_snapshot is None:
        return False
    draft = {
        "name": channel.name, "assistant_id": str(channel.assistant_id), "environment": channel.environment,
        "allowed_origins": list(channel.allowed_origins or []), "overrides": dict(channel.experience or {}),
    }
    return draft != _published_view(channel.published_snapshot)


def version_label(seq: int) -> str:
    return f"v1.{seq - 1}" if seq else "—"  # v1.0 for the first publish, like assistant versions


@approval_action(ACTION)
def apply_publish(db, context: ApplyContext) -> None:
    """Freeze the snapshot that was submitted (and, when approval was needed, reviewed) and switch the channel on."""
    channel = db.scalar(
        select(ChannelInstance)
        .where(ChannelInstance.id == context.object_id, ChannelInstance.tenant_id == context.tenant_id)
        .with_for_update()
    )
    if channel is None:
        raise ApiError(409, "channel.publish_blocked", "渠道已被删除，无法发布")
    assistant = db.scalar(
        select(Assistant).where(
            Assistant.id == channel.assistant_id, Assistant.tenant_id == channel.tenant_id, Assistant.deleted_at.is_(None)
        )
    )
    if assistant is None:
        raise ApiError(409, "channel.publish_blocked", "绑定的助理已被删除，无法发布")
    if assistant.status == "disabled":
        raise ApiError(409, "channel.publish_blocked", "绑定的助理已停用，请启用后重试")
    if channel.environment == "production" and assistant.live_version_id is None:
        raise ApiError(409, "channel.publish_blocked", "生产环境的渠道需要先发布助理")
    snapshot = dict(context.payload["snapshot"])
    snapshot["overrides"] = dict(context.payload.get("overrides") or {})
    channel.published_snapshot = snapshot
    channel.version_seq += 1
    channel.published_at = _now()
    channel.status = "active"
    channel.row_version += 1
    from beework.models import AuditEvent

    db.add(
        AuditEvent(
            tenant_id=channel.tenant_id, actor_user_id=context.requester_id, action="channel.published",
            details={"channel_id": str(channel.id), "name": channel.name, "version": version_label(channel.version_seq)},
        )
    )
    if context.requester_id is not None:
        notify(
            db, tenant_id=channel.tenant_id, recipient_ids=[context.requester_id], type="channel.published",
            title=f"渠道“{channel.name}”已发布 {version_label(channel.version_seq)}", link="/channels",
            params={"name": channel.name, "version": version_label(channel.version_seq)},
        )


def _now():
    from beework.models import utc_now

    return utc_now()


def assistant_of(db, channel: ChannelInstance) -> Assistant | None:
    return db.scalar(
        select(Assistant).where(
            Assistant.id == channel.assistant_id, Assistant.tenant_id == channel.tenant_id, Assistant.deleted_at.is_(None)
        )
    )


def parse_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except (ValueError, TypeError):
        return None
