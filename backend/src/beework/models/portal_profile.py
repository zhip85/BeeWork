from uuid import UUID, uuid4

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base
from beework.models.mixins import TenantOwnedMixin, TimestampMixin


class PortalProfile(Base, TenantOwnedMixin, TimestampMixin):
    """One employee's own portal preferences in one tenant (T-145). No row = the defaults."""

    __tablename__ = "portal_profiles"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_portal_profiles_member"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    job_title: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    default_app_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("portal_apps.id", ondelete="SET NULL"))
    personal_memory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)  # the employee's own switch
    task_notifications: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
