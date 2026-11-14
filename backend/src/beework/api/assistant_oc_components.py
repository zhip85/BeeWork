"""OC · components (T-059, OC-072): what each channel bound to the assistant will inject, from its *published snapshot*."""

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework import assistant_oc
from beework.api.assistants import _load
from beework.channel_publish import version_label
from beework.channel_types import get_type
from beework.component_service import build_component_prompt
from beework.db import get_db
from beework.models import Membership
from beework.models.channels import ChannelInstance
from beework.rbac import require_tenant_permission

router = APIRouter()


class InjectedComponent(BaseModel):
    trigger_tag: str
    name: str
    mode: str
    version: str


class ChannelInjection(BaseModel):
    channel_id: UUID
    name: str
    type: str
    type_name: str
    published_version: str  # "—" until published
    published: bool
    components: list[InjectedComponent]
    prompt: str | None  # None: nothing is injected (not published, no components, or orchestration off)


class InjectionPreview(BaseModel):
    enabled: bool
    channels: list[ChannelInjection]


@router.get("/assistants/{assistant_id}/oc/components/preview", response_model=InjectionPreview)
def injection_preview(
    assistant_id: UUID,
    membership: Membership = Depends(require_tenant_permission("orchestration:view")),
    db: Session = Depends(get_db),
) -> InjectionPreview:
    """OC-072: the channels bound to this assistant and the prompt each would inject. Draft edits of the component library change nothing here:
    the frozen definitions of the published snapshot are what runs (CHN-025). The numbers come from the OC draft being edited."""
    assistant = _load(db, membership, assistant_id)
    config = assistant_oc.draft_oc(assistant).components
    channels = db.scalars(select(ChannelInstance).where(ChannelInstance.tenant_id == assistant.tenant_id, ChannelInstance.assistant_id == assistant.id).order_by(ChannelInstance.created_at))
    out: list[ChannelInjection] = []
    for channel in channels:
        snapshot = channel.published_snapshot
        frozen = list((snapshot or {}).get("components", []))
        kind = get_type(db, channel.type)
        usable = [c for c in frozen if c.get("mode") != "off"]
        out.append(ChannelInjection(
            channel_id=channel.id, name=channel.name, type=channel.type, type_name=kind.name if kind else channel.type,
            published_version=version_label(channel.version_seq), published=snapshot is not None,
            components=[InjectedComponent(trigger_tag=c["trigger_tag"], name=c["name_zh"], mode=c["mode"], version=c["version"]) for c in frozen],
            prompt=build_component_prompt(frozen, config, "zh", str((snapshot or {}).get("name") or channel.name)) if config.enabled and usable else None,
        ))
    return InjectionPreview(enabled=config.enabled, channels=out)
