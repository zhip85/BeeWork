"""AI portal apps (T-140): what an app can point at, when it can be listed, and who can see it (演进 PRD 10, 15).

``visible_apps`` is the one function that decides which apps an employee sees; the employee side (T-143 / T-144) calls
it too, so listing, delisting and visibility can never disagree.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.capabilities import MOUNT_RESOLVERS, MountItem
from beework.capability_catalog import MOUNT_FOR_TARGET, assistant_edges
from beework.models import Membership, Organization
from beework.models.assistants import Assistant, AssistantVersion
from beework.models.portal import PortalApp, PortalAppOrganization
from beework.rbac import organization_subtree

SourceState = Literal["available", "unavailable", "unset"]


@dataclass
class SourceInfo:
    name: str
    state: SourceState
    modes: list[str] = field(default_factory=lambda: ["chat"])
    description: str = ""


# resolver(db, tenant_id, ids) -> {id: SourceInfo} for what exists; the connections register theirs (T-075).
SourceResolver = Callable[[Session, UUID, list[UUID]], dict[UUID, SourceInfo]]
SOURCE_RESOLVERS: dict[str, SourceResolver] = {}
# lister(db, tenant_id, q) -> [(id, SourceInfo)] of the sources that can be put in the portal right now.
SourceLister = Callable[[Session, UUID, str | None], list[tuple[UUID, SourceInfo]]]
SOURCE_LISTERS: dict[str, SourceLister] = {}


def _agent_state(assistant: Assistant) -> SourceState:
    """演进 PRD 10.2: the agent must be published (a live version) and not disabled."""
    return "available" if assistant.status == "published" and assistant.live_version_id is not None else "unavailable"


def resolve_agents(db: Session, tenant_id: UUID, ids: list[UUID]) -> dict[UUID, SourceInfo]:
    if not ids:
        return {}
    rows = db.scalars(
        select(Assistant).where(Assistant.tenant_id == tenant_id, Assistant.id.in_(ids), Assistant.deleted_at.is_(None))
    )
    return {a.id: SourceInfo(a.name, _agent_state(a), ["chat"], a.description) for a in rows}


def list_agents(db: Session, tenant_id: UUID, q: str | None) -> list[tuple[UUID, SourceInfo]]:
    statement = select(Assistant).where(
        Assistant.tenant_id == tenant_id, Assistant.deleted_at.is_(None), Assistant.status == "published",
        Assistant.live_version_id.is_not(None),
    )
    if q and q.strip():
        statement = statement.where(Assistant.name.ilike(f"%{q.strip()}%"))
    return [(a.id, SourceInfo(a.name, "available", ["chat"], a.description)) for a in db.scalars(statement.order_by(Assistant.name, Assistant.id))]


SOURCE_RESOLVERS["agent"] = resolve_agents
SOURCE_LISTERS["agent"] = list_agents


def resolve_source(db: Session, app: PortalApp) -> tuple[SourceState, str | None, list[str]]:
    """(state, name, modes) of the app's source. A row without a source id is “unset”, never an error (10.2, 14.3)."""
    if app.source_id is None:
        return "unset", None, ["chat"]
    resolver = SOURCE_RESOLVERS.get(app.source_type)
    info = resolver(db, app.tenant_id, [app.source_id]).get(app.source_id) if resolver else None
    if info is None:
        return "unavailable", None, ["chat"]
    return info.state, info.name, info.modes


def live_invalid_references(db: Session, assistant: Assistant) -> list[MountItem]:
    """Capabilities the live version mounts that are deleted or not available — what employees would actually hit."""
    if assistant.live_version_id is None:
        return []
    version = db.get(AssistantVersion, assistant.live_version_id)
    by_mount: dict[str, list[UUID]] = {}
    for _relation, target_type, target_id in assistant_edges(((version.snapshot or {}) if version else {}).get("config")):
        by_mount.setdefault(MOUNT_FOR_TARGET[target_type], []).append(target_id)
    items: list[MountItem] = []
    for mount_kind, ids in by_mount.items():
        resolver = MOUNT_RESOLVERS.get(mount_kind)
        found = resolver(db, assistant.tenant_id, ids) if resolver else {}
        for target_id in ids:
            item = found.get(target_id) or MountItem(id=target_id, kind=mount_kind, name="", state="deleted")
            if item.state != "available":
                items.append(item)
    return items


def app_organization_ids(db: Session, app: PortalApp) -> list[UUID]:
    return list(db.scalars(select(PortalAppOrganization.organization_id).where(PortalAppOrganization.app_id == app.id)))


