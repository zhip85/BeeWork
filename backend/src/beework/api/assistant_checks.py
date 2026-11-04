from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from beework.api.assistants import MODEL_ENVIRONMENT, _enabled_chat_models, _load
from beework.assistant_checks import check_assistant
from beework.capabilities import MOUNT_RESOLVERS, resolve_mounts
from beework.db import get_db
from beework.models import Membership
from beework.rbac import require_tenant_permission
from beework.schemas.assistant_checks import CheckResponse, MountEntry, MountGroup, MountsResponse
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
