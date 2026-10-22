from datetime import datetime
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class CreateTenantRequest(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    slug: str = Field(pattern="^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$")
    seat_limit: int = Field(ge=1, le=100000)
    plan: Literal["standard", "professional", "enterprise"] = "standard"
    region: Literal["us_east", "eu_west", "ap_singapore", "cn_shanghai"] = "ap_singapore"
    admin_email: EmailStr
    admin_name: str = Field(min_length=1, max_length=120)
    admin_password: str = Field(min_length=12, max_length=256)

    @field_validator("admin_name")
    @classmethod
    def strip_admin_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("企业管理员姓名不能为空")
        return value


class CreateOrganizationRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    parent_id: UUID | None = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("部门名称不能为空")
        return value


class RenameOrganizationRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("部门名称不能为空")
        return value


class OrganizationResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    parent_id: UUID | None
    name: str
    depth: int
    member_count: int
    child_count: int
    is_root: bool


class OrganizationListResponse(BaseModel):
    tenant_id: UUID
    organizations: list[OrganizationResponse]


class TenantMembershipResponse(BaseModel):
    id: UUID
    name: str
    slug: str
    role: str
    custom_role_id: UUID | None = None
    custom_role_name: str | None = None
    permissions: list[str] = Field(default_factory=list)
    organization_id: UUID | None = None
    tenant_status: str = "active"
    access_mode: Literal["full", "read_only"] = "full"


class PlatformTenantResponse(BaseModel):
    id: UUID
    name: str
    slug: str
    status: str
    seat_limit: int
    plan: str
    region: str
    member_count: int
    admin_email: EmailStr | None = None
    admin_name: str | None = None
    trial_ends_at: datetime | None = None
    available_actions: list["TenantLifecycleActionOption"] = Field(default_factory=list)


class TenantLifecycleActionOption(BaseModel):
    action: str
    label: str
    to_status: str
    requires_review: bool


class PlatformOverviewResponse(BaseModel):
    tenant_count: int
    active_tenant_count: int
    suspended_tenant_count: int
    member_count: int
    active_member_count: int
    platform_admin_count: int


class PlatformModelWriteRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    provider: Literal[
        "openai", "anthropic", "google", "azure_openai", "alibaba_qwen", "private"
    ]
    model_type: Literal["chat", "embedding", "rerank", "vision", "speech"]
    availability: Literal["all_plans", "professional_plus", "enterprise", "allowlist"]
    input_price_per_million: Decimal = Field(ge=0, max_digits=12, decimal_places=6)
    output_price_per_million: Decimal = Field(ge=0, max_digits=12, decimal_places=6)
    input_points_per_thousand: Decimal = Field(ge=0, max_digits=12, decimal_places=6)
    output_points_per_thousand: Decimal = Field(ge=0, max_digits=12, decimal_places=6)
    status: Literal["healthy", "canary", "disabled"]
    provider_model_id: str = Field(min_length=1, max_length=160)
    endpoint_url: str = Field(min_length=1, max_length=2048)
    api_key: str | None = Field(default=None, min_length=6, max_length=4096)
    api_version: str | None = Field(default=None, max_length=80)
    region: Literal["us_east", "eu_west", "ap_singapore", "cn_shanghai"]
    context_window: int | None = Field(default=None, ge=1, le=10_000_000)
    max_output_tokens: int | None = Field(default=None, ge=1, le=10_000_000)
    default_temperature: Decimal | None = Field(
        default=None, ge=0, le=2, max_digits=3, decimal_places=2
    )
    rpm_limit: int | None = Field(default=None, ge=1, le=10_000_000)
    fallback_model_id: UUID | None = None
    allowlist_tenant_ids: list[UUID] = Field(default_factory=list, max_length=500)

    @field_validator("display_name", "provider_model_id", "endpoint_url")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("该字段不能为空")
        return value

    @field_validator("api_version")
    @classmethod
    def strip_api_version(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("endpoint_url")
    @classmethod
    def require_https_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("模型接口地址必须是有效的 HTTPS URL")
        return value

    @model_validator(mode="after")
    def validate_allowlist(self):
        if self.availability == "allowlist" and not self.allowlist_tenant_ids:
            raise ValueError("白名单开放范围至少需要一个租户")
        if self.availability != "allowlist" and self.allowlist_tenant_ids:
            raise ValueError("只有白名单开放范围可以指定租户")
        return self


class PlatformModelResponse(BaseModel):
    id: UUID
    display_name: str
    provider: str
    model_type: str
    availability: str
    input_price_per_million: Decimal
    output_price_per_million: Decimal
    input_points_per_thousand: Decimal
    output_points_per_thousand: Decimal
    status: str
    provider_model_id: str
    endpoint_url: str
    credential_configured: bool
    api_key_masked: str
    api_version: str | None
    region: str
    context_window: int | None
    max_output_tokens: int | None
    default_temperature: Decimal | None
    rpm_limit: int | None
    fallback_model_id: UUID | None
    allowlist_tenant_ids: list[UUID]
    gateway_sync_status: str
    gateway_error: str | None
    gateway_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ModelTestRequest(BaseModel):
    message: str = Field(default="请用一句话介绍你自己。", min_length=1, max_length=2000)
    # Only verify gateway key + model allowlist (no provider call, no cost).
    access_only: bool = False


class TenantModelTestRequest(ModelTestRequest):
    model_id: UUID


class ModelTestResponse(BaseModel):
    ok: bool
    model: str
    content: str | None = None
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    access_only: bool
    error: str | None = None


class IntegrationStatus(BaseModel):
    key: str
    name: str
    purpose: str
    configured: bool
    reachable: bool | None
    detail: str | None = None


class PlatformModelListResponse(BaseModel):
    models: list[PlatformModelResponse]


class TenantModelEnvironmentWriteRequest(BaseModel):
    expected_version: int = Field(ge=0)
    enabled_model_ids: list[UUID] = Field(max_length=200)
    default_model_ids: dict[Literal["chat", "embedding", "rerank"], UUID | None]

    @model_validator(mode="after")
    def validate_selection_ids(self):
        if len(self.enabled_model_ids) != len(set(self.enabled_model_ids)):
            raise ValueError("启用模型列表不能包含重复项")
        if set(self.default_model_ids) != {"chat", "embedding", "rerank"}:
            raise ValueError("必须为对话、向量化和重排序分别指定默认模型")
        for model_id in self.default_model_ids.values():
            if model_id is not None and model_id not in self.enabled_model_ids:
                raise ValueError("默认模型必须在本环境启用")
        return self


class TenantModelOption(BaseModel):
    id: UUID
    display_name: str
    provider: str
    model_type: str
    region: str
    input_price_per_million: Decimal


class TenantModelEnvironmentResponse(BaseModel):
    tenant_id: UUID
    environment: str
    available_environments: list[str]
    plan: str
    region: str
    version: int
    updated_at: datetime | None
    models: list[TenantModelOption]
    enabled_model_ids: list[UUID]
    default_model_ids: dict[str, UUID | None]
    unavailable_enabled_model_ids: list[UUID]
    gateway_sync_status: str = "not_configured"
    gateway_error: str | None = None


class TenantLifecycleRequest(BaseModel):
    action: Literal[
        "convert_to_active",
        "extend_trial",
        "suspend",
        "mark_grace",
        "record_payment",
        "make_read_only",
        "restore",
        "schedule_deletion",
        "restore_to_suspended",
        "delete_permanently",
    ]
    reason: str = Field(default="", max_length=500)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        return value.strip()


class ReviewPlatformOperationRequest(BaseModel):
    comment: str = Field(default="", max_length=500)


class PlatformOperationResponse(BaseModel):
    id: UUID
    operation: str
    operation_label: str
    target_tenant_id: UUID
    target_tenant_name: str
    from_status: str
    to_status: str
    reason: str
    status: Literal["pending", "succeeded", "rejected"]
    requested_by_name: str
    requested_by_id: UUID
    reviewed_by_name: str | None = None
    review_comment: str | None = None
    created_at: datetime
    expires_at: datetime
    reviewed_at: datetime | None = None


class TenantLifecycleResponse(BaseModel):
    tenant: "PlatformTenantResponse"
    operation: PlatformOperationResponse | None = None


class CurrentUserResponse(BaseModel):
    id: UUID
    email: EmailStr
    display_name: str
    is_platform_admin: bool
    memberships: list[TenantMembershipResponse]


class MemberResponse(BaseModel):
    id: UUID
    email: EmailStr
    display_name: str
    role: str
    is_active: bool
    status: str
    organization_id: UUID
    organization_name: str
    custom_role_id: UUID | None = None
    custom_role_name: str | None = None


class PermissionCatalogItem(BaseModel):
    key: str
    label: str
    actions: list[str]


class PermissionCatalogResponse(BaseModel):
    modules: list[PermissionCatalogItem]
    action_labels: dict[str, str]


class RoleResponse(BaseModel):
    key: str
    id: UUID | None
    name: str
    description: str
    scope: str
    scope_organization_id: UUID | None
    permissions: dict[str, list[str]]
    is_active: bool
    is_builtin: bool
    member_count: int = 0


class RoleListResponse(BaseModel):
    roles: list[RoleResponse]


class CreateRoleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    scope: str = Field(pattern="^(tenant|organization)$")
    scope_organization_id: UUID | None = None
    permissions: dict[str, list[str]]
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("角色名称不能为空")
        return value

    @field_validator("scope_organization_id")
    @classmethod
    def validate_scope_organization(cls, value: UUID | None, info):
        scope = info.data.get("scope")
        if scope == "organization" and value is None:
            raise ValueError("组织范围角色必须指定组织")
        if scope == "tenant" and value is not None:
            raise ValueError("租户范围角色不能指定组织")
        return value

    @field_validator("permissions")
    @classmethod
    def validate_permission_catalog(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        from beework.rbac import validate_permissions

        try:
            return validate_permissions(value)
        except ValueError as error:
            raise ValueError(str(error)) from error


class UpdateRoleRequest(CreateRoleRequest):
    pass


class ChangeRoleRequest(BaseModel):
    role: str = Field(pattern="^(tenant_admin|builder|operator|viewer|auditor|custom)$")
    custom_role_id: UUID | None = None

    @field_validator("custom_role_id")
    @classmethod
    def validate_custom_role(cls, value: UUID | None, info):
        if info.data.get("role") == "custom" and value is None:
            raise ValueError("请选择自定义角色")
        if info.data.get("role") != "custom" and value is not None:
            raise ValueError("内置角色不能指定自定义角色")
        return value


class ChangeMemberOrganizationRequest(BaseModel):
    organization_id: UUID


class CreateMemberInvitationsRequest(BaseModel):
    emails: list[EmailStr] = Field(min_length=1, max_length=50)
    role: str = Field(pattern="^(tenant_admin|builder|operator|viewer|auditor|custom)$")
    custom_role_id: UUID | None = None
    organization_id: UUID

    @field_validator("custom_role_id")
    @classmethod
    def validate_custom_role(cls, value: UUID | None, info):
        if info.data.get("role") == "custom" and value is None:
            raise ValueError("请选择自定义角色")
        if info.data.get("role") != "custom" and value is not None:
            raise ValueError("内置角色不能指定自定义角色")
        return value

    @field_validator("emails")
    @classmethod
    def normalize_unique_emails(cls, values: list[EmailStr]) -> list[str]:
        normalized = [str(email).strip().lower() for email in values]
        if len({str(email).lower() for email in normalized}) != len(normalized):
            raise ValueError("邀请邮箱不能重复")
        return normalized


class MemberInvitationResponse(BaseModel):
    id: UUID
    email: EmailStr
    role: str
    custom_role_id: UUID | None = None
    custom_role_name: str | None = None
    organization_id: UUID
    organization_name: str
    status: str
    expires_at: datetime


class MemberListResponse(BaseModel):
    tenant_id: UUID
    seat_limit: int
    seats_used: int
    members: list[MemberResponse]
    invitations: list[MemberInvitationResponse]


class AuditEventResponse(BaseModel):
    id: UUID
    category: str
    result: str
    created_at: datetime
    actor_name: str
    actor_email: EmailStr | None
    description: str
    source_ip: str | None
    details: dict[str, str]


class AuditEventListResponse(BaseModel):
    events: list[AuditEventResponse]
    total: int
    limit: int
    offset: int


class AuditIntegrityResponse(BaseModel):
    chain_scope: str
    is_valid: bool
    checked_events: int
    unchained_legacy_events: int
    through_sequence: int


class CreatedMemberInvitationResponse(MemberInvitationResponse):
    invitation_url: str


class CreateMemberInvitationsResponse(BaseModel):
    invitations: list[CreatedMemberInvitationResponse]
    seat_limit: int
    seats_used: int


class AcceptMemberInvitationRequest(BaseModel):
    token: str = Field(min_length=32, max_length=128)
    password: str = Field(min_length=12, max_length=256)
    display_name: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("姓名不能为空")
        return value


class ChangeMemberStatusRequest(BaseModel):
    status: str = Field(pattern="^(active|suspended)$")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
