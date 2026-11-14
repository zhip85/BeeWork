"""Reading and writing the layers of the channel experience (T-042). The rules are in ``experience``; this is the database side."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework import experience as rules
from beework.channel_types import get_type
from beework.models import utc_now
from beework.models.assistants import Assistant
from beework.models.channels import ChannelInstance, TenantChannelStyle


def platform_layer(db: Session, channel_type: str) -> dict[str, Any]:
    kind = get_type(db, channel_type)
    return dict(kind.default_experience) if kind else {}


def tenant_layer(db: Session, tenant_id: UUID, channel_type: str) -> dict[str, Any]:
    row = db.scalar(select(TenantChannelStyle).where(TenantChannelStyle.tenant_id == tenant_id, TenantChannelStyle.channel_type == channel_type))
    return dict(row.overrides or {}) if row else {}


def assistant_layer(assistant: Assistant | None, channel_type: str) -> dict[str, Any]:
    return dict(((assistant.draft_config or {}).get("channel_experience") or {}).get(channel_type) or {}) if assistant else {}


def resolve_for(db: Session, channel: ChannelInstance, assistant: Assistant | None) -> tuple[dict[str, Any], dict[str, str]]:
    """What the channel would show if published now, and which layer each value comes from."""
    return rules.resolve(
        channel.type, platform=platform_layer(db, channel.type), tenant=tenant_layer(db, channel.tenant_id, channel.type),
        assistant=assistant_layer(assistant, channel.type), instance=dict(channel.experience or {}),
        greeting=((assistant.draft_config or {}).get("greeting") or "") if assistant else "",
    )


def save_tenant_layer(db: Session, tenant_id: UUID, channel_type: str, values: dict[str, Any], user_id: UUID | None) -> dict[str, Any]:
    clean = rules.validate_layer(channel_type, values)
    row = db.scalar(select(TenantChannelStyle).where(TenantChannelStyle.tenant_id == tenant_id, TenantChannelStyle.channel_type == channel_type))
    if row is None:
        row = TenantChannelStyle(tenant_id=tenant_id, channel_type=channel_type, overrides=clean)
        db.add(row)
    row.overrides, row.updated_by, row.updated_at = clean, user_id, utc_now()
    return clean
