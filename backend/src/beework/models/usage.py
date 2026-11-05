from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base, utc_now
from beework.models.mixins import TenantOwnedMixin

# Stand-in for "unknown" so the natural key of ``usage_records`` stays NOT NULL and uniquely indexable.
UNKNOWN_ID = UUID(int=0)


class UsageRecord(Base, TenantOwnedMixin):
    """Daily usage of one tenant (PRD 4.12), aggregated from the gateway's spend logs (T-011)."""

    __tablename__ = "usage_records"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "day", "environment", "model_id", "assistant_id", "source",
            name="uq_usage_records_key",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    day: Mapped[date] = mapped_column(Date, nullable=False, index=True)  # UTC calendar day
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    model_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, default=UNKNOWN_ID)
    assistant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, default=UNKNOWN_ID)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_in: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    points: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, default=Decimal(0))
    spend_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False, default=Decimal(0))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class UsageSeen(Base):
    """Gateway request ids already counted; makes every pull idempotent. Pruned once outside the re-read window."""

    __tablename__ = "usage_seen"

    request_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)


class UsageSyncState(Base):
    __tablename__ = "usage_sync_state"

    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    cursor: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))


class TenantBudget(Base):
    """Tenant-set monthly point budget (soft limit, alerts only) and its alert settings (SET-082)."""

    __tablename__ = "tenant_budgets"

    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    monthly_points: Mapped[int] = mapped_column(Integer, nullable=False, default=50_000)
    thresholds: Mapped[list] = mapped_column(JSON, nullable=False, default=lambda: [80, 100])
    recipients: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    pause_non_production: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    paused_month: Mapped[str | None] = mapped_column(String(7))  # "YYYY-MM" in which the 100% pause fired
    updated_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class UsageAlert(Base, TenantOwnedMixin):
    """One row per tenant, month and threshold: the unique key is what makes an alert fire only once."""

    __tablename__ = "usage_alerts"
    __table_args__ = (UniqueConstraint("tenant_id", "month", "threshold", name="uq_usage_alerts_once"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    month: Mapped[str] = mapped_column(String(7), nullable=False)
    threshold: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
