from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from beework.schemas.governance import Action, MatchType
from beework.schemas.versions import ApprovalInfo


class TenantRuleWrite(BaseModel):
    name: str = Field(max_length=200)
    match_type: MatchType = "regex"
    pattern: str = Field(default="", max_length=4000)
    stage: Literal["both", "input", "output"] = "both"
    action: Action = "mask"
    block_message: str = Field(default="", max_length=600)
    enabled: bool = True


class TenantSettingsWrite(BaseModel):
    inherit_platform: bool
    row_version: int = Field(ge=0)


class EffectiveRuleResponse(BaseModel):
    key: UUID  # what to edit / restore / delete
    name: str
    match_type: str
    pattern: str
    stage: str
    action: str
    block_message: str
    enabled: bool
    enforced: bool
    label: str  # platform_inherited | tenant | tenant_modified
    restorable: bool  # a tenant override exists
    deletable: bool  # a rule of the tenant's own
    pending_approval_id: UUID | None = None


class ChainSummary(BaseModel):
    platform_total: int
    platform_enforced: int
    inherited: int
    tenant_level: int
    overrides: int
    total: int
    effective: int


class TenantPiiState(BaseModel):
    tenant_name: str
    chain: ChainSummary
    inherit_platform: bool
    settings_version: int
    items: list[EffectiveRuleResponse]
    approval: ApprovalInfo


class MutationResponse(BaseModel):
    outcome: Literal["applied", "pending"]
    kind: str  # created | override_created | updated | restored | deleted | settings
    approval_id: UUID | None = None
