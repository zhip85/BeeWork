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
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base, utc_now


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(24), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    price_text: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class PlanVersion(Base):
    """Immutable plan definition; edits publish a new version (PLT-085)."""

    __tablename__ = "plan_versions"
    __table_args__ = (UniqueConstraint("plan_id", "version", name="uq_plan_versions_plan_version"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    plan_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("plans.id"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    entitlements: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    quotas: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    overage_policy: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class TenantSubscription(Base):
    """Per-tenant snapshot of a plan version; history is kept, one row is current."""

    __tablename__ = "tenant_subscriptions"
    __table_args__ = (
        Index(
            "uq_tenant_subscriptions_current",
            "tenant_id",
            unique=True,
            postgresql_where=text("is_current"),
            sqlite_where=text("is_current"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("plan_versions.id"), nullable=False, index=True
    )
    entitlements: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    quotas: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    overage_policy: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    billing_cycle: Mapped[str] = mapped_column(String(16), nullable=False, default="monthly")
    renews_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    addons: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
