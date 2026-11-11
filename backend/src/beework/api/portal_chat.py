"""AI portal — the employee's own conversations and projects (T-143, 演进 PRD 11.2–11.3).

Everything here is the employee's own: another employee's conversation or project is indistinguishable from a missing one
(404) and administrators have no view of them. Every endpoint needs a portal seat (T-151); features the administrator
switched off (projects, pinning, search) answer 403 ``portal.feature_disabled`` (T-141). Messages run through the ordinary
runtime — frozen live version, OC pipeline, tracing, usage — with ``source=portal``.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework import portal_service
from beework.auth import require_csrf
from beework.channel_chat import (
    relay_events,
    resolve_target,
    start_chat,
    wait_for_reply,
)
from beework.db import get_db
from beework.errors import ApiError
from beework.models import AuditEvent, Membership, Tenant
from beework.models.conversations import Conversation, EndUser, Message
from beework.models.portal import PortalApp
from beework.models.portal_chat import PortalProject
from beework.schemas.portal_chat import (
    ChatReply,
    ChatRequest,
    CitationEntry,
    ConversationEntry,
    ConversationList,
    ConversationPatch,
    MessageEntry,
    MessageList,
    PortalAppEntry,
    ProjectBody,
    ProjectDetail,
    ProjectEntry,
)

router = APIRouter()
seat = Depends(portal_service.require_portal_seat)
TITLE_MAX = 30
NOT_FOUND = ("not_found", "未找到指定内容")


def _feature(db: Session, membership: Membership, feature: str) -> None:
    if not portal_service.feature_enabled(db, membership.tenant_id, feature):
        raise ApiError(403, "portal.feature_disabled", "管理员已关闭此功能")


def _audit(db: Session, membership: Membership, action: str, details: dict[str, str]) -> None:
    db.add(AuditEvent(tenant_id=membership.tenant_id, actor_user_id=membership.user_id, action=action, details=details))


def _end_user(db: Session, membership: Membership) -> EndUser | None:
    return db.scalar(
        select(EndUser).where(
            EndUser.tenant_id == membership.tenant_id, EndUser.channel_type == "portal", EndUser.external_id == str(membership.user_id)
        )
    )


def _own_conversations(db: Session, membership: Membership):
    end_user = _end_user(db, membership)
    owner = end_user.id if end_user is not None else None
    return select(Conversation).where(
        Conversation.tenant_id == membership.tenant_id, Conversation.source == "portal",
        Conversation.end_user_id == owner, Conversation.deleted_at.is_(None),
    )


def _conversation(db: Session, membership: Membership, conversation_id: UUID) -> Conversation:
    conversation = db.scalar(_own_conversations(db, membership).where(Conversation.id == conversation_id))
    if conversation is None:
        raise ApiError(404, *NOT_FOUND)
    return conversation


def _app_available(db: Session, app: PortalApp | None) -> bool:
    return app is not None and app.deleted_at is None and app.status == "listed" and portal_service.resolve_source(db, app)[0] == "available"


def _entry(db: Session, membership: Membership, conversation: Conversation, apps: dict[UUID, PortalApp | None]) -> ConversationEntry:
    app = apps.get(conversation.portal_app_id) if conversation.portal_app_id else None
    return ConversationEntry(
        id=conversation.id, title=conversation.title or "新对话", app_id=conversation.portal_app_id,
        app_name=app.name if app is not None else None, project_id=conversation.project_id,
        pinned=conversation.pinned_at is not None, last_active_at=conversation.last_active_at,
        readonly=not _app_available(db, app),
    )


def _apps_by_id(db: Session, conversations: list[Conversation]) -> dict[UUID, PortalApp | None]:
    ids = {c.portal_app_id for c in conversations if c.portal_app_id}
    return {app.id: app for app in db.scalars(select(PortalApp).where(PortalApp.id.in_(ids)))} if ids else {}


def _hidden(db: Session, membership: Membership, entry: ConversationEntry) -> bool:
    """A conversation of a delisted app is shown read-only or not at all, as the administrator set (T-141)."""
    return entry.readonly and portal_service.load_settings(db, membership.tenant_id)[0].delisted_history == "hidden"


def _project(db: Session, membership: Membership, project_id: UUID) -> PortalProject:
    project = db.get(PortalProject, project_id)
    if project is None or project.deleted_at is not None or project.tenant_id != membership.tenant_id or project.user_id != membership.user_id:
        raise ApiError(404, *NOT_FOUND)
    return project


# ---------------------------------------------------------------------------
# Apps (the picker; the full library with search and categories is T-144).
# ---------------------------------------------------------------------------


@router.get("/portal/me/apps", response_model=list[PortalAppEntry])
def my_apps(membership: Membership = seat, db: Session = Depends(get_db)) -> list[PortalAppEntry]:
    return [
        PortalAppEntry(id=a.id, name=a.name, description=a.description, category=a.category, icon=a.icon, featured=a.featured, mode=a.mode)
        for a in portal_service.visible_apps(db, membership)
    ]


# ---------------------------------------------------------------------------
# Conversations.
# ---------------------------------------------------------------------------


@router.get("/portal/me/conversations", response_model=ConversationList)
def list_conversations(
    q: str | None = Query(default=None, max_length=100),
    project_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    membership: Membership = seat,
    db: Session = Depends(get_db),
) -> ConversationList:
    """Pinned first, then most recently used."""
    statement = _own_conversations(db, membership)
    if q and q.strip():
        _feature(db, membership, "search")
        statement = statement.where(func.lower(func.coalesce(Conversation.title, "")).like(f"%{q.strip().lower()}%"))
    if project_id is not None:
        _feature(db, membership, "projects")
        _project(db, membership, project_id)
        statement = statement.where(Conversation.project_id == project_id)
    rows = list(db.scalars(statement.order_by(Conversation.pinned_at.is_(None), Conversation.pinned_at.desc(), Conversation.last_active_at.desc(), Conversation.id)))
    apps = _apps_by_id(db, rows)
    entries = [e for e in (_entry(db, membership, c, apps) for c in rows) if not _hidden(db, membership, e)]
    return ConversationList(items=entries[offset:offset + limit], total=len(entries), limit=limit, offset=offset)


@router.get("/portal/me/conversations/{conversation_id}/messages", response_model=MessageList)
def conversation_messages(conversation_id: UUID, membership: Membership = seat, db: Session = Depends(get_db)) -> MessageList:
    conversation = _conversation(db, membership, conversation_id)
    entry = _entry(db, membership, conversation, _apps_by_id(db, [conversation]))
    if _hidden(db, membership, entry):
        raise ApiError(404, *NOT_FOUND)
    rows = db.scalars(
        select(Message).where(Message.conversation_id == conversation.id, Message.role.in_(("user", "assistant"))).order_by(Message.seq)
    )
    return MessageList(
        conversation=entry,
        messages=[
            MessageEntry(
                id=m.id, seq=m.seq, role=m.role, content=m.content, created_at=m.created_at,
                citations=[CitationEntry(document=c.get("document_name"), snippet=c.get("snippet"), kind=c.get("kind", "document")) for c in (m.citations or [])],
            )
            for m in rows
        ],
    )


@router.patch("/portal/me/conversations/{conversation_id}", response_model=ConversationEntry)
def update_conversation(
    conversation_id: UUID,
    payload: ConversationPatch,
    membership: Membership = seat,
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ConversationEntry:
    conversation = _conversation(db, membership, conversation_id)
    if payload.title is not None:
        if not payload.title.strip():
            raise ApiError(422, "validation_error", "标题不能为空")
        conversation.title = payload.title.strip()
    if payload.pinned is not None:
        _feature(db, membership, "pinned")
        conversation.pinned_at = datetime.now(UTC) if payload.pinned else None
    if payload.clear_project:
        conversation.project_id = None
    elif payload.project_id is not None:
        _feature(db, membership, "projects")
        conversation.project_id = _project(db, membership, payload.project_id).id
    entry = _entry(db, membership, conversation, _apps_by_id(db, [conversation]))
    db.commit()
    return entry


@router.delete("/portal/me/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(
    conversation_id: UUID, membership: Membership = seat, _: None = Depends(require_csrf), db: Session = Depends(get_db),
) -> Response:
    """The employee's own delete is a soft delete; the tenant's retention policy still decides when the data is purged."""
    conversation = _conversation(db, membership, conversation_id)
    conversation.deleted_at = datetime.now(UTC)
    _audit(db, membership, "portal_conversation.deleted", {"conversation_id": str(conversation.id)})
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Sending a message: the real runtime, never a canned answer.
# ---------------------------------------------------------------------------


def _unavailable() -> ApiError:
    return ApiError(410, "portal.app_unavailable", "该应用已下架或暂时不可用，不能继续发送消息")


def _app_for_chat(db: Session, membership: Membership, app_id: UUID) -> PortalApp:
    app = db.get(PortalApp, app_id)
    if app is None or app.deleted_at is not None or app.tenant_id != membership.tenant_id or app.status == "draft":
        raise ApiError(404, *NOT_FOUND)
    if not portal_service.in_audience(db, membership, app):
        raise ApiError(404, *NOT_FOUND)  # out of scope looks exactly like missing
    if not _app_available(db, app) or app.source_id is None or app.source_type != "agent":
        raise _unavailable()
    return app


@router.post("/portal/me/chat", response_model=ChatReply, responses={200: {"content": {"text/event-stream": {}}}})
def chat(
    payload: ChatRequest,
    membership: Membership = seat,
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    app = _app_for_chat(db, membership, payload.app_id)
    if payload.conversation_id is not None:
        existing = _conversation(db, membership, payload.conversation_id)
        if existing.portal_app_id != app.id:
            raise ApiError(404, *NOT_FOUND)
    project = None
    if payload.project_id is not None and payload.conversation_id is None:
        _feature(db, membership, "projects")
        project = _project(db, membership, payload.project_id)
    tenant = db.get(Tenant, membership.tenant_id)
    target = resolve_target(db, tenant, app.source_id, "production", payload.message)  # the frozen live version
    title = " ".join(payload.message.split())[:TITLE_MAX]

    def prepare(conversation: Conversation) -> None:
        conversation.title, conversation.portal_app_id = title, app.id
        conversation.project_id = project.id if project is not None else None

    from beework.models import User

    user = db.get(User, membership.user_id)
    run, conversation = start_chat(
        db, target, source="portal", channel_type="portal", channel_instance_id=None, external_id=str(membership.user_id),
        end_user_name=user.display_name if user else None, message=payload.message, conversation_id=payload.conversation_id,
        on_create=prepare,
    )
    if project is not None:
        project.last_used_at = datetime.now(UTC)
        db.commit()
    timeout = target.dsl.limits.timeout_seconds
    if payload.stream:
        return StreamingResponse(
            relay_events(db.get_bind(), run.id, conversation.id, timeout_seconds=timeout),
            media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    reply = wait_for_reply(db.get_bind(), run.id, timeout_seconds=timeout)
    return ChatReply(conversation_id=conversation.id, run_id=run.id, message={"role": "assistant", "content": reply["content"]}, usage=reply["usage"])


# ---------------------------------------------------------------------------
# Projects.
# ---------------------------------------------------------------------------


def _project_entry(db: Session, membership: Membership, project: PortalProject) -> ProjectEntry:
    count = db.scalar(_own_conversations(db, membership).where(Conversation.project_id == project.id).with_only_columns(func.count())) or 0
    return ProjectEntry(
        id=project.id, name=project.name, description=project.description, conversation_count=count,
        updated_at=project.updated_at, last_used_at=project.last_used_at,
    )


def _projects_enabled(membership: Membership = seat, db: Session = Depends(get_db)) -> Membership:
    _feature(db, membership, "projects")
    return membership


@router.get("/portal/me/projects", response_model=list[ProjectEntry])
def list_projects(membership: Membership = Depends(_projects_enabled), db: Session = Depends(get_db)) -> list[ProjectEntry]:
    rows = db.scalars(
        select(PortalProject).where(
            PortalProject.tenant_id == membership.tenant_id, PortalProject.user_id == membership.user_id, PortalProject.deleted_at.is_(None)
        ).order_by(PortalProject.updated_at.desc(), PortalProject.id)
    )
    return [_project_entry(db, membership, p) for p in rows]


@router.post("/portal/me/projects", response_model=ProjectEntry, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectBody, membership: Membership = Depends(_projects_enabled), _: None = Depends(require_csrf), db: Session = Depends(get_db),
) -> ProjectEntry:
    project = PortalProject(tenant_id=membership.tenant_id, user_id=membership.user_id, name=payload.name, description=payload.description.strip())
    db.add(project)
    db.flush()
    _audit(db, membership, "portal_project.created", {"project_id": str(project.id)})
    entry = _project_entry(db, membership, project)
    db.commit()
    return entry


@router.get("/portal/me/projects/{project_id}", response_model=ProjectDetail)
def get_project(project_id: UUID, membership: Membership = Depends(_projects_enabled), db: Session = Depends(get_db)) -> ProjectDetail:
    project = _project(db, membership, project_id)
    rows = list(
        db.scalars(
            _own_conversations(db, membership).where(Conversation.project_id == project.id)
            .order_by(Conversation.pinned_at.is_(None), Conversation.pinned_at.desc(), Conversation.last_active_at.desc())
        )
    )
    apps = _apps_by_id(db, rows)
    entries = [e for e in (_entry(db, membership, c, apps) for c in rows) if not _hidden(db, membership, e)]
    return ProjectDetail(**_project_entry(db, membership, project).model_dump(), conversations=entries)


@router.patch("/portal/me/projects/{project_id}", response_model=ProjectEntry)
def update_project(
    project_id: UUID, payload: ProjectBody, membership: Membership = Depends(_projects_enabled),
    _: None = Depends(require_csrf), db: Session = Depends(get_db),
) -> ProjectEntry:
    project = _project(db, membership, project_id)
    project.name, project.description = payload.name, payload.description.strip()
    _audit(db, membership, "portal_project.updated", {"project_id": str(project.id)})
    db.flush()
    entry = _project_entry(db, membership, project)
    db.commit()
    return entry


@router.delete("/portal/me/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: UUID, membership: Membership = Depends(_projects_enabled), _: None = Depends(require_csrf), db: Session = Depends(get_db),
) -> Response:
    """Deleting a project keeps its conversations — they just leave the project."""
    project = _project(db, membership, project_id)
    for conversation in db.scalars(select(Conversation).where(Conversation.project_id == project.id, Conversation.tenant_id == membership.tenant_id)):
        conversation.project_id = None
    project.deleted_at = datetime.now(UTC)
    _audit(db, membership, "portal_project.deleted", {"project_id": str(project.id)})
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
