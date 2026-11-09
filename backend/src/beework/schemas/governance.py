from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

MatchType = Literal["regex", "keyword", "classifier", "llm_judge"]
Action = Literal["mask", "block", "flag"]


class PiiRuleWrite(BaseModel):
    name: str = Field(max_length=200)
    match_type: MatchType = "regex"
    pattern: str = Field(default="", max_length=4000)
    stage: Literal["both", "input", "output"] = "both"
    action: Action = "mask"
    block_message: str = Field(default="", max_length=600)
    enforced: bool = False
    enabled: bool = True
    row_version: int | None = None
    reason: str = Field(default="", max_length=500)  # shown to the reviewer when the change needs one


class PiiRuleResponse(BaseModel):
    id: UUID
    name: str
    match_type: str
    pattern: str
    stage: str
    action: str
    block_message: str
    enforced: bool
    enabled: bool
    row_version: int
    updated_by: str | None
    updated_at: datetime
    pending_request_id: UUID | None = None


class PiiRuleListResponse(BaseModel):
    items: list[PiiRuleResponse]
    total: int
    enforced: int
    enabled: int


class RulesetRef(BaseModel):
    kind: Literal["all", "enforced", "rules"] = "all"
    rule_ids: list[UUID] = Field(default_factory=list)


class BaselineRuleWrite(BaseModel):
    name: str = Field(max_length=200)
    category: Literal["pii", "prompt_attack", "content_safety", "data_retention"] = "pii"
    stage: Literal["both", "input", "output", "data"] = "both"
    level: Literal["enforced", "recommended"] = "recommended"
    scope: Literal["all", "enterprise", "region_cn", "industry_finance"] = "all"
    status: Literal["enabled", "draft"] = "draft"
    action: Action = "flag"
    match_type: MatchType = "keyword"
    pattern: str = Field(default="", max_length=4000)
    block_message: str = Field(default="", max_length=600)
    ruleset: RulesetRef | None = None
    row_version: int | None = None
    reason: str = Field(default="", max_length=500)


class BaselineRuleResponse(BaseModel):
    id: UUID
    name: str
    category: str
    stage: str
    level: str
    scope: str
    status: str
    action: str
    match_type: str
    pattern: str
    block_message: str
    ruleset: dict[str, Any] | None
    row_version: int
    updated_by: str | None
    updated_at: datetime
    pending_request_id: UUID | None = None


class BaselineRuleListResponse(BaseModel):
    items: list[BaselineRuleResponse]
    total: int
    counts: dict[str, int]  # all / enabled / draft


class ChangeRequestResponse(BaseModel):
    id: UUID
    kind: str
    operation: str
    object_id: UUID | None
    object_name: str
    summary: str
    reason: str
    status: str
    requested_by_id: UUID
    requested_by_name: str
    reviewed_by_name: str | None
    review_comment: str | None
    created_at: datetime
    expires_at: datetime
    reviewed_at: datetime | None


class ReviewRequest(BaseModel):
    comment: str = Field(default="", max_length=500)
