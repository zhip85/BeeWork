from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from beework.api_keys import SCOPES

Environment = Literal["development", "staging", "production"]
Scope = Literal["chat", "knowledge.read", "knowledge.write", "analytics.read"]


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    environment: Environment = "production"
    scopes: list[Scope] = Field(default_factory=lambda: ["chat"], min_length=1, max_length=len(SCOPES))
    expires_at: datetime | None = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("名称不能为空")
        return value


class ApiKeyResponse(BaseModel):
    id: UUID
    name: str
    environment: str
    scopes: list[str]
    masked: str
    created_by_name: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    status: Literal["active", "expired", "revoked"]


class ApiKeyCreated(ApiKeyResponse):
    # The only time the plaintext is ever returned.
    key: str


class ApiKeyList(BaseModel):
    items: list[ApiKeyResponse]


# -- open API (/v1) ----------------------------------------------------------------------------------


class EndUserIn(BaseModel):
    id: str = Field(min_length=1, max_length=200, description="终端用户在调用方系统中的唯一标识")
    name: str | None = Field(default=None, max_length=120)


class ComponentEvent(BaseModel):
    """What an end user did on a component (a button, a confirmed appointment). It becomes the next turn's input (T-059)."""

    tag: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_]+$", description="组件的触发标签")
    action: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.-]+$")
    data: dict = Field(default_factory=dict)

    def as_message(self) -> str:
        import json

        return f"[用户操作] 组件 <{self.tag}> {self.action}：{json.dumps(self.data, ensure_ascii=False)}"[:4000]


class ChatRequest(BaseModel):
    message: str = Field(default="", max_length=20000, description="用户消息；与 event 二选一")
    event: ComponentEvent | None = Field(default=None, description="用户在组件上的操作，作为下一轮输入")
    conversation_id: UUID | None = Field(default=None, description="继续已有会话；省略则开始新会话")
    channel_id: UUID | None = Field(default=None, description="开放 API 类型的渠道；填写后按该渠道已发布快照里的组件作答，响应带 widgets")
    end_user: EndUserIn
    stream: bool = Field(default=False, description="true 时以 text/event-stream 返回")

    @model_validator(mode="after")
    def message_or_event(self) -> "ChatRequest":
        if self.event is not None:
            self.message = self.event.as_message()
        elif not self.message.strip():
            raise ValueError("message 与 event 至少填写一个")
        return self


class ChatMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str


class Usage(BaseModel):
    input_tokens: int | None
    output_tokens: int | None


class ChatResponse(BaseModel):
    conversation_id: UUID
    run_id: UUID
    message: ChatMessage
    usage: Usage
    widgets: list[dict] = Field(default_factory=list, description="渠道组件的结构化数据（json / native 模式）")


class MessageOut(BaseModel):
    id: UUID
    role: str
    content: str
    created_at: datetime


class MessagesResponse(BaseModel):
    conversation_id: UUID
    messages: list[MessageOut]
    has_more: bool