def listing_problems(db: Session, app: PortalApp) -> list[tuple[str, str]]:
    """演进 PRD 10.3: why the app cannot be listed (empty list = it can)."""
    problems: list[tuple[str, str]] = []
    if not app.name.strip():
        problems.append(("name_required", "请填写员工端名称"))
    state, _name, _modes = resolve_source(db, app)
    if state == "unset":
        problems.append(("source_unset", "来源未设置"))
    elif state != "available":
        problems.append(("source_unavailable", "来源未发布、已停用或已删除"))
    elif app.source_type == "agent" and app.source_id is not None:
        assistant = db.get(Assistant, app.source_id)
        if assistant is not None and live_invalid_references(db, assistant):
            problems.append(("source_invalid_references", "来源含失效的能力引用，请先处理后重新发布"))
    if app.audience == "orgs":
        ids = app_organization_ids(db, app)
        existing = set(
            db.scalars(select(Organization.id).where(Organization.tenant_id == app.tenant_id, Organization.id.in_(ids)))
        ) if ids else set()
        if not ids or existing != set(ids):
            problems.append(("audience_invalid", "可见范围需指定存在的组织"))
    if app.mode == "form" and "form" not in resolve_source(db, app)[2]:
        problems.append(("mode_invalid", "该来源不支持表单模式"))
    return problems


def visible_apps(db: Session, membership: Membership) -> list[PortalApp]:
    """What this member may see in the portal: listed, in scope, and the underlying resource still valid (演进 PRD 5.4).

    Scope: ``all`` — everyone in the tenant; ``orgs`` — members whose organization is one of the app's organizations or
    below it; ``admins`` — tenant administrators only.
    """
    apps = db.scalars(
        select(PortalApp).where(
            PortalApp.tenant_id == membership.tenant_id, PortalApp.deleted_at.is_(None), PortalApp.status == "listed"
        ).order_by(PortalApp.featured.desc(), PortalApp.name, PortalApp.id)
    ).all()
    visible: list[PortalApp] = []
    for app in apps:
        if app.audience == "admins" and membership.role != "tenant_admin":
            continue
        if app.audience == "orgs" and membership.role != "tenant_admin":
            scope: set[UUID] = set()
            for org_id in app_organization_ids(db, app):
                scope |= set(organization_subtree(db, membership.tenant_id, org_id))
            if membership.organization_id not in scope:
                continue
        if resolve_source(db, app)[0] != "available":
            continue
        visible.append(app)
    return visible


# ---------------------------------------------------------------------------
# Portal settings (T-141).
# ---------------------------------------------------------------------------

from fastapi import Depends  # noqa: E402

from beework.auth import get_tenant_membership  # noqa: E402
from beework.db import get_db  # noqa: E402
from beework.models import Tenant  # noqa: E402
from beework.models.portal import PortalSettings  # noqa: E402
from beework.schemas.portal import PortalSettingsBody  # noqa: E402

SETTING_FIELDS = tuple(PortalSettingsBody.model_fields)
FEATURES = {
    "search": "nav_search", "projects": "nav_projects", "app_library": "nav_app_library", "recent": "nav_recent",
    "pinned": "nav_pinned", "file_upload": "file_upload", "voice_input": "voice_input", "profile_edit": "profile_edit",
    "personal_memory": "personal_memory", "task_notifications": "task_notifications",
}


def load_settings(db: Session, tenant_id: UUID) -> tuple[PortalSettingsBody, PortalSettings | None]:
    """The tenant's settings, or the defaults when it never saved any (nothing is written by reading)."""
    row = db.get(PortalSettings, tenant_id)
    if row is None:
        return PortalSettingsBody(), None
    return PortalSettingsBody(**{name: getattr(row, name) for name in SETTING_FIELDS}), row


def portal_name(db: Session, tenant_id: UUID, settings_: PortalSettingsBody) -> str:
    if settings_.portal_name.strip():
        return settings_.portal_name.strip()
    tenant = db.get(Tenant, tenant_id)
    return f"{tenant.name} AI" if tenant else "AI"


def require_portal_feature(feature: str):
    """Dependency for employee-side endpoints: refused with 403 ``portal.feature_disabled`` when the administrator
    switched the feature off — the server enforces it, hiding the menu is only a courtesy (演进 PRD 10.4)."""
    field = FEATURES[feature]

    def dependency(membership: Membership = Depends(get_tenant_membership), db: Session = Depends(get_db)) -> Membership:
        from beework.api.conventions import (
            ApiError,  # imported late: the API package imports this module
        )

        settings_, _row = load_settings(db, membership.tenant_id)
        if not getattr(settings_, field):
            raise ApiError(403, "portal.feature_disabled", "管理员已关闭此功能")
        return membership

    return dependency


def feature_enabled(db: Session, tenant_id: UUID, feature: str) -> bool:
    """For code paths that are not endpoints (e.g. the memory write of a portal conversation, T-056 / T-145)."""
    return bool(getattr(load_settings(db, tenant_id)[0], FEATURES[feature]))
