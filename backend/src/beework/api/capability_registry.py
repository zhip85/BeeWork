"""The unified capability registry (T-160): list / read / edit the entries, repair the derivation, recall inside an assistant's scope."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import beework.capability_registry as registry  # noqa: F401  (also registers the flush listener that keeps the entries derived)
from beework.api.assistants import _actor_name, _load
from beework.api.conventions import ApiError, check_row_version
from beework.auth import require_csrf
from beework.db import get_db
from beework.models import AuditEvent, Membership, utc_now
from beework.models.capability_registry import CapabilityEntry
from beework.rbac import require_tenant_permission
from beework.schemas.capability_registry import (
    EntryKind,
    EntryList,
    EntryOwner,
    EntryResponse,
    EntryUpdate,
    RecallEntry,
    RecallResponse,
)

router = APIRouter()


def _response(entry: CapabilityEntry, status: str, *, with_schema: bool = False) -> EntryResponse:
    return EntryResponse(
        id=entry.id, kind=entry.kind, identifier=entry.identifier, name=entry.name, description=entry.description,  # type: ignore[arg-type]
        intent_description=entry.intent_description, intent_examples=list(entry.intent_examples or []), writes=entry.writes,
        direct_enabled=entry.direct_enabled, direct_output=entry.direct_output, timeout_ms=entry.timeout_ms, retry=entry.retry,
        policy=registry.resolve_policy(entry), status=status,  # type: ignore[arg-type]
        owner=EntryOwner(page=registry.OWNER_PAGE[entry.kind], id=entry.source_id, name=str((entry.meta or {}).get("source_name") or entry.name)),  # type: ignore[arg-type]
        meta=entry.meta or {}, row_version=entry.row_version, updated_at=entry.updated_at,
        input_schema=entry.input_schema if with_schema else None,
    )


def _load_entry(db: Session, membership: Membership, entry_id: UUID) -> CapabilityEntry:
    entry = db.get(CapabilityEntry, entry_id)
    if entry is None or entry.tenant_id != membership.tenant_id or entry.removed_at is not None:
        raise ApiError(404, "not_found", "未找到指定的注册项")
    return entry


@router.get("/registry/entries", response_model=EntryList)
def list_entries(
    kind: EntryKind | None = None,
    source_id: UUID | None = None,
    q: str | None = Query(default=None, max_length=100),
    status: str | None = Query(default=None, pattern="^(available|disabled|invalid)$"),
    limit: int = Query(default=200, ge=1, le=500),
    membership: Membership = Depends(require_tenant_permission("capabilities:view")),
    db: Session = Depends(get_db),
) -> EntryList:
    """Every live entry of the tenant, with whether its source can serve it now. Sub-agents and knowledge bases are here too —
    managed on their own pages; the owner link says where."""
    registry.ensure_backfilled(db, membership.tenant_id)
    statement = select(CapabilityEntry).where(CapabilityEntry.tenant_id == membership.tenant_id, CapabilityEntry.removed_at.is_(None))
    if kind:
        statement = statement.where(CapabilityEntry.kind == kind)
    if source_id:
        statement = statement.where(CapabilityEntry.source_id == source_id)
    if q and q.strip():
        pattern = f"%{q.strip().lower()}%"
        statement = statement.where(func.lower(CapabilityEntry.name).like(pattern) | func.lower(CapabilityEntry.description).like(pattern) | func.lower(CapabilityEntry.intent_description).like(pattern))
    entries = list(db.scalars(statement.order_by(CapabilityEntry.kind, func.lower(CapabilityEntry.name), CapabilityEntry.id)))
    statuses = registry.effective_status(db, membership.tenant_id, entries)
    items = [_response(e, statuses[e.id]) for e in entries if status is None or statuses[e.id] == status]
    db.commit()  # a first read may have backfilled
    return EntryList(items=items[:limit], total=len(items))


@router.get("/registry/entries/{entry_id}", response_model=EntryResponse)
def get_entry(
    entry_id: UUID,
    membership: Membership = Depends(require_tenant_permission("capabilities:view")),
    db: Session = Depends(get_db),
) -> EntryResponse:
    entry = _load_entry(db, membership, entry_id)
    return _response(entry, registry.effective_status(db, membership.tenant_id, [entry])[entry.id], with_schema=True)


@router.put("/registry/entries/{entry_id}", response_model=EntryResponse)
def update_entry(
    entry_id: UUID,
    payload: EntryUpdate,
    membership: Membership = Depends(require_tenant_permission("capabilities:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> EntryResponse:
    """Edit what only the registry holds: the intent description and examples, whether path A may call it directly, whether its result
    is returned as is, and its timeout / retry. A write tool cannot be made directly callable without a second thought: it can, but
    the executor still asks for the end user's confirmation (T-071)."""
    entry = _load_entry(db, membership, entry_id)
    check_row_version(entry, payload.row_version, db)
    entry.intent_description = payload.intent_description.strip()
    entry.intent_examples = payload.intent_examples
    entry.direct_enabled = payload.direct_enabled
    entry.direct_output = payload.direct_output and payload.direct_enabled
    entry.timeout_ms = payload.timeout_ms
    entry.retry = payload.retry.model_dump() if payload.retry is not None else None
    entry.row_version += 1
    entry.updated_by = _actor_name(db, membership)
    entry.updated_at = utc_now()
    db.add(AuditEvent(
        tenant_id=membership.tenant_id, actor_user_id=membership.user_id, action="registry_entry.updated",
        details={"entry_id": str(entry.id), "kind": entry.kind, "name": entry.name, "direct_enabled": str(entry.direct_enabled)},
    ))
    db.commit()
    return _response(entry, registry.effective_status(db, membership.tenant_id, [entry])[entry.id], with_schema=True)


@router.post("/registry/resync", response_model=dict)
def resync(
    membership: Membership = Depends(require_tenant_permission("capabilities:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    """Recompute every entry of the tenant from its sources. The entries follow their sources on their own; this is the repair for
    data that was changed behind the application's back."""
    live = registry.reconcile_tenant(db, membership.tenant_id)
    db.add(AuditEvent(tenant_id=membership.tenant_id, actor_user_id=membership.user_id, action="registry.resynced", details={"entries": str(live)}))
    db.commit()
    return {"entries": live}


@router.get("/assistants/{assistant_id}/registry/recall", response_model=RecallResponse)
def recall(
    assistant_id: UUID,
    q: str = Query(min_length=1, max_length=500),
    k: int = Query(default=8, ge=1, le=20),
    membership: Membership = Depends(require_tenant_permission("assistants:view")),
    db: Session = Depends(get_db),
) -> RecallResponse:
    """The entries of this assistant's scope — what it mounted and ticked — that best fit the query (the recall L2 and L3 build on).
    Nothing outside the scope is ever returned."""
    assistant = _load(db, membership, assistant_id)
    config = assistant.draft_config or {}
    found = registry.recall(db, membership.tenant_id, config, q, k)
    scope = len(registry.visible_entries(db, membership.tenant_id, config))
    db.commit()
    return RecallResponse(items=[RecallEntry(entry=_response(item.entry, "available"), score=round(item.score, 4)) for item in found], scope=scope)
