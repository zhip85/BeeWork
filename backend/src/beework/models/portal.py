from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base
from beework.models.mixins import SoftDeleteMixin, TenantOwnedMixin, TimestampMixin, VersionedMixin


class PortalApp(Base, TenantOwnedMixin, TimestampMixin, VersionedMixin, SoftDeleteMixin):
    """An app the administrator puts in front of employees (演进 PRD 10.1). The underlying resource type is never shown
    to employees. ``source_id`` is nullable only so that legacy rows without a source still load (演进 PRD 10.2)."""

    __tablename__ = "portal_apps"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_portal_apps_id_tenant_id"),
        Index("ix_portal_apps_tenant_status", "tenant_id", "status"),
        Index("ix_portal_apps_source", "tenant_id", "source_type", "source_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_type: Mapped[str] = mapped_column(String(24), nullable=False)  # agent | chatflow_connection | workflow_connection
    source_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    category: Mapped[str] = mapped_column(String(24), nullable=False, default="")  # preset or custom text
    audience: Mapped[str] = mapped_column(String(8), nullable=False, default="all")  # all | orgs | admins
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default="chat")  # chat | form
    icon: Mapped[str] = mapped_column(String(40), nullable=False, default="sparkles")
    featured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="draft")  # draft | listed | delisted
    listed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    listed_by: Mapped[str | None] = mapped_column(String(64))
    delisted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delisted_reason: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[str | None] = mapped_column(String(64))


class PortalAppOrganization(Base):
    """The organizations an app is visible to when ``audience == "orgs"`` (an organization includes its children)."""

    __tablename__ = "portal_app_organizations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "tenant_id"], ["organizations.id", "organizations.tenant_id"],
            ondelete="CASCADE", name="fk_portal_app_org_same_tenant",
        ),
        ForeignKeyConstraint(
            ["app_id", "tenant_id"], ["portal_apps.id", "portal_apps.tenant_id"],
            ondelete="CASCADE", name="fk_portal_app_org_app_same_tenant",
        ),
    )

    app_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
