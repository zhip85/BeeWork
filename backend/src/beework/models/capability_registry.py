from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base
from beework.models.mixins import TenantOwnedMixin, TimestampMixin, VersionedMixin


class CapabilityEntry(Base, TenantOwnedMixin, TimestampMixin, VersionedMixin):
    """One thing an agent can use, in the unified capability registry (T-160, 演进 PRD 23.2): an MCP tool, an API tool, a Skill, a
    Dify workflow / chatflow, a sub-agent or a knowledge base (FAQ included).

    The entry is a **derived layer**: its identity and the facts the source owns (name, description, input schema, whether it
    writes) are recomputed from the source whenever the source changes (``capability_registry``), while what only the
    registry holds — the intent description and examples that recall and the JEV classifier work from, whether a message may
    go straight to it (path A), and its timeout / retry policy — is edited here and survives the recomputation. The id is
    stable (matched by ``source_id`` + ``identifier``), and an entry whose source disappears is only marked ``removed_at``.
    Whether it can be used *now* is never stored: it is read from the source (``MOUNT_RESOLVERS``)."""

    __tablename__ = "capability_entries"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "source_id", "identifier", name="uq_capability_entries_source"),
        Index("ix_capability_entries_tenant_kind", "tenant_id", "kind"),
        Index("ix_capability_entries_source", "tenant_id", "source_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    # mcp_tool | api_tool | skill | dify_workflow | dify_chatflow | agent | kb
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)  # capability / flow connection / assistant / knowledge base
    identifier: Mapped[str] = mapped_column(String(120), nullable=False, default="")  # the tool or operation name; empty for single-entry sources
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    intent_description: Mapped[str] = mapped_column(String(500), nullable=False, default="")  # “用户想做什么时该用它”
    intent_examples: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # up to 10 example phrasings
    input_schema: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    writes: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    direct_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # may path A (L2) call it directly
    direct_output: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # path A returns its result as is
    timeout_ms: Mapped[int | None] = mapped_column(Integer)
    retry: Mapped[dict | None] = mapped_column(JSON)  # {max_attempts, backoff_ms, retry_on}
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # kind-specific facts, e.g. the source's name
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
