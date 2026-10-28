from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base, utc_now
from beework.models.mixins import SoftDeleteMixin, TenantOwnedMixin, TimestampMixin, VersionedMixin


class Assistant(Base, TenantOwnedMixin, TimestampMixin, VersionedMixin, SoftDeleteMixin):
    """An AI assistant (PRD 4.4). Editable configuration lives in ``draft_config`` (AssistantDraftConfig)."""

    __tablename__ = "assistants"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_assistants_id_tenant_id"),
        ForeignKeyConstraint(
            ["organization_id", "tenant_id"],
            ["organizations.id", "organizations.tenant_id"],
            ondelete="RESTRICT",
            name="fk_assistants_organization_same_tenant",
        ),
        Index("ix_assistants_tenant_status", "tenant_id", "status"),
        Index("ix_assistants_organization_id", "organization_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    category: Mapped[str] = mapped_column(String(24), nullable=False, default="customer_service")
    icon: Mapped[str | None] = mapped_column(String(40))
    color: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="own_org")
    draft_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    draft_version: Mapped[str] = mapped_column(String(16), nullable=False, default="0.1")
    # Set by T-024 when a version goes live; no foreign key until assistant_versions exists.
    live_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    template_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    owner_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_by: Mapped[str | None] = mapped_column(String(64))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AssistantVisibleOrganization(Base):
    """Organizations an assistant is visible to when ``visibility == "specified"``."""

    __tablename__ = "assistant_visible_organizations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "tenant_id"],
            ["organizations.id", "organizations.tenant_id"],
            ondelete="CASCADE",
            name="fk_assistant_visible_org_same_tenant",
        ),
        ForeignKeyConstraint(
            ["assistant_id", "tenant_id"],
            ["assistants.id", "assistants.tenant_id"],
            ondelete="CASCADE",
            name="fk_assistant_visible_assistant_same_tenant",
        ),
    )

    assistant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )


class AssistantVersion(Base, TenantOwnedMixin):
    """An immutable published version of an assistant (PRD 4.4). Updates are rejected by an ORM guard
    and, on PostgreSQL, by a trigger; the live version is ``assistants.live_version_id``."""

    __tablename__ = "assistant_versions"
    __table_args__ = (
        UniqueConstraint("assistant_id", "version_seq", name="uq_assistant_versions_seq"),
        UniqueConstraint("id", "tenant_id", name="uq_assistant_versions_id_tenant_id"),
        ForeignKeyConstraint(
            ["assistant_id", "tenant_id"],
            ["assistants.id", "assistants.tenant_id"],
            ondelete="CASCADE",
            name="fk_assistant_versions_assistant_same_tenant",
        ),
        Index("ix_assistant_versions_assistant", "assistant_id", "version_seq"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    assistant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[str] = mapped_column(String(16), nullable=False)  # "1.0", "1.1", ...
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="publish")  # publish | rollback
    source_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))  # rollback target
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    change_note: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    change_diff: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    approval_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))  # T-022
    published_by: Mapped[str | None] = mapped_column(String(64))
    published_by_user_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


@event.listens_for(AssistantVersion, "before_update")
def _forbid_version_update(_mapper, _connection, _target) -> None:
    raise ValueError("助理版本是不可变的，不能修改")
