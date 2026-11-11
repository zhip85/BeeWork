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

from beework.models import Base, utc_now
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


class PlatformSkill(Base):
    """A Skill the platform provides to every tenant (CAP-009): read-only for tenants, maintained by platform admins."""

    __tablename__ = "platform_skills"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)  # e.g. "handoff"
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    content: Mapped[str] = mapped_column(String(20000), nullable=False, default="")  # the SKILL.md text
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active | disabled
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
    updated_by: Mapped[str | None] = mapped_column(String(64))


class Skill(Base):
    """A tenant's Skill (PRD 3.5.3). Built-in ones point at ``platform_skills`` and carry no package; self-built ones keep
    the uploaded package in the file store and must pass the security check before they can be enabled (CAP-026)."""

    __tablename__ = "skills"
    __table_args__ = (
        ForeignKeyConstraint(
            ["capability_id", "tenant_id"], ["capabilities.id", "capabilities.tenant_id"],
            ondelete="CASCADE", name="fk_skills_capability_same_tenant",
        ),
    )

    capability_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    platform_skill_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), index=True)  # set for built-in skills
    filename: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    package_key: Mapped[str | None] = mapped_column(String(300))
    package_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # bumps on every new file
    manifest: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # [{path, size}]
    has_scripts: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    security_status: Mapped[str] = mapped_column(String(10), nullable=False, default="pending")  # pending | running | passed | failed
    checks: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # [{key, status, issues: [{path, reason}]}]
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uploaded_by_user_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))  # files the enable request once checks pass
    enable_warning: Mapped[str | None] = mapped_column(String(300))  # why that request could not be filed
