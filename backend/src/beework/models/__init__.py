import hashlib
import json
from contextvars import ContextVar
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    event,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, validates
from sqlalchemy.types import Uuid

audit_source_ip: ContextVar[str | None] = ContextVar("audit_source_ip", default=None)

AUDIT_ACTION_DESCRIPTIONS = {
    "tenant.created": "创建企业租户",
    "tenant.status_changed": "变更企业状态",
    "tenant.lifecycle_requested": "发起租户生命周期复核",
    "tenant.lifecycle_rejected": "驳回租户生命周期操作",
    "membership.invited": "邀请成员",
    "membership.invitation_accepted": "接受成员邀请",
    "membership.invitation_resent": "重新发送成员邀请",
    "membership.invitation_revoked": "撤销成员邀请",
    "membership.removed": "移除成员",
    "membership.role_changed": "变更成员角色",
    "membership.organization_changed": "变更成员所属组织",
    "role.created": "创建角色",
    "role.updated": "更新角色",
    "role.deleted": "删除角色",
    "organization.created": "创建组织",
    "organization.renamed": "重命名组织",
    "organization.deleted": "删除组织",
    "auth.login": "登录成功",
    "auth.login_failed": "登录失败",
    "auth.logout": "退出登录",
    "audit.exported": "导出审计日志",
    "model_catalog.created": "新增模型目录项",
    "model_catalog.updated": "更新模型目录项",
    "model_catalog.disabled": "停用模型目录项",
    "model_environment.updated": "更新模型环境配置",
    "model_environment.published": "发布模型环境配置",
    "model_catalog.tested": "测试模型连通性",
    "model_environment.tested": "测试环境模型调用",
    "job.retried": "重试后台任务",
    "assistant.created": "创建助理",
    "assistant.draft_saved": "保存助理草稿",
    "assistant.updated": "更新助理草稿",
    "assistant.copied": "复制助理",
    "assistant.disabled": "停用助理",
    "assistant.enabled": "启用助理",
    "assistant.deleted": "删除助理",
    "assistant.run_started": "发起助理调试运行",
    "compliance.updated": "更新数据与隐私设置",
    "conversation.retention_purged": "清理过期会话",
    "assistant.run_cancelled": "取消助理运行",
}


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    seat_limit: Mapped[int] = mapped_column(nullable=False, default=10)
    plan: Mapped[str] = mapped_column(String(24), nullable=False, default="standard")
    region: Mapped[str] = mapped_column(String(32), nullable=False, default="ap_singapore")
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    locale: Mapped[str | None] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PlatformOperationRequest(Base):
    """A high-risk platform operation awaiting a second administrator (PLT-052)."""

    __tablename__ = "platform_operation_requests"
    __table_args__ = (
        Index("ix_platform_operation_requests_status_created", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    operation: Mapped[str] = mapped_column(String(48), nullable=False)
    target_tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    from_status: Mapped[str] = mapped_column(String(24), nullable=False)
    to_status: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    requested_by: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    reviewed_by: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    review_comment: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlatformModel(Base):
    __tablename__ = "platform_models"
    __table_args__ = (
        Index("ix_platform_models_provider_region_type", "provider", "region", "model_type"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    display_name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model_type: Mapped[str] = mapped_column(String(24), nullable=False)
    availability: Mapped[str] = mapped_column(String(24), nullable=False)
    input_price_per_million: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False
    )
    output_price_per_million: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False
    )
    input_points_per_thousand: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False
    )
    output_points_per_thousand: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="healthy")
    provider_model_id: Mapped[str] = mapped_column(String(160), nullable=False)
    endpoint_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    encrypted_api_key: Mapped[str] = mapped_column(String(4096), nullable=False)
    api_key_last_four: Mapped[str] = mapped_column(String(4), nullable=False)
    api_version: Mapped[str | None] = mapped_column(String(80))
    region: Mapped[str] = mapped_column(String(32), nullable=False)
    context_window: Mapped[int | None] = mapped_column(Integer)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer)
    default_temperature: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    rpm_limit: Mapped[int | None] = mapped_column(Integer)
    # Registration state in the AI gateway (LiteLLM): not_configured / synced / failed.
    gateway_sync_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="not_configured"
    )
    gateway_error: Mapped[str | None] = mapped_column(String(500))
    gateway_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fallback_model_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("platform_models.id", ondelete="SET NULL"),
        nullable=True,
    )
    allowlist: Mapped[list["PlatformModelTenant"]] = relationship(
        back_populates="model",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class PlatformModelTenant(Base):
    __tablename__ = "platform_model_tenant_allowlist"
    __table_args__ = (Index("ix_platform_model_tenant_allowlist_tenant_id", "tenant_id"),)

    model_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("platform_models.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    model: Mapped[PlatformModel] = relationship(back_populates="allowlist")


class TenantModelEnvironment(Base):
    __tablename__ = "tenant_model_environments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "environment", name="uq_tenant_model_environment"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    # LiteLLM team + virtual key that scope this environment's model access.
    gateway_team_id: Mapped[str | None] = mapped_column(String(80))
    gateway_key_encrypted: Mapped[str | None] = mapped_column(String(4096))
    gateway_sync_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="not_configured"
    )
    gateway_error: Mapped[str | None] = mapped_column(String(500))
    gateway_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TenantModelSelection(Base):
    __tablename__ = "tenant_model_selections"
    __table_args__ = (
        UniqueConstraint("environment_id", "model_id", name="uq_environment_model_selection"),
        Index(
            "uq_tenant_model_default_type",
            "environment_id",
            "model_type",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default = 1"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    environment_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenant_model_environments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    model_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("platform_models.id", ondelete="RESTRICT"), nullable=False
    )
    model_type: Mapped[str] = mapped_column(String(24), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_organizations_id_tenant_id"),
        UniqueConstraint(
            "tenant_id", "parent_id", "name", name="uq_organizations_tenant_parent_name"
        ),
        ForeignKeyConstraint(
            ["parent_id", "tenant_id"],
            ["organizations.id", "organizations.tenant_id"],
            ondelete="RESTRICT",
            name="fk_organizations_parent_same_tenant",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TenantRole(Base):
    __tablename__ = "tenant_roles"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_tenant_roles_id_tenant_id"),
        UniqueConstraint("tenant_id", "name", name="uq_tenant_roles_tenant_name"),
        ForeignKeyConstraint(
            ["scope_organization_id", "tenant_id"],
            ["organizations.id", "organizations.tenant_id"],
            ondelete="RESTRICT",
            name="fk_tenant_roles_scope_organization_same_tenant",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="tenant")
    scope_organization_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
        index=True,
    )
    # Assistant-scoped roles (SET-022) only apply to these assistants; empty for other scopes.
    scope_assistant_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    permissions: Mapped[dict[str, list[str]]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "tenant_id"],
            ["organizations.id", "organizations.tenant_id"],
            ondelete="RESTRICT",
            name="fk_memberships_organization_same_tenant",
        ),
        ForeignKeyConstraint(
            ["custom_role_id", "tenant_id"],
            ["tenant_roles.id", "tenant_roles.tenant_id"],
            ondelete="RESTRICT",
            name="fk_memberships_custom_role_same_tenant",
        ),
        UniqueConstraint("tenant_id", "user_id", name="uq_memberships_tenant_user"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    custom_role_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (UniqueConstraint("token_id"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    token_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MemberInvitation(Base):
    __tablename__ = "member_invitations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "tenant_id"],
            ["organizations.id", "organizations.tenant_id"],
            ondelete="RESTRICT",
            name="fk_member_invitations_organization_same_tenant",
        ),
        ForeignKeyConstraint(
            ["custom_role_id", "tenant_id"],
            ["tenant_roles.id", "tenant_roles.tenant_id"],
            ondelete="RESTRICT",
            name="fk_member_invitations_custom_role_same_tenant",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    custom_role_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    organization_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    invited_by: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_tenant_created_at", "tenant_id", "created_at"),
        UniqueConstraint(
            "chain_scope",
            "chain_sequence",
            name="uq_audit_events_chain_scope_sequence",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="tenant")
    chain_scope: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    chain_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    previous_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tenant_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    actor_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="security")
    result: Mapped[str] = mapped_column(String(24), nullable=False, default="success")
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    source_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    details: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    @validates("action")
    def set_audit_metadata(self, _key: str, action: str) -> str:
        if action.startswith(("membership.", "role.", "organization.")):
            self.category = "members_permissions"
        elif action.startswith("assistant."):
            self.category = "assistants"
        else:
            self.category = "security"
        self.description = AUDIT_ACTION_DESCRIPTIONS.get(action, action)
        return action


class AuditChainCheckpoint(Base):
    __tablename__ = "audit_chain_checkpoints"

    chain_scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    through_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    through_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


def _audit_event_hash(event: AuditEvent) -> str:
    created_at = event.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    payload = {
        "id": str(event.id),
        "scope": event.scope,
        "chain_scope": event.chain_scope,
        "chain_sequence": event.chain_sequence,
        "previous_hash": event.previous_hash,
        "tenant_id": str(event.tenant_id) if event.tenant_id else None,
        "actor_user_id": str(event.actor_user_id) if event.actor_user_id else None,
        "target_user_id": str(event.target_user_id) if event.target_user_id else None,
        "action": event.action,
        "category": event.category,
        "result": event.result,
        "description": event.description,
        "source_ip": event.source_ip,
        "details": event.details,
        "created_at": created_at.astimezone(UTC).isoformat(),
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _lock_audit_chain(connection, chain_scope: str) -> None:
    connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
        {"scope": chain_scope},
    )


@event.listens_for(AuditEvent, "before_insert")
def capture_audit_source_ip(_mapper, _connection, target: AuditEvent) -> None:
    if target.source_ip is None:
        target.source_ip = audit_source_ip.get()
    details = target.details or {}
    context = details.get("email") or details.get("name") or details.get("slug")
    description = AUDIT_ACTION_DESCRIPTIONS.get(target.action, target.action)
    if context:
        description = f"{description}：{context}"
    target.description = description[:500]


@event.listens_for(Session, "before_flush")
def prevent_audit_event_mutation(session, _flush_context, _instances) -> None:
    if any(isinstance(item, AuditEvent) for item in session.deleted):
        raise ValueError("Audit events are append-only")
    if any(isinstance(item, AuditEvent) for item in session.dirty):
        raise ValueError("Audit events are append-only")
    new_events = [
        item
        for item in session.new
        if isinstance(item, AuditEvent) and item.event_hash is None
    ]
    if not new_events:
        return

    for audit_event in new_events:
        audit_event.id = audit_event.id or uuid4()
        audit_event.created_at = audit_event.created_at or utc_now()
        audit_event.category = audit_event.category or "security"
        audit_event.result = audit_event.result or "success"
        audit_event.details = audit_event.details or {}
        audit_event.source_ip = audit_event.source_ip or audit_source_ip.get()
        details = audit_event.details
        context = details.get("email") or details.get("name") or details.get("slug")
        description = AUDIT_ACTION_DESCRIPTIONS.get(
            audit_event.action, audit_event.action
        )
        if context:
            description = f"{description}：{context}"
        audit_event.description = description[:500]
        if audit_event.tenant_id is None:
            audit_event.scope = "platform"
            audit_event.chain_scope = "platform"
        else:
            audit_event.scope = "tenant"
            audit_event.chain_scope = f"tenant:{audit_event.tenant_id}"

    scopes = sorted({item.chain_scope for item in new_events})
    if session.bind and session.bind.dialect.name == "postgresql":
        connection = session.connection()
        for scope in scopes:
            _lock_audit_chain(connection, scope)

    for scope in scopes:
        latest = session.scalar(
            select(AuditEvent)
            .where(
                AuditEvent.chain_scope == scope,
                AuditEvent.chain_sequence.is_not(None),
            )
            .order_by(AuditEvent.chain_sequence.desc())
            .limit(1)
            .with_for_update()
        )
        checkpoint = session.get(AuditChainCheckpoint, scope)
        sequence = (
            latest.chain_sequence
            if latest
            else checkpoint.through_sequence if checkpoint else 0
        )
        previous_hash = (
            latest.event_hash if latest else checkpoint.through_hash if checkpoint else None
        )
        for audit_event in sorted(
            (item for item in new_events if item.chain_scope == scope),
            key=lambda item: (item.created_at, str(item.id)),
        ):
            sequence += 1
            audit_event.chain_sequence = sequence
            audit_event.previous_hash = previous_hash
            audit_event.event_hash = _audit_event_hash(audit_event)
            previous_hash = audit_event.event_hash


# Domain model modules. Each new business domain adds `models/<domain>.py`
# (importing Base / utc_now from this package) and registers it here so that
# Alembic autogenerate and `Base.metadata.create_all` see its tables.
# from beework.models import assistants  # noqa: E402,F401  (example)
from beework.models import assistants, conversations, jobs, plans, runtime  # noqa: E402,F401
