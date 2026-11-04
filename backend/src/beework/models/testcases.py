from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKeyConstraint, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base, utc_now
from beework.models.mixins import TenantOwnedMixin, TimestampMixin, VersionedMixin


class AssistantTestCase(Base, TenantOwnedMixin, TimestampMixin, VersionedMixin):
    """A regression / safety / knowledge case of an assistant (PRD 4.4 TestCase, AST-071).

    ``status`` and the ``last_*`` columns are written only by test runs, never by the API clients.
    """

    __tablename__ = "assistant_test_cases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["assistant_id", "tenant_id"],
            ["assistants.id", "assistants.tenant_id"],
            ondelete="CASCADE",
            name="fk_assistant_test_cases_assistant_same_tenant",
        ),
        Index("ix_assistant_test_cases_assistant", "assistant_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    assistant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    input: Mapped[str] = mapped_column(Text, nullable=False)
    expected: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False, default="regression")
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="not_run")  # not_run / passed / failed
    run_state: Mapped[str] = mapped_column(String(12), nullable=False, default="idle")  # idle / queued / running
    last_output: Mapped[str | None] = mapped_column(Text)
    last_reason: Mapped[str | None] = mapped_column(String(1000))
    last_run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    last_target: Mapped[str | None] = mapped_column(String(40))  # "draft" or "v1.0"
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(64))


class AssistantTestRun(Base, TenantOwnedMixin):
    """A batch of test case executions run by the worker; progress and per-case results are visible while it runs."""

    __tablename__ = "assistant_test_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["assistant_id", "tenant_id"],
            ["assistants.id", "assistants.tenant_id"],
            ondelete="CASCADE",
            name="fk_assistant_test_runs_assistant_same_tenant",
        ),
        Index("ix_assistant_test_runs_assistant", "assistant_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    assistant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # {"kind": "draft"} | {"kind": "version", "version_id": ...} | {"kind": "snapshot", "approval_id": ...}
    target: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    target_label: Mapped[str] = mapped_column(String(40), nullable=False, default="draft")
    case_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="queued")  # queued/running/succeeded/failed/cancelled
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    passed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # [{case_id, name, status, reason, output, run_id}] in execution order
    results: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approval_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))  # set for the pre-publish gate
    error: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
