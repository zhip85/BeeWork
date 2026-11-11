from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class PortalAppEntry(BaseModel):
    """What an employee sees of an app: never the resource type or the source (演进 PRD 10.1)."""

    id: UUID
    name: str
    description: str
    category: str
    icon: str
    featured: bool
    mode: str


class ChatRequest(BaseModel):
    app_id: UUID
    message: str = Field(min_length=1, max_length=20000)
    conversation_id: UUID | None = None  # omitted: start a new conversation
    project_id: UUID | None = None  # only for a new conversation
    stream: bool = True

    @field_validator("message")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("消息不能为空")
        return value


class ChatReply(BaseModel):
    conversation_id: UUID
    run_id: UUID
    message: dict
    usage: dict


class ConversationEntry(BaseModel):
    id: UUID
    title: str
    app_id: UUID | None
    app_name: str | None
    project_id: UUID | None
    pinned: bool
    last_active_at: datetime
    readonly: bool  # the app was delisted or its source is gone: history stays, new messages do not


class ConversationList(BaseModel):
    items: list[ConversationEntry]
    total: int
    limit: int
    offset: int


class ConversationPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    pinned: bool | None = None
    project_id: UUID | None = None
    clear_project: bool = False  # move out of its project


class CitationEntry(BaseModel):
    document: str | None = None
    snippet: str | None = None
    kind: str = "document"


class MessageEntry(BaseModel):
    id: UUID
    seq: int
    role: str
    content: str
    citations: list[CitationEntry] = Field(default_factory=list)
    created_at: datetime


class MessageList(BaseModel):
    conversation: ConversationEntry
    messages: list[MessageEntry]


class ProjectBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=300)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("项目名称不能为空")
        return value


class ProjectEntry(BaseModel):
    id: UUID
    name: str
    description: str
    conversation_count: int
    updated_at: datetime
    last_used_at: datetime | None


class ProjectDetail(ProjectEntry):
    conversations: list[ConversationEntry]
