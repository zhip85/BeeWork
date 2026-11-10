from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base, utc_now
from beework.models.mixins import TenantOwnedMixin


class ToolInvocation(Base, TenantOwnedMixin):
    """One tool call made through the executor (T-071). Only metadata is kept — never arguments, results or
    credentials."""

    __tablename__ = "tool_invocations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_tool_invocations_idempotency"),
        Index("ix_tool_invocations_run", "run_id", "call_index"),
        Index("ix_tool_invocations_tenant_time", "tenant_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    assistant_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    call_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_name: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # mcp | api | workflow | skill | connection
    capability_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    is_write: Mapped[bool] = mapped_column(nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # ok | error | cancelled | denied
    error_code: Mapped[str | None] = mapped_column(String(64))
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    response_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
