from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.api.assistants import _load, _visible_statement
from beework.api.conventions import ApiError, Page, Pagination, get_pagination, paginate
from beework.db import get_db
from beework.models import Membership
from beework.models.assistants import Assistant
from beework.models.conversations import Conversation, EndUser, Message
from beework.rbac import require_tenant_permission
from beework.schemas.conversations import (
    ConversationDetailResponse,
    ConversationResponse,
    MessageResponse,
)

router = APIRouter()


def _channel_types(db: Session, conversations: list[Conversation]) -> dict[UUID, str]:
    ids = {c.end_user_id for c in conversations if c.end_user_id}
    if not ids:
        return {}
    return {row.id: row.channel_type for row in db.scalars(select(EndUser).where(EndUser.id.in_(ids)))}


def _response(conversation: Conversation, channels: dict[UUID, str]) -> dict:
    return {
        "id": conversation.id,
        "assistant_id": conversation.assistant_id,
        "assistant_version": conversation.assistant_version,
        "source": conversation.source,
        "channel": channels.get(conversation.end_user_id, conversation.source) if conversation.end_user_id else conversation.source,
        "environment": conversation.environment,
        "end_user_id": conversation.end_user_id,
        "end_user_name": conversation.end_user_name,
        "turns": conversation.turns,
        "csat": conversation.csat,
        "tokens": conversation.tokens,
        "status": conversation.status,
        "started_at": conversation.started_at,
        "last_active_at": conversation.last_active_at,
        "ended_at": conversation.ended_at,
    }


@router.get("/assistants/{assistant_id}/conversations", response_model=Page[ConversationResponse])
def list_conversations(
    assistant_id: UUID,
    status: str | None = Query(default=None, pattern="^(in_progress|handoff|resolved|abandoned)$"),
    started_from: datetime | None = None,
    started_to: datetime | None = None,
    pagination: Pagination = Depends(get_pagination),
    membership: Membership = Depends(require_tenant_permission("conversations:view")),
    db: Session = Depends(get_db),
) -> Page[ConversationResponse]:
    assistant = _load(db, membership, assistant_id)
    statement = select(Conversation).where(
        Conversation.tenant_id == membership.tenant_id, Conversation.assistant_id == assistant.id
    )
    if status:
        statement = statement.where(Conversation.status == status)
    if started_from:
        statement = statement.where(Conversation.started_at >= started_from)
    if started_to:
        statement = statement.where(Conversation.started_at <= started_to)
    items, total = paginate(
        db, statement.order_by(Conversation.last_active_at.desc(), Conversation.id), pagination
    )
    channels = _channel_types(db, items)
    return Page[ConversationResponse](
        items=[ConversationResponse(**_response(item, channels)) for item in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailResponse)
def get_conversation(
    conversation_id: UUID,
    membership: Membership = Depends(require_tenant_permission("conversations:view")),
    db: Session = Depends(get_db),
) -> ConversationDetailResponse:
    statement = _visible_statement(
        select(Conversation)
        .join(
            Assistant,
            (Assistant.id == Conversation.assistant_id) & (Assistant.tenant_id == Conversation.tenant_id),
        )
        .where(Conversation.id == conversation_id, Conversation.tenant_id == membership.tenant_id),
        membership,
        db,
    )
    conversation = db.scalar(statement)
    if conversation is None:
        raise ApiError(404, "not_found", "未找到指定会话")
    messages = list(
        db.scalars(select(Message).where(Message.conversation_id == conversation.id).order_by(Message.seq))
    )
    return ConversationDetailResponse(
        **_response(conversation, _channel_types(db, [conversation])),
        messages=[MessageResponse.model_validate(message, from_attributes=True) for message in messages],
    )
