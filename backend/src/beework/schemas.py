from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class CreateTenantRequest(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    slug: str = Field(pattern="^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$")


class TenantMembershipResponse(BaseModel):
    id: UUID
    name: str
    slug: str
    role: str


class PlatformTenantResponse(BaseModel):
    id: UUID
    name: str
    slug: str
    status: str


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


class ChangeRoleRequest(BaseModel):
    role: str = Field(pattern="^(tenant_admin|builder|operator|approver|member)$")


class MemberListResponse(BaseModel):
    tenant_id: UUID
    members: list[MemberResponse]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
