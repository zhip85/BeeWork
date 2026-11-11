from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class ProfileResponse(BaseModel):
    display_name: str
    email: str
    department: str  # read-only, from the organization structure
    job_title: str
    default_app_id: UUID | None
    default_app_name: str | None
    can_edit: bool  # false when the administrator switched profile editing off
    login_method: str  # a description of how this account signs in
    memory_available: bool  # the administrator allows personal memory
    memory_enabled: bool  # the employee's own switch
    notifications_available: bool
    notifications_enabled: bool


class ProfileUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    job_title: str = Field(default="", max_length=60)
    default_app_id: UUID | None = None

    @field_validator("display_name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("显示名称不能为空")
        return value


class SwitchBody(BaseModel):
    enabled: bool


class MemoryEntry(BaseModel):
    id: UUID
    kind: str  # profile | memory | session
    content: str
    created_at: datetime


class MemoryResponse(BaseModel):
    enabled: bool
    items: list[MemoryEntry]


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)
