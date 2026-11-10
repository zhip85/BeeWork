"""Public endpoints of the website widget (T-040): ``/v1/widget/*`` and the embed script.

A page proves itself with the channel's public ``site_key`` *and* its ``Origin`` (checked against the published
snapshot's allow-list), and receives a short-lived visitor token bound to that origin. The token is the only
credential for chatting; the site key alone can never reach another tenant's assistants.
"""

import hashlib
import secrets
import time
from pathlib import Path
from uuid import UUID

import jwt
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.channel_chat import (
    enforce_qps,
    enforce_visitor_limit,
    relay_events,
    resolve_target,
    start_chat,
    wait_for_reply,
)
from beework.channel_publish import normalize_origin
from beework.config import settings
from beework.db import get_db
from beework.errors import ApiError
from beework.models import Tenant
from beework.models.channels import ChannelInstance
from beework.models.conversations import Conversation, EndUser, Message
from beework.schemas.channels import (
    WidgetChatRequest,
    WidgetConfig,
    WidgetSession,
    WidgetSessionRequest,
)
from beework.schemas.open_api import ChatResponse, MessageOut, MessagesResponse

router = APIRouter(prefix="/v1", tags=["widget"])

TOKEN_TTL_SECONDS = 3600
SCRIPT = Path(__file__).resolve().parent.parent / "static" / "widget.js"


def _signing_key() -> str:
    # Derived, so a widget token can never be mistaken for (or forged from) a console session token.
    return hashlib.sha256(f"widget:{settings.auth_secret}".encode()).hexdigest()


def issue_token(channel: ChannelInstance, visitor_id: str, origin: str, *, now: float | None = None) -> str:
    issued = int(now if now is not None else time.time())
    claims = {"typ": "widget", "ch": str(channel.id), "vis": visitor_id, "org": origin, "iat": issued, "exp": issued + TOKEN_TTL_SECONDS}
    return jwt.encode(claims, _signing_key(), algorithm="HS256")


