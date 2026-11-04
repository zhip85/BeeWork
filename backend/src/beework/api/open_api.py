"""The open API (T-028): ``/v1`` is separate from the console API, authenticated by tenant API keys.

Errors use ``{"error": {"code", "message"}}`` (see ``api.conventions``); the schema is published separately at
``/v1/openapi.json`` and browsable at ``/v1/docs``.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.api_keys import authenticate, require_scope
from beework.channel_chat import (
    enforce_qps,
    relay_events,
    resolve_target,
    start_chat,
    wait_for_reply,
)
from beework.db import get_db
from beework.errors import ApiError
from beework.models.conversations import Conversation, Message
from beework.schemas.open_api import ChatRequest, ChatResponse, MessageOut, MessagesResponse

router = APIRouter(prefix="/v1", tags=["open-api"])

OPEN_API_DESCRIPTION = (
    "BeeWork 开放 API：用租户 API 密钥调用助理。认证：`Authorization: Bearer bw_live_...`（生产）/ `bw_test_...`（开发、预发）。"
    "生产环境的密钥调用助理的已发布版本，开发/预发环境的密钥调用最新草稿。超过套餐 QPS 返回 429 与 `Retry-After`。"
    "错误统一为 `{\"error\": {\"code\": \"...\", \"message\": \"...\"}}`。"
)


@router.post(
    "/assistants/{assistant_id}/chat",
    response_model=ChatResponse,
    summary="与助理对话",
    responses={200: {"content": {"text/event-stream": {}}, "description": "stream=true 时返回 SSE：start / delta / done / error"}},
)
def chat(
    assistant_id: UUID,
    payload: ChatRequest,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    key, tenant = authenticate(db, authorization)
    require_scope(key, "chat")
    enforce_qps(db, tenant)
    target = resolve_target(db, tenant, assistant_id, key.environment, payload.message)
    run, conversation = start_chat(
        db, target, source="api", channel_type="open-api", channel_instance_id=None, external_id=payload.end_user.id,
        end_user_name=payload.end_user.name, message=payload.message, conversation_id=payload.conversation_id,
    )
    timeout = target.dsl.limits.timeout_seconds
    if payload.stream:
        return StreamingResponse(
            relay_events(db.get_bind(), run.id, conversation.id, timeout_seconds=timeout),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    reply = wait_for_reply(db.get_bind(), run.id, timeout_seconds=timeout)
    return ChatResponse(
        conversation_id=conversation.id, run_id=run.id, message={"role": "assistant", "content": reply["content"]}, usage=reply["usage"]
    )


@router.get("/conversations/{conversation_id}/messages", response_model=MessagesResponse, summary="读取会话消息")
def list_messages(
    conversation_id: UUID,
    after_seq: int = Query(default=0, ge=0, description="只返回序号大于它的消息"),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> MessagesResponse:
    key, tenant = authenticate(db, authorization)
    require_scope(key, "chat")
    enforce_qps(db, tenant)
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.tenant_id == tenant.id, Conversation.source == "api",
            Conversation.environment == key.environment,  # environments are isolated: a test key cannot read live conversations
        )
    )
    if conversation is None:  # other tenants' and console conversations are indistinguishable from missing ones
        raise ApiError(404, "conversation_not_found", "未找到指定会话")
    rows = list(
        db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id, Message.tenant_id == tenant.id, Message.seq > after_seq)
            .order_by(Message.seq)
            .limit(limit + 1)
        )
    )
    return MessagesResponse(
        conversation_id=conversation.id,
        messages=[MessageOut(id=m.id, role=m.role, content=m.content, created_at=m.created_at) for m in rows[:limit]],
        has_more=len(rows) > limit,
    )


@router.get("/openapi.json", include_in_schema=False)
def open_api_schema() -> JSONResponse:
    routes = [r for r in router.routes if r.path not in ("/v1/openapi.json", "/v1/docs")]
    return JSONResponse(get_openapi(title="BeeWork Open API", version="1.0.0", description=OPEN_API_DESCRIPTION, routes=routes))


@router.get("/docs", include_in_schema=False)
def open_api_docs():
    return get_swagger_ui_html(openapi_url="/v1/openapi.json", title="BeeWork Open API")
