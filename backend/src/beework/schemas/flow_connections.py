from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

FlowKind = Literal["chatflow", "workflow"]
SourceType = Literal["native", "dify"]  # ``native`` only on leftover rows of the retired canvas (T-169)
ConnectionState = Literal["available", "disabled", "invalid"]


class FlowConnectionWrite(BaseModel):
    """Create / update. ``credential`` blank or absent keeps the stored API key on update (it is never echoed back)."""

    kind: FlowKind | None = None  # required on create; the kind of an existing connection cannot change
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=1000)
    source_type: SourceType = "dify"
    base_url: str = Field(default="", max_length=500)
    app_id: str = Field(default="", max_length=200)
    credential: str | None = Field(default=None, max_length=2000)
    # input_mapping: {"<Dify input variable>": "<name of the tool argument the model fills>"}
    input_mapping: dict[str, str] = Field(default_factory=dict, max_length=50)
    # output_mapping: {"field": "<output key to hand back to the model>"}; empty returns every output as JSON.
    output_mapping: dict[str, str] = Field(default_factory=dict, max_length=10)
    streaming: bool = False
    # A multi-turn Chatflow: the next message of the conversation goes straight back to it while it waits (T-163).
    sticky: bool = True
    idle_ttl_minutes: int = Field(default=10, ge=1, le=1440)
    exit_words: list[str] = Field(default_factory=lambda: ["退出", "取消", "结束"], max_length=20)
    row_version: int | None = None  # required on update


class FlowConnectionResponse(BaseModel):
    id: UUID
    kind: FlowKind
    name: str
    description: str
    source_type: SourceType
    base_url: str = ""
    host: str = ""
    app_id: str = ""
    credential_mask: str | None = None
    input_mapping: dict[str, str] = Field(default_factory=dict)
    output_mapping: dict[str, str] = Field(default_factory=dict)
    streaming: bool = False
    app_mode: str | None = None
    parameters: list[dict] = Field(default_factory=list)  # the application's input variables, as read from Dify
    parameters_synced_at: datetime | None = None
    parameters_error: str | None = None
    sticky: bool = True
    idle_ttl_minutes: int = 10
    exit_words: list[str] = Field(default_factory=list)
    status: Literal["enabled", "disabled"]
    state: ConnectionState  # derived: a leftover ``native`` connection is ``invalid``
    state_reason: str | None = None
    last_test_at: datetime | None = None
    last_test_ok: bool | None = None
    last_test_error: str | None = None
    last_test_ms: int | None = None
    references: int = 0
    row_version: int
    created_by: str | None = None
    updated_by: str | None = None
    created_at: datetime
    updated_at: datetime
    can_edit: bool = False
    can_delete: bool = False


class FlowConnectionList(BaseModel):
    items: list[FlowConnectionResponse]
    total: int


class FlowConnectionTestResult(BaseModel):
    ok: bool
    error: str | None = None
    error_category: str | None = None
    duration_ms: int | None = None
    connection: FlowConnectionResponse


class FlowConnectionReference(BaseModel):
    type: str
    id: UUID
    name: str
    status: str
    layers: list[str]


class FlowConnectionReferences(BaseModel):
    total: int
    items: list[FlowConnectionReference]
