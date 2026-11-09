from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework.api.assistants import MODEL_ENVIRONMENT, _enabled_chat_models, _load
from beework.api.knowledge import _visible as _visible_knowledge
from beework.assistant_checks import check_assistant
from beework.capabilities import MOUNT_RESOLVERS, resolve_mounts
from beework.db import get_db
from beework.knowledge import content_stats, derive_status
from beework.models import Membership
from beework.models.knowledge import KnowledgeBase
from beework.rbac import require_tenant_permission
from beework.schemas.assistant_checks import (
    CandidateEntry,
    CandidatesResponse,
    CheckResponse,
    MountEntry,
    MountGroup,
    MountsResponse,
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


@router.get("/capabilities/candidates", response_model=CandidatesResponse)
def mount_candidates(
    kind: str = Query(default="kb", pattern="^kb$"),
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=100),
    membership: Membership = Depends(require_tenant_permission("assistants:edit")),
    db: Session = Depends(get_db),
) -> CandidatesResponse:
    """What can be mounted (the capability picker, AST-022): only knowledge bases that are already indexed (RUL-010),
    limited to the ones the caller may see."""
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
    items: list[CandidateEntry] = []
    for kb in db.scalars(statement.order_by(func.lower(KnowledgeBase.name), KnowledgeBase.id)):
        stats = content_stats(db, kb)
        if derive_status(kb, stats) != "indexed":
            continue
        items.append(
            CandidateEntry(
                id=kb.id, kind="kb", name=kb.name, kb_type=kb.kb_type,
                documents=stats.documents, faqs=stats.faqs,
            )
        )
        if len(items) >= limit:
            break
    return CandidatesResponse(items=items)
