from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

CapabilityKind = Literal["skill", "mcp", "api"]
CapabilityStatus = Literal["draft", "available", "disabled"]
AuthType = Literal["none", "bearer", "oauth"]
ApiType = Literal["rest", "graphql"]
ApiAuthType = Literal["none", "api_key", "bearer", "oauth_cc"]


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


class ApiPluginWrite(BaseModel):
    """Create / update an API plugin (T-073). Secrets blank or absent keep the stored value; ``spec_text`` absent keeps the stored spec."""

    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=1000)
    api_type: ApiType = "rest"
    base_url: str = Field(default="", max_length=500)  # GraphQL: the endpoint
    auth_type: ApiAuthType = "none"
    api_key_header: str = Field(default="", max_length=100)
    secret: str | None = Field(default=None, max_length=2000)  # the API key value or the bearer token
    oauth_token_url: str = Field(default="", max_length=500)
    oauth_client_id: str = Field(default="", max_length=200)
    oauth_client_secret: str | None = Field(default=None, max_length=2000)
    oauth_scope: str = Field(default="", max_length=300)
    spec_text: str | None = Field(default=None, max_length=1_100_000)
    spec_filename: str = Field(default="", max_length=200)
    selected_tools: list[str] | None = Field(default=None, max_length=500)  # None keeps the current selection
    row_version: int | None = None  # required on update


class ApiToolEntry(BaseModel):
    name: str
    description: str = ""
    writes: bool = False
    selected: bool = False
    input_schema: dict = Field(default_factory=dict)


class ApiDetail(BaseModel):
    api_type: ApiType
    base_url: str
    host: str
    auth_type: ApiAuthType
    api_key_header: str = ""
    secret_mask: str | None = None
    oauth_token_url: str = ""
    oauth_client_id: str = ""
    oauth_client_secret_mask: str | None = None
    oauth_scope: str = ""
    spec_filename: str = ""
    has_spec: bool = False
    tools: list[ApiToolEntry] = Field(default_factory=list)
    last_test_at: datetime | None = None
    last_test_ok: bool = False
    last_test_tool: str | None = None
    last_test_error: str | None = None


class ApiParseRequest(BaseModel):
    api_type: ApiType = "rest"
    spec_text: str = Field(max_length=1_100_000)


class ApiParseResult(BaseModel):
    ok: bool
    tool_count: int = 0
    tools: list[ApiToolEntry] = Field(default_factory=list)
    error: str | None = None
    line: int | None = None


class ApiTestCallRequest(BaseModel):
    tool: str = Field(max_length=64)
    arguments: dict = Field(default_factory=dict)
    confirmed: bool | None = None  # a write needs True (“二次确认”)


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
    api: ApiDetail | None = None


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


class ApiTestCallResult(BaseModel):
    ok: bool
    status: Literal["ok", "error", "cancelled", "confirmation_required"]
    content: str = ""
    code: str | None = None
    capability: CapabilityResponse | None = None


class OAuthStartResponse(BaseModel):
    authorize_url: str
