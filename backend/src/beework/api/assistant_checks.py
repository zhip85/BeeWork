from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework.api.assistants import MODEL_ENVIRONMENT, _enabled_chat_models, _load
from beework.api.conventions import ApiError
from beework.api.knowledge import _visible as _visible_knowledge
from beework.assistant_checks import check_assistant
from beework.capabilities import CANDIDATE_LISTERS, MOUNT_RESOLVERS, resolve_mounts
from beework.capability_catalog import references_to
from beework.db import get_db
from beework.knowledge import content_stats, derive_status
from beework.models import Membership
from beework.models.assistants import Assistant
from beework.models.channels import ChannelInstance
from beework.models.knowledge import KnowledgeBase
from beework.rbac import require_tenant_permission
from beework.schemas.assistant_checks import (
    CandidateEntry,
    CandidatesResponse,
    CheckResponse,
    DependenciesResponse,
    DependencyEntry,
    MountEntry,
    MountGroup,
    MountsResponse,
    TypeEntry,
    TypesResponse,
)
from beework.schemas.assistants import MOUNT_KINDS

router = APIRouter()


@router.get("/assistants/{assistant_id}/checks", response_model=CheckResponse)
def assistant_checks(
    assistant_id: UUID,
    membership: Membership = Depends(require_tenant_permission("assistants:view")),
    db: Session = Depends(get_db),
) -> CheckResponse:
    assistant = _load(db, membership, assistant_id)
    models, _ = _enabled_chat_models(db, assistant.tenant_id)
    issues = check_assistant(
        db, assistant, enabled_model_ids={str(model.id) for model, _ in models}, environment=MODEL_ENVIRONMENT
    )
    return CheckResponse(
        issues=issues,
        blockers=sum(issue.severity == "blocker" for issue in issues),
        high_impact=sum(issue.severity == "high" for issue in issues),
        info=sum(issue.severity == "info" for issue in issues),
    )


@router.get("/assistants/{assistant_id}/mounts", response_model=MountsResponse)
def assistant_mounts(
    assistant_id: UUID,
    membership: Membership = Depends(require_tenant_permission("assistants:view")),
    db: Session = Depends(get_db),
) -> MountsResponse:
    assistant = _load(db, membership, assistant_id)
    items = resolve_mounts(db, assistant.tenant_id, (assistant.draft_config or {}).get("mounts") or {})
    groups = [
        MountGroup(
            kind=kind,
            items=[
                MountEntry(id=item.id, kind=item.kind, name=item.name, state=item.state, meta=item.meta)
                for item in items
                if item.kind == kind
            ],
        )
        for kind in MOUNT_KINDS
        if any(item.kind == kind for item in items)
    ]
    return MountsResponse(total=len(items), groups=groups, mountable_kinds=sorted(MOUNT_RESOLVERS))


@router.get("/capabilities/types", response_model=TypesResponse)
def capability_types(
    membership: Membership = Depends(require_tenant_permission("assistants:edit")),
    db: Session = Depends(get_db),
) -> TypesResponse:
    """The type column of the mount picker (AST-022): every type with whether it can be mounted yet and how many
    resources are available (the badge)."""
    items: list[TypeEntry] = []
    for kind in MOUNT_KINDS:
        lister = CANDIDATE_LISTERS.get(kind)
        available = len(lister(db, membership, None, 100)) if lister else 0
        items.append(TypeEntry(kind=kind, mountable=kind in MOUNT_RESOLVERS and lister is not None, available=available))
    return TypesResponse(items=items)


@router.get("/capabilities/candidates", response_model=CandidatesResponse)
def mount_candidates(
    kind: str = Query(default="kb", pattern="^(" + "|".join(MOUNT_KINDS) + ")$"),
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=100),
    membership: Membership = Depends(require_tenant_permission("assistants:edit")),
    db: Session = Depends(get_db),
) -> CandidatesResponse:
    """What can be mounted (the capability picker, AST-022): only resources that are available now (RUL-010) and that
    the caller may see — a knowledge base must already be indexed, a self-built skill must have passed its check."""
    lister = CANDIDATE_LISTERS.get(kind)
    if lister is None:
        raise ApiError(422, "capability.kind_unavailable", "该类型的能力暂未开放挂载")
    return CandidatesResponse(items=[CandidateEntry(**entry) for entry in lister(db, membership, q, limit)])


def _list_kb_candidates(db: Session, membership: Membership, q: str | None, limit: int) -> list[dict]:
    statement = _visible_knowledge(
        select(KnowledgeBase).where(KnowledgeBase.tenant_id == membership.tenant_id, KnowledgeBase.deleted_at.is_(None)),
        membership,
        db,
    )
    if q and q.strip():
        pattern = f"%{q.strip().lower()}%"
        statement = statement.where(
            func.lower(KnowledgeBase.name).like(pattern) | func.lower(KnowledgeBase.description).like(pattern)
        )
    items: list[dict] = []
    for kb in db.scalars(statement.order_by(func.lower(KnowledgeBase.name), KnowledgeBase.id)):
        stats = content_stats(db, kb)
        if derive_status(kb, stats) != "indexed":
            continue
        items.append(
            {"id": kb.id, "kind": "kb", "name": kb.name, "kb_type": kb.kb_type, "documents": stats.documents, "faqs": stats.faqs}
        )
        if len(items) >= limit:
            break
    return items


CANDIDATE_LISTERS["kb"] = _list_kb_candidates


# Other modules add what depends on an assistant (portal apps T-140): fn(db, assistant) -> [DependencyEntry].
DEPENDENCY_PROVIDERS: list[Callable[[Session, Assistant], list[DependencyEntry]]] = []


@router.get("/assistants/{assistant_id}/dependencies", response_model=DependenciesResponse)
def assistant_dependencies(
    assistant_id: UUID,
    membership: Membership = Depends(require_tenant_permission("assistants:view")),
    db: Session = Depends(get_db),
) -> DependenciesResponse:
    """AST-070: what depends on this agent — channels bound to it, agents that delegate to it, flows that call it, and
    whatever other modules register (portal apps). Read only."""
    assistant = _load(db, membership, assistant_id)
    items = [
        DependencyEntry(kind="channel", id=channel.id, name=channel.name, status=channel.status, detail=channel.type)
        for channel in db.scalars(
            select(ChannelInstance).where(
                ChannelInstance.assistant_id == assistant.id, ChannelInstance.tenant_id == assistant.tenant_id
            ).order_by(ChannelInstance.name, ChannelInstance.id)
        )
    ]
    for ref in references_to(db, assistant.tenant_id, "assistant", assistant.id):
        items.append(
            DependencyEntry(
                kind="assistant" if ref.type == "assistant" else "flow", id=ref.id, name=ref.name, status=ref.status,
                detail=",".join(sorted(ref.relations)),
            )
        )
    for provider in DEPENDENCY_PROVIDERS:
        items.extend(provider(db, assistant))
    return DependenciesResponse(items=items)
