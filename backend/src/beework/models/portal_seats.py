from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base
from beework.models.mixins import TenantOwnedMixin, TimestampMixin


class PortalSeatRule(Base, TenantOwnedMixin, TimestampMixin):
    """A department whose members (and its sub-departments') all hold an AI portal seat. A row exists only while the
    rule is on; turning it off deletes the row (T-150)."""

    __tablename__ = "portal_seat_rules"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "tenant_id"], ["organizations.id", "organizations.tenant_id"],
            ondelete="CASCADE", name="fk_portal_seat_rules_org_same_tenant",
        ),
        UniqueConstraint("tenant_id", "organization_id", name="uq_portal_seat_rules_org"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(64))


class PortalSeatAssignment(Base, TenantOwnedMixin, TimestampMixin):
    """One member's portal seat state beyond the department rules.

    ``status``: ``assigned`` (a manual / auto seat), ``requested`` (the employee asked, T-151) or ``none`` (no seat of
    its own — the row only keeps the last-login time and, while ``excluded``, stops a department rule from granting the
    member a seat again). Having no row means no individual seat state at all.
    """

    __tablename__ = "portal_seat_assignments"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_portal_seat_assignments_user"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="none")
    source: Mapped[str | None] = mapped_column(String(8))  # manual | auto
    excluded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    request_reason: Mapped[str | None] = mapped_column(Text)
    assigned_by: Mapped[str | None] = mapped_column(String(64))
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PortalSeatPolicy(Base, TenantOwnedMixin, TimestampMixin):
    """Per-tenant policy (one row): auto-assign on first sign-in, and how many idle days release a seat."""

    __tablename__ = "portal_seat_policy"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_portal_seat_policy_tenant"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    auto_assign: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    idle_days: Mapped[int | None] = mapped_column(Integer)  # 30 | 60 | 90 | None (never reclaim)
