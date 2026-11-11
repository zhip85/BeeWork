from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from beework.schemas.plans import PortalGrantResponse

SeatState = Literal["assigned", "requested", "unassigned"]


class SeatSummary(BaseModel):
    grant: PortalGrantResponse  # what the platform granted; read-only here
    assigned: int
    remaining: int | None  # assignable seats left (grace included); None = no limit
    requested: int
    idle: int
    over_contract: bool  # assigned beyond the contract number (inside the grace allowance)
    console_seats_used: int
    console_seats_limit: int | None


class SeatMember(BaseModel):
    user_id: UUID
    display_name: str
    email: str
    organization_id: UUID
    organization_name: str
    role: str
    state: SeatState
    source: Literal["rule", "manual", "auto"] | None
    reason: str | None
    last_login_at: datetime | None
    assigned_at: datetime | None
    idle: bool


class SeatMemberPage(BaseModel):
    items: list[SeatMember]
    total: int
    page: int
    page_size: int


class SeatBatchRequest(BaseModel):
    user_ids: list[UUID] = Field(min_length=1, max_length=500)


class SeatChangeResponse(BaseModel):
    changed: int
    notice: str | None = None  # e.g. “当前已超出合同 N 席位，处于宽限期”
    summary: SeatSummary


class SeatRuleOrganization(BaseModel):
    organization_id: UUID
    name: str
    parent_id: UUID | None
    member_count: int  # active members of the department and its sub-departments
    enabled: bool


class SeatRulesResponse(BaseModel):
    organizations: list[SeatRuleOrganization]


class SeatRulesUpdate(BaseModel):
    organization_ids: list[UUID] = Field(max_length=500)


class SeatPolicyBody(BaseModel):
    auto_assign: bool = False
    idle_days: Literal[30, 60, 90] | None = None


class SeatReclaimResponse(BaseModel):
    reclaimed: int
    summary: SeatSummary
