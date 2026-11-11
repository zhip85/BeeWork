from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base
from beework.models.mixins import SoftDeleteMixin, TenantOwnedMixin, TimestampMixin, VersionedMixin


class Capability(Base, TenantOwnedMixin, TimestampMixin, VersionedMixin, SoftDeleteMixin):
    """What the three capability types share (PRD 3.5, 4.7): Skill, MCP connection and API plugin.

    ``status``: ``draft`` | ``available`` | ``disabled`` (state machine 5.1). The type-specific configuration lives in
    ``mcp_connections`` (T-072), ``skills`` (T-074) and ``api_plugins`` (T-073)."""

    __tablename__ = "capabilities"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_capabilities_id_tenant_id"),
        Index("ix_capabilities_tenant_kind", "tenant_id", "kind", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)  # skill | mcp | api
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    created_by: Mapped[str | None] = mapped_column(String(64))


class McpConnection(Base):
    """An MCP server a tenant connects (PRD 3.5.4). Credentials and tokens are encrypted; responses only show masks."""

    __tablename__ = "mcp_connections"
    __table_args__ = (
        ForeignKeyConstraint(
            ["capability_id", "tenant_id"], ["capabilities.id", "capabilities.tenant_id"],
            ondelete="CASCADE", name="fk_mcp_connections_capability_same_tenant",
        ),
    )

    capability_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    transport: Mapped[str] = mapped_column(String(16), nullable=False, default="streamable_http")
    url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    auth_type: Mapped[str] = mapped_column(String(8), nullable=False, default="none")  # none | bearer | oauth
    secret_encrypted: Mapped[str | None] = mapped_column(String(2000))  # the bearer token
    secret_last4: Mapped[str | None] = mapped_column(String(4))
    # OAuth 2.1 (authorization code + PKCE): the endpoints and client are entered by the administrator.
    oauth_authorize_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    oauth_token_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    oauth_client_id: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    oauth_client_secret_encrypted: Mapped[str | None] = mapped_column(String(2000))
    oauth_client_secret_last4: Mapped[str | None] = mapped_column(String(4))
    oauth_scopes: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    oauth_access_encrypted: Mapped[str | None] = mapped_column(String(4000))
    oauth_refresh_encrypted: Mapped[str | None] = mapped_column(String(4000))
    oauth_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Tools the server offered at the last test, and the ones the administrator ticked (only those are visible).
    tools: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    selected_tools: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_test_error: Mapped[str | None] = mapped_column(String(300))
    # Health (CAP-032): a failing connection is flagged, never disabled automatically.
    health: Mapped[str] = mapped_column(String(8), nullable=False, default="unknown")  # unknown | ok | error
    health_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    health_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
