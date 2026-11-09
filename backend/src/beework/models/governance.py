"""Platform governance data (T-050): platform PII rules, the security baseline and pending high-risk rule changes."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base, utc_now
from beework.models.mixins import TimestampMixin, VersionedMixin


class PlatformPiiRule(Base, TimestampMixin, VersionedMixin):
    """A PII rule every tenant inherits (PRD 3.16.2). ``enforced`` rules cannot be disabled by tenants or assistants."""

    __tablename__ = "platform_pii_rules"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    match_type: Mapped[str] = mapped_column(String(16), nullable=False, default="regex")  # regex | keyword | classifier | llm_judge
    pattern: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stage: Mapped[str] = mapped_column(String(8), nullable=False, default="both")  # both | input | output
    action: Mapped[str] = mapped_column(String(8), nullable=False, default="mask")  # mask | block | flag
    block_message: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    enforced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(nullable=False, default=0)


class SecurityBaselineRule(Base, TimestampMixin, VersionedMixin):
    """One row of the platform security baseline (PRD 3.16.3). PII rows are rule-set references (PLT-143)."""

    __tablename__ = "security_baseline_rules"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    category: Mapped[str] = mapped_column(String(20), nullable=False)  # pii | prompt_attack | content_safety | data_retention
    stage: Mapped[str] = mapped_column(String(8), nullable=False, default="both")  # both | input | output | data
    level: Mapped[str] = mapped_column(String(12), nullable=False, default="recommended")  # enforced | recommended
    scope: Mapped[str] = mapped_column(String(20), nullable=False, default="all")  # all | enterprise | region_cn | industry_finance
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="draft")  # enabled | draft
    action: Mapped[str] = mapped_column(String(8), nullable=False, default="flag")  # mask | block | flag
    match_type: Mapped[str] = mapped_column(String(16), nullable=False, default="keyword")
    pattern: Mapped[str] = mapped_column(Text, nullable=False, default="")  # retention rules: the number of days
    block_message: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    # PII rows reference platform PII rules instead of storing a pattern: {"kind": "all" | "enforced" | "rules", "rule_ids": [...]}.
    ruleset: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    sort_order: Mapped[int] = mapped_column(nullable=False, default=0)


class PlatformChangeRequest(Base):
    """A high-risk change to a platform rule that waits for a second administrator (PLT-071)."""

    __tablename__ = "platform_change_requests"
    __table_args__ = (Index("ix_platform_change_requests_status_created", "status", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # pii_rule | baseline_rule
    operation: Mapped[str] = mapped_column(String(8), nullable=False)  # create | update | delete
    object_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    object_name: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    base_row_version: Mapped[int | None] = mapped_column(nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    summary: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    reason: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")  # pending | succeeded | rejected | expired
    requested_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False)
    reviewed_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"), nullable=True)
    review_comment: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
