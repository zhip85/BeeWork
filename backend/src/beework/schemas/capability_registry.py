from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from beework.capability_registry import MAX_EXAMPLES, RETRY_ON

EntryKind = Literal["mcp_tool", "api_tool", "skill", "dify_workflow", "dify_chatflow", "agent", "kb"]
EntryStatus = Literal["available", "disabled", "invalid"]


class RetryPolicy(BaseModel):
    max_attempts: int = Field(default=1, ge=1, le=5)
    backoff_ms: int = Field(default=0, ge=0, le=60_000)
    retry_on: list[Literal["network", "timeout", "5xx", "429"]] = Field(default_factory=list)

    @field_validator("retry_on")
    @classmethod
    def unique(cls, value: list[str]) -> list[str]:
        return [item for item in RETRY_ON if item in value]


class EntryUpdate(BaseModel):
    """What only the registry holds. The source owns name, description, schema and the write flag."""

    intent_description: str = Field(default="", max_length=500)
    intent_examples: list[str] = Field(default_factory=list, max_length=MAX_EXAMPLES)
    direct_enabled: bool
    direct_output: bool = False
    timeout_ms: int | None = Field(default=None, ge=1_000, le=300_000)  # empty = the default
    retry: RetryPolicy | None = None
    row_version: int

    @field_validator("intent_examples")
    @classmethod
    def clean(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        if any(len(item) > 200 for item in cleaned):
            raise ValueError("每条示例不超过 200 个字符")
        return cleaned


class EntryOwner(BaseModel):
    """Where the owner of the entry is managed: the capability centre, or the sub-agent's / knowledge base's own page."""

    page: Literal["capabilities", "assistants", "knowledge"]
    id: UUID
    name: str


class EntryResponse(BaseModel):
    id: UUID
    kind: EntryKind
    identifier: str
    name: str
    description: str
    intent_description: str
    intent_examples: list[str]
    writes: bool
    direct_enabled: bool
    direct_output: bool
    timeout_ms: int | None
    retry: dict[str, Any] | None
    policy: dict[str, Any]  # resolved: the entry's own over the platform default
    status: EntryStatus
    owner: EntryOwner
    meta: dict[str, Any]
    row_version: int
    updated_at: datetime
    input_schema: dict[str, Any] | None = None  # only on the single-entry read
    unstable: bool = False  # its circuit is open or being probed (T-165): calls are being refused


class EntryList(BaseModel):
    items: list[EntryResponse]
    total: int


class RecallEntry(BaseModel):
    entry: EntryResponse
    score: float


class RecallResponse(BaseModel):
    items: list[RecallEntry]
    scope: int  # how many entries the assistant can see (available ones)


class ToolStat(BaseModel):
    """What happened to the calls of one tool in a period (T-165): from the metadata of ``tool_invocations``."""

    tool: str
    entry_id: UUID | None = None
    name: str
    calls: int
    ok: int
    success_rate: float  # of the calls that ran (refused calls are counted separately)
    avg_ms: int  # of the successful calls
    retries: int  # extra attempts beyond the first
    circuit_opened: int
    refused: int  # calls refused because the circuit was open


class ToolStatList(BaseModel):
    days: int
    items: list[ToolStat]
