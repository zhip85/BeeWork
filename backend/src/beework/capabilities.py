"""Mounted-capability resolution for assistants (AST-060–066, GLB-083).

Each capability kind (knowledge base, agent, MCP, ...) registers a resolver when the task that owns it lands
(T-033 ``kb``, T-060 ``agent``/``chatflow``/``flow``, T-072 ``mcp``, T-074 ``skill``...). A mounted ID with no
resolver, or one the resolver does not return, is reported as ``deleted`` so the configuration check flags it.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from sqlalchemy.orm import Session

from beework.schemas.assistants import MOUNT_KINDS

MountState = Literal["available", "disabled", "deleted"]


@dataclass
class MountItem:
    id: UUID
    kind: str
    name: str
    state: MountState
    meta: dict[str, Any] = field(default_factory=dict)


# resolver(db, tenant_id, ids) -> {id: MountItem}; only IDs that still exist in the tenant are returned.
MountResolver = Callable[[Session, UUID, list[UUID]], dict[UUID, MountItem]]
MOUNT_RESOLVERS: dict[str, MountResolver] = {}


def resolve_mounts(db: Session, tenant_id: UUID, mounts: dict[str, Any]) -> list[MountItem]:
    items: list[MountItem] = []
    for kind in MOUNT_KINDS:
        ids = [UUID(str(value)) for value in mounts.get(kind) or []]
        if not ids:
            continue
        resolver = MOUNT_RESOLVERS.get(kind)
        found = resolver(db, tenant_id, ids) if resolver else {}
        for mount_id in ids:
            items.append(found.get(mount_id) or MountItem(id=mount_id, kind=kind, name="", state="deleted"))
    return items
