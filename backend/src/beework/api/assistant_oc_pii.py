"""The assistant layer of the PII rules on the OC page (T-055, OC-010): the effective rules of an unsaved draft."""

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from beework.api.assistants import _load
from beework.db import get_db
from beework.governance.runtime_policy import assistant_view
from beework.models import Membership
from beework.rbac import require_tenant_permission
from beework.runtime.oc import PiiConfig

router = APIRouter()


class PiiPreviewRequest(BaseModel):
    pii: PiiConfig


class PiiRuleItem(BaseModel):
    key: str
    name: str
    match_type: str
    pattern: str
    stage: str
    action: str
    block_message: str
    enabled: bool
    enforced: bool
    label: str  # platform_inherited | tenant | tenant_modified | assistant | assistant_modified
    restorable: bool
    deletable: bool
    switched_off: bool  # disabled only because PII detection is off


class PiiChain(BaseModel):
    platform_total: int
    platform_enforced: int
    tenant_inherited: int
    tenant_level: int
    assistant_inherited: int
    assistant_level: int
    effective: int


class PiiPreviewResponse(BaseModel):
    chain: PiiChain
    items: list[PiiRuleItem]


@router.post("/assistants/{assistant_id}/oc/pii/preview", response_model=PiiPreviewResponse)
def preview_pii_rules(
    assistant_id: UUID,
    payload: PiiPreviewRequest,
    membership: Membership = Depends(require_tenant_permission("orchestration:view")),
    db: Session = Depends(get_db),
) -> PiiPreviewResponse:
    """What the rules would be with this draft ``oc.pii``; nothing is saved."""
    assistant = _load(db, membership, assistant_id)
    effective, chain = assistant_view(db, assistant.tenant_id, payload.pii)
    return PiiPreviewResponse(
        chain=PiiChain(**chain),
        items=[
            PiiRuleItem(
                key=item.key, name=item.rule.name, match_type=item.rule.match_type, pattern=item.rule.pattern, stage=item.rule.stage,
                action=item.rule.action, block_message=item.rule.block_message, enabled=item.enabled, enforced=item.enforced,
                label=item.label, restorable=item.override_id is not None and item.label == "assistant_modified",
                deletable=item.label == "assistant", switched_off=item.switched_off,
            )
            for item in effective
        ],
    )
