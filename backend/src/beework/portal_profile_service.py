"""The employee's portal profile and personal-memory switches (T-145).

Personal memory has two switches that both must be on: the administrator's (portal settings) and the employee's own.
Either one off stops writing *and* reading memory for their portal conversations — on the server, in the runtime.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework import portal_service
from beework.models.conversations import EndUser
from beework.models.portal_profile import PortalProfile


def get_profile(db: Session, tenant_id: UUID, user_id: UUID, *, create: bool = False) -> PortalProfile | None:
    profile = db.scalar(select(PortalProfile).where(PortalProfile.tenant_id == tenant_id, PortalProfile.user_id == user_id))
    if profile is None and create:
        profile = PortalProfile(tenant_id=tenant_id, user_id=user_id, job_title="", personal_memory=True, task_notifications=True)
        db.add(profile)
        db.flush()
    return profile


def memory_allowed(db: Session, tenant_id: UUID, user_id: UUID) -> bool:
    if not portal_service.feature_enabled(db, tenant_id, "personal_memory"):
        return False
    profile = get_profile(db, tenant_id, user_id)
    return profile is None or profile.personal_memory


def memory_allowed_for_end_user(db: Session, tenant_id: UUID, end_user_id: UUID | None) -> bool:
    """For the runtime and the Worker, which know the end user. Only portal end users are subject to these switches."""
    if end_user_id is None:
        return True
    end_user = db.get(EndUser, end_user_id)
    if end_user is None or end_user.channel_type != "portal":
        return True
    try:
        return memory_allowed(db, tenant_id, UUID(end_user.external_id))
    except ValueError:
        return True
