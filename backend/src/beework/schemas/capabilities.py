from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

CapabilityKind = Literal["skill", "mcp", "api"]
CapabilityStatus = Literal["draft", "available", "disabled"]
AuthType = Literal["none", "bearer", "oauth"]


class McpWriteRequest(BaseModel):
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=1000)
    transport: Literal["streamable_http"] = "streamable_http"  # stdio only on private deployments (T-124)
    url: str = Field(default="", max_length=500)
    auth_type: AuthType = "none"
    token: str | None = Field(default=None, max_length=2000)  # blank / absent keeps the stored token
    oauth_authorize_url: str = Field(default="", max_length=500)
    oauth_token_url: str = Field(default="", max_length=500)
    oauth_client_id: str = Field(default="", max_length=200)
    oauth_client_secret: str | None = Field(default=None, max_length=2000)  # blank / absent keeps the stored secret
    oauth_scopes: str = Field(default="", max_length=300)
    selected_tools: list[str] | None = Field(default=None, max_length=500)  # None keeps the current selection
    row_version: int | None = None  # required on update


class McpToolEntry(BaseModel):
    name: str
    description: str = ""
    read_only: bool = False
    selected: bool = False


class McpDetail(BaseModel):
    transport: str
    url: str
    host: str
    auth_type: AuthType
    token_mask: str | None = None
    oauth_authorize_url: str = ""
    oauth_token_url: str = ""
    oauth_client_id: str = ""
    oauth_client_secret_mask: str | None = None
    oauth_scopes: str = ""
    oauth_authorized: bool = False
    oauth_expires_at: datetime | None = None
    tools: list[McpToolEntry] = Field(default_factory=list)
    last_test_at: datetime | None = None
    last_test_ok: bool = False
    last_test_error: str | None = None
    health: Literal["unknown", "ok", "error"] = "unknown"


class SkillCheck(BaseModel):
    key: Literal["structure", "executables", "secrets", "dependencies"]
    status: Literal["pending", "running", "passed", "warning", "failed"]
    issues: list[dict] = Field(default_factory=list)


class SkillDetail(BaseModel):
    builtin: bool
    platform_active: bool = True
    filename: str = ""
    size_bytes: int = 0
    files: list[dict] = Field(default_factory=list)
    has_scripts: bool = False
    security_status: Literal["pending", "running", "passed", "failed"]
    checks: list[SkillCheck] = Field(default_factory=list)
    checked_at: datetime | None = None
    enable_warning: str | None = None


class SkillUpdateRequest(BaseModel):
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=1000)
    row_version: int | None = None


class PlatformSkillResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    name: str
    description: str
    content: str
    status: Literal["active", "disabled"]
    updated_at: datetime
    updated_by: str | None = None


class PlatformSkillUpdate(BaseModel):
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=1000)
    content: str = Field(max_length=20000)
    status: Literal["active", "disabled"] = "active"


class CapabilityResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: CapabilityKind
    name: str
    description: str
    status: CapabilityStatus
    meta: str = ""  # “{host} · MCP”
    pending_approval: bool = False
    approval_id: UUID | None = None
    approval_warning: str | None = None  # why the enable request could not be filed (e.g. no other approver)
    updated_at: datetime
    updated_by: str | None = None
    row_version: int
    can_edit: bool = False
    can_publish: bool = False
    can_delete: bool = False
    mcp: McpDetail | None = None
    skill: SkillDetail | None = None


class CapabilityCounts(BaseModel):
    skill: int
    mcp: int
    api: int


class CapabilityReferences(BaseModel):
    total: int
    items: list[dict] = Field(default_factory=list)


class TestResult(BaseModel):
    ok: bool
    error: str | None = None
    tool_count: int = 0
    capability: CapabilityResponse | None = None


class OAuthStartResponse(BaseModel):
    authorize_url: str