def _claims(authorization: str | None) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(401, "missing_token", "缺少访客令牌")
    try:
        claims = jwt.decode(authorization[7:].strip(), _signing_key(), algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise ApiError(401, "expired_token", "访客令牌已过期，请刷新会话") from None
    except jwt.InvalidTokenError:
        raise ApiError(401, "invalid_token", "访客令牌无效") from None
    if claims.get("typ") != "widget":
        raise ApiError(401, "invalid_token", "访客令牌无效")
    return claims


def _origin(request: Request) -> str:
    raw = request.headers.get("origin")
    if not raw:
        raise ApiError(403, "origin_required", "请求缺少 Origin，挂件只能在浏览器页面中使用")
    try:
        return normalize_origin(raw)
    except ValueError:
        raise ApiError(403, "origin_not_allowed", "该网站不在渠道的允许域名中") from None


def _live_channel(db: Session, channel: ChannelInstance | None, origin: str, request: Request) -> ChannelInstance:
    """The channel as published: active, with a snapshot whose allow-list contains the caller's origin."""
    if channel is None:
        raise ApiError(404, "site_not_found", "未找到该站点")
    snapshot = channel.published_snapshot
    if channel.status != "active" or not snapshot:
        raise ApiError(403, "channel_unavailable", "该渠道当前未启用")
    if origin not in (snapshot.get("allowed_origins") or []):
        raise ApiError(403, "origin_not_allowed", "该网站不在渠道的允许域名中")
    request.state.cors_origin = origin  # the response may now be read by that page
    return channel


def _visitor_context(db: Session, request: Request, authorization: str | None) -> tuple[ChannelInstance, Tenant, str]:
    claims = _claims(authorization)
    origin = _origin(request)
    if claims["org"] != origin:
        raise ApiError(403, "origin_mismatch", "令牌不属于当前网站")
    channel = _live_channel(db, db.get(ChannelInstance, UUID(claims["ch"])), origin, request)
    tenant = db.get(Tenant, channel.tenant_id)
    if tenant is None:
        raise ApiError(404, "site_not_found", "未找到该站点")
    return channel, tenant, claims["vis"]


@router.options("/widget/{rest:path}", include_in_schema=False)
def preflight(request: Request) -> Response:
    """CORS preflight. It reveals nothing: every real request is checked against the channel's allow-list."""
    origin = request.headers.get("origin")
    headers = {"Vary": "Origin"}
    if origin:
        headers.update({
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Access-Control-Max-Age": "600",
        })
    return Response(status_code=204, headers=headers)


@router.get("/widget.js", include_in_schema=False)
def widget_script() -> FileResponse:
    return FileResponse(
        SCRIPT, media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=300", "Access-Control-Allow-Origin": "*"},
    )


@router.post("/widget/session", response_model=WidgetSession, summary="为访客创建会话令牌")
def create_session(payload: WidgetSessionRequest, request: Request, db: Session = Depends(get_db)) -> WidgetSession:
    ip = request.client.host if request.client else None
    enforce_visitor_limit(ip, "session", per_second=0.2, burst=10)
    origin = _origin(request)
    channel = db.scalar(select(ChannelInstance).where(ChannelInstance.site_key == payload.site_key, ChannelInstance.type == "website"))
    channel = _live_channel(db, channel, origin, request)
    visitor_id = payload.visitor_id or "v_" + secrets.token_urlsafe(18)
    experience = channel.published_snapshot["experience"]
    return WidgetSession(
        token=issue_token(channel, visitor_id, origin), expires_in=TOKEN_TTL_SECONDS, visitor_id=visitor_id,
        config=WidgetConfig(**{k: experience[k] for k in ("assistant_name", "welcome", "primary_color")}),
    )


@router.post("/widget/chat", response_model=ChatResponse, summary="访客与智能体对话")
def widget_chat(
    payload: WidgetChatRequest,
    request: Request,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    channel, tenant, visitor_id = _visitor_context(db, request, authorization)
    ip = request.client.host if request.client else None
    enforce_visitor_limit(ip, f"chat:{channel.id}", per_second=0.5, burst=10)
    enforce_qps(db, tenant)
    snapshot = channel.published_snapshot
    target = resolve_target(db, tenant, UUID(snapshot["assistant_id"]), snapshot["environment"], payload.message)
    run, conversation = start_chat(
        db, target, source="website", channel_type="website", channel_instance_id=channel.id, external_id=visitor_id,
        end_user_name=None, message=payload.message, conversation_id=payload.conversation_id,
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


@router.get("/widget/conversations/{conversation_id}/messages", response_model=MessagesResponse, summary="读取本访客的会话消息")
def widget_messages(
    conversation_id: UUID,
    request: Request,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> MessagesResponse:
    channel, tenant, visitor_id = _visitor_context(db, request, authorization)
    conversation = db.scalar(
        select(Conversation)
        .join(EndUser, EndUser.id == Conversation.end_user_id)
        .where(
            Conversation.id == conversation_id,
            Conversation.tenant_id == tenant.id,
            Conversation.channel_instance_id == channel.id,
            EndUser.external_id == visitor_id,
            EndUser.channel_type == "website",
        )
    )
    if conversation is None:  # someone else's conversation looks exactly like a missing one
        raise ApiError(404, "conversation_not_found", "未找到指定会话")
    rows = list(
        db.scalars(
            select(Message).where(Message.conversation_id == conversation.id, Message.tenant_id == tenant.id, Message.seq > after_seq)
            .order_by(Message.seq).limit(limit + 1)
        )
    )
    return MessagesResponse(
        conversation_id=conversation.id,
        messages=[MessageOut(id=m.id, role=m.role, content=m.content, created_at=m.created_at) for m in rows[:limit]],
        has_more=len(rows) > limit,
    )
