"""Agent / Chatflow / Workflow schemas (PRD 3.3, 4.5). Configs are versioned DSLs (``dsl_version``)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

AgentKind = Literal["agent", "chatflow", "workflow"]
AgentStatus = Literal["draft", "available", "disabled"]
AGENT_KINDS = ("agent", "chatflow", "workflow")
ICONS = ("brain", "assistant", "creative", "chat", "flow", "team", "global", "governance")
DEFAULT_ICONS = {"agent": "brain", "chatflow": "chat", "workflow": "flow"}
TEAM_MODES = ("router", "plan_execute", "group_chat", "sequential", "parallel")
NODE_TYPES = (
    "agent", "classify", "extract", "copilot", "review", "connector", "function", "variable", "if", "loop", "note",
)
FIELD_TYPES = ("string", "number", "boolean", "object", "array")
TIMEZONES = ("Asia/Shanghai", "Asia/Singapore", "Asia/Bangkok", "UTC")

Icon = Literal["brain", "assistant", "creative", "chat", "flow", "team", "global", "governance"]


class AgentResources(BaseModel):
    """References to what an Agent can use. ``agents`` also holds Workflows / Chatflows / teams (“连接的智能体”)."""

    channels: list[UUID] = Field(default_factory=list, max_length=50)
    skills: list[UUID] = Field(default_factory=list, max_length=50)
    tools: list[UUID] = Field(default_factory=list, max_length=100)
    knowledge: list[UUID] = Field(default_factory=list, max_length=50)
    agents: list[UUID] = Field(default_factory=list, max_length=50)


class TeamSettings(BaseModel):
    mode: Literal["router", "plan_execute", "group_chat", "sequential", "parallel"] = "router"
    lead_agent_id: UUID | None = None


class AgentConfigV1(BaseModel):
    """Configuration of an Agent (AGT-030/031). Empty values are allowed in drafts; the checker rejects them
    before the object can be made available."""

    dsl_version: Literal[1] = 1
    instructions: str = Field(default="", max_length=8000)
    purpose: str = Field(default="", max_length=200)
    model_id: UUID | None = None
    resources: AgentResources = Field(default_factory=AgentResources)
    memory_enabled: bool = False
    team: TeamSettings | None = None


class FlowNode(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    type: Literal[
        "agent", "classify", "extract", "copilot", "review", "connector", "function", "variable", "if", "loop", "note"
    ]
    name: str = Field(default="", max_length=60)
    instruction: str = Field(default="", max_length=2000)
    target_id: UUID | None = None
    on_true: str | None = Field(default=None, max_length=40)
    on_false: str | None = Field(default=None, max_length=40)
    loop_target: str | None = Field(default=None, max_length=40)
    max_iterations: int = Field(default=3, ge=1, le=100)
    order: int = 0


class ChatSettings(BaseModel):
    opening: str = Field(default="", max_length=500)
    suggestions: list[str] = Field(default_factory=list, max_length=4)
    memory_scope: Literal["thread", "user", "none"] = "thread"
    history_turns: int = Field(default=10, ge=-1000, le=1000)  # range 1–50 is a configuration check, not a save error
    checkpoint: bool = True
    interrupt: bool = True
    max_clarify: Literal[1, 2, 3] = 2
    fallback: str = Field(default="", max_length=500)
    handoff: bool = True


class WorkflowField(BaseModel):
    name: str = Field(default="", max_length=60)
    type: Literal["string", "number", "boolean", "object", "array"] = "string"
    required: bool = False
    description: str = Field(default="", max_length=200)
    default_value: str | None = Field(default=None, max_length=500)


class ApiTrigger(BaseModel):
    enabled: bool = True


class ScheduleTrigger(BaseModel):
    enabled: bool = False
    cron: str = Field(default="0 9 * * 1-5", max_length=100)
    timezone: Literal["Asia/Shanghai", "Asia/Singapore", "Asia/Bangkok", "UTC"] = "Asia/Shanghai"


class ToolTrigger(BaseModel):
    enabled: bool = True
    name: str = Field(default="", max_length=60)
    when: str = Field(default="", max_length=500)


class WorkflowTriggers(BaseModel):
    api: ApiTrigger = Field(default_factory=ApiTrigger)
    schedule: ScheduleTrigger = Field(default_factory=ScheduleTrigger)
    tool: ToolTrigger = Field(default_factory=ToolTrigger)


class FlowConfigV1(BaseModel):
    """Chatflow / Workflow configuration (3.3.5–3.3.10). Node-level details are completed by T-062."""

    dsl_version: Literal[1] = 1
    nodes: list[FlowNode] = Field(default_factory=list, max_length=200)
    chat_settings: ChatSettings | None = None  # Chatflow only
    inputs: list[WorkflowField] = Field(default_factory=list, max_length=100)  # Workflow only
    outputs: list[WorkflowField] = Field(default_factory=list, max_length=100)
    triggers: WorkflowTriggers | None = None


class AgentCreateRequest(BaseModel):
    kind: AgentKind
    is_team: bool = False
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=1000)
    icon: Icon | None = None


class AgentUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    icon: Icon | None = None
    config: dict | None = None  # validated against AgentConfigV1 / FlowConfigV1 by kind
    row_version: int | None = None


class AgentTransitionRequest(BaseModel):
    row_version: int | None = None


class AgentSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: AgentKind
    is_team: bool
    name: str
    description: str
    icon: str
    status: AgentStatus
    model_id: UUID | None = None
    model_name: str | None = None
    node_count: int = 0
    has_draft_changes: bool = False
    config_corrupted: bool = False
    available_at: datetime | None = None
    updated_at: datetime
    updated_by: str | None = None
    row_version: int


class AgentResponse(AgentSummaryResponse):
    config: dict | None = None  # draft config; None when it cannot be parsed
    available_config: dict | None = None
    reference_count: int = 0
    can_edit: bool = False
    can_publish: bool = False


class AgentCountsResponse(BaseModel):
    agent: int
    chatflow: int
    workflow: int


class CheckIssue(BaseModel):
    code: str
    message: str


class AgentChecksResponse(BaseModel):
    ok: bool
    issues: list[CheckIssue]


class AgentReferenceEntry(BaseModel):
    kind: Literal["assistant", "agent", "chatflow", "workflow"]
    id: UUID
    name: str


class AgentReferencesResponse(BaseModel):
    total: int
    items: list[AgentReferenceEntry]
