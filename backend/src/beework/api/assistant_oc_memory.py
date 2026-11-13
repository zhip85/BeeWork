"""OC · memory (T-056): what the tenant's plan allows, for the memory panel (OC-020 – OC-022, PLT-081)."""

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from beework import memory
from beework.api.assistants import _load
from beework.db import get_db
from beework.models import Membership
from beework.rbac import require_tenant_permission

router = APIRouter()


class MemoryInfo(BaseModel):
    tier: str  # short | short_long | all_cross_assistant
    long_term_allowed: bool
    shared_profile_allowed: bool


@router.get("/assistants/{assistant_id}/oc/memory/info", response_model=MemoryInfo)
def memory_info(
    assistant_id: UUID,
    membership: Membership = Depends(require_tenant_permission("orchestration:view")),
    db: Session = Depends(get_db),
) -> MemoryInfo:
    """Which kinds of memory the plan has: the standard plan only the conversation window, the professional plan long-term memory too, the
    enterprise plan also one UserProfile shared by every assistant."""
    _load(db, membership, assistant_id)
    tier = memory.memory_tier(db, membership.tenant_id)
    db.commit()
    return MemoryInfo(tier=tier, long_term_allowed=tier != "short", shared_profile_allowed=tier == "all_cross_assistant")
