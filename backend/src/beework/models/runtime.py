from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKeyConstraint, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base, utc_now
from beework.models.mixins import TenantOwnedMixin


class Run(Base, TenantOwnedMixin):
    """One execution of an assistant (debug / test / live / api); events live in ``run_events``."""

    __tablename__ = "runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["assistant_id", "tenant_id"],
            ["assistants.id", "assistants.tenant_id"],
            ondelete="CASCADE",
            name="fk_runs_assistant_same_tenant",
        ),
        Index("ix_runs_tenant_created", "tenant_id", "created_at"),
        Index("ix_runs_assistant_created", "assistant_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    assistant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # Set for runs of a published version (T-024); debug runs execute a draft snapshot instead.
    version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    draft_row_version: Mapped[int | None] = mapped_column(Integer)
    conversation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))  # T-026
    environment: Mapped[str] = mapped_column(String(16), nullable=False, default="production")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="debug")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    # The AssistantDSLv1 the run executes, frozen at creation so edits never change a queued run.
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    input: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    output: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(String(500))
    retrieval_mode: Mapped[str | None] = mapped_column(String(16))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    # Pipeline overhead (all stages minus model / retrieval calls) and per-stage wall time, for NFR-001.
    pipeline_ms: Mapped[int | None] = mapped_column(Integer)
    stage_ms: Mapped[dict | None] = mapped_column(JSON)
    error_stage: Mapped[str | None] = mapped_column(String(32))
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    trace_url: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
