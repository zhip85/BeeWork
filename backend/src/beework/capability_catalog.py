"""Who references what (T-070, GLB-083, RUL-011 / RUL-012, 演进 PRD 15.2).

``resource_references`` is derived data: whenever an assistant (智能体) is flushed with a changed configuration, deleted
or published, its rows are replaced as a whole inside the same transaction — so every write path (API, restore, rollback,
copy, tests) is covered and nothing has to remember to call a hook. (The self-built flow canvas, which was also a referrer,
was retired in T-169.)
"""

from collections import defaultdict
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from sqlalchemy import delete, event, insert, inspect, select
from sqlalchemy.orm import Session

from beework.capabilities import MOUNT_RESOLVERS, MountItem
from beework.models import utc_now
from beework.models.assistants import Assistant, AssistantVersion
from beework.models.capability_refs import ResourceReference

# An assistant's ``mounts`` keys → what the target is. ``chatflow`` / ``flow`` mount flow *connections* (T-075).
TARGET_FOR_MOUNT = {
    "kb": "kb", "agent": "assistant", "skill": "skill", "mcp": "mcp", "api": "api",
    "chatflow": "chatflow_connection", "flow": "workflow_connection",
}
MOUNT_FOR_TARGET = {target: mount for mount, target in TARGET_FOR_MOUNT.items()}
Edge = tuple[str, str, UUID]  # (relation, target_type, target_id)


def assistant_edges(config: dict | None) -> set[Edge]:
    mounts = (config or {}).get("mounts") or {}
    edges: set[Edge] = set()
    for mount_kind, target_type in TARGET_FOR_MOUNT.items():
        for value in mounts.get(mount_kind) or []:
            try:
                edges.add(("mount", target_type, UUID(str(value))))
            except ValueError:
                continue
    return edges


def _replace(session: Session, tenant_id: UUID, referrer_type: str, referrer_id: UUID, layer: str, edges: set[Edge]) -> None:
    session.execute(
        delete(ResourceReference).where(
            ResourceReference.referrer_type == referrer_type,
            ResourceReference.referrer_id == referrer_id,
            ResourceReference.layer == layer,
        )
    )
    if edges:
        session.execute(
            insert(ResourceReference),
            [
                {
                    "id": uuid4(), "tenant_id": tenant_id, "referrer_type": referrer_type,
                    "referrer_id": referrer_id, "layer": layer, "relation": relation, "target_type": target_type,
                    "target_id": target_id,
                    "created_at": utc_now(),
                }
                for relation, target_type, target_id in sorted(edges, key=str)
            ],
        )


def _changed(obj: object, *attributes: str) -> bool:
    state = inspect(obj)
    return any(state.attrs[name].history.has_changes() for name in attributes)


def sync_assistant(session: Session, assistant: Assistant) -> None:
    """Recompute both layers: the draft from ``draft_config``, the live one from the live version's snapshot."""
    if assistant.deleted_at is not None:
        draft: set[Edge] = set()
        live: set[Edge] = set()
    else:
        draft = assistant_edges(assistant.draft_config)
        live = set()
        if assistant.live_version_id is not None:
            version = session.get(AssistantVersion, assistant.live_version_id)
            live = assistant_edges((version.snapshot or {}).get("config")) if version else set()
    _replace(session, assistant.tenant_id, "assistant", assistant.id, "draft", draft)
    _replace(session, assistant.tenant_id, "assistant", assistant.id, "live", live)


@event.listens_for(Session, "after_flush")
def _sync_after_flush(session: Session, _context) -> None:
    for obj in list(session.new) + list(session.dirty):
        if isinstance(obj, Assistant):
            if obj in session.new or _changed(obj, "draft_config", "live_version_id", "deleted_at"):
                sync_assistant(session, obj)


# ---------------------------------------------------------------------------
# Queries.
# ---------------------------------------------------------------------------


@dataclass
class Referrer:
    type: str  # assistant
    id: UUID
    name: str
    status: str  # the referrer's own status (draft / published / disabled)
    layers: set[str] = field(default_factory=set)
    relations: set[str] = field(default_factory=set)


def references_to(db: Session, tenant_id: UUID, target_type: str, target_id: UUID) -> list[Referrer]:
    """Everything that points at the target (draft or live), one entry per referrer — for “N 处引用” prompts before
    deleting or disabling (RUL-012, KNW-042, AGT-005) and for the dependencies tab (AST-070)."""
    rows = db.scalars(
        select(ResourceReference).where(
            ResourceReference.tenant_id == tenant_id, ResourceReference.target_type == target_type,
            ResourceReference.target_id == target_id,
        )
    ).all()
    grouped: dict[tuple[str, UUID], Referrer] = {}
    by_type: dict[str, set[UUID]] = defaultdict(set)
    for row in rows:
        by_type[row.referrer_type].add(row.referrer_id)
        entry = grouped.setdefault((row.referrer_type, row.referrer_id), Referrer(row.referrer_type, row.referrer_id, "", ""))
        entry.layers.add(row.layer)
        entry.relations.add(row.relation)
    assistants = {
        a.id: a for a in db.scalars(select(Assistant).where(Assistant.id.in_(by_type["assistant"]), Assistant.deleted_at.is_(None)))
    } if by_type["assistant"] else {}
    result: list[Referrer] = []
    for (referrer_type, referrer_id), entry in grouped.items():
        owner = assistants.get(referrer_id) if referrer_type == "assistant" else None
        if owner is None:
            continue
        entry.name, entry.status = owner.name, owner.status
        result.append(entry)
    return sorted(result, key=lambda r: (r.type, r.name.lower(), str(r.id)))


def invalid_references(db: Session, assistant: Assistant) -> list[MountItem]:
    """GLB-083: the draft's mounts that are deleted, disabled or not yet available."""
    items: list[MountItem] = []
    edges = assistant_edges(assistant.draft_config)
    by_mount: dict[str, list[UUID]] = defaultdict(list)
    for _relation, target_type, target_id in edges:
        by_mount[MOUNT_FOR_TARGET[target_type]].append(target_id)
    for mount_kind, ids in by_mount.items():
        resolver = MOUNT_RESOLVERS.get(mount_kind)
        found = resolver(db, assistant.tenant_id, ids) if resolver else {}
        for target_id in ids:
            item = found.get(target_id) or MountItem(id=target_id, kind=mount_kind, name="", state="deleted")
            if item.state != "available":
                items.append(item)
    return items
