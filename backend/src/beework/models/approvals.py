from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base, utc_now
from beework.models.mixins import TenantOwnedMixin, TimestampMixin, VersionedMixin


class ApprovalPolicy(Base, TenantOwnedMixin, TimestampMixin, VersionedMixin):
    """One row per tenant and controlled change (SET-071, PRD 4.11); created lazily with the PRD defaults."""

    __tablename__ = "approval_policies"
    __table_args__ = (UniqueConstraint("tenant_id", "event", name="uq_approval_policies_tenant_event"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    event: Mapped[str] = mapped_column(String(24), nullable=False)
    approver_role: Mapped[str] = mapped_column(String(24), nullable=False)
    levels: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ApprovalRequest(Base, TenantOwnedMixin, TimestampMixin):
    """An approval request (PRD 4.11). Created only by the source operation, never by hand."""

    __tablename__ = "approval_requests"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_approval_requests_id_tenant_id"),
        Index("ix_approval_requests_tenant_status", "tenant_id", "status", "submitted_at"),
        # APR-002: at most one pending request per object and type, enforced by the database.
        Index(
            "uq_approval_requests_one_pending",
            "tenant_id",
            "object_type",
            "object_id",
            "type",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[str] = mapped_column(String(24), nullable=False)  # publish / capability_access / data_export / rule_change
    policy_event: Mapped[str] = mapped_column(String(24), nullable=False)
    object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    object_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    organization_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    change_diff: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    checks: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # What the registered action executor needs to apply the change once approved.
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    requester_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    # [{level, approver_role, approvers: [user id], status, decided_by, comment, decided_at}]
    levels: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    current_level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    apply_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    apply_error: Mapped[str | None] = mapped_column(String(2000))
    apply_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApprovalComment(Base, TenantOwnedMixin):
    __tablename__ = "approval_comments"
    __table_args__ = (Index("ix_approval_comments_request", "approval_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    approval_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("approval_requests.id", ondelete="CASCADE"), nullable=False)
    author_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    author_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
