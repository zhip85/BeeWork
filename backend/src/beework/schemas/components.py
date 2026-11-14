from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

ComponentType = Literal["card", "comparison", "timeline", "form"]
Mode = Literal["native", "text", "json", "off"]


class ComponentContent(BaseModel):
    name_zh: str = Field(min_length=1, max_length=80)
    name_en: str = Field(default="", max_length=80)
    type: ComponentType = "card"
    description: str = Field(default="", max_length=300)
    trigger_tag: str = Field(min_length=1, max_length=40)
    data_schema: str = Field(min_length=2, max_length=20000)
    sample_data: str = Field(default="{}", max_length=20000)
    compatibility: dict[str, Mode] = Field(default_factory=dict)

    @field_validator("name_zh", "name_en", "description")
    @classmethod
    def strip(cls, value: str) -> str:
        return value.strip()


class ComponentWrite(ComponentContent):
    status: Literal["enabled", "draft", "disabled"] = "draft"
    row_version: int | None = None


class TemplateWrite(ComponentContent):
    status: Literal["published", "draft", "deprecated"] = "draft"
    version: str = Field(default="0.1", pattern=r"^[0-9]+\.[0-9]+$")


class ComponentResponse(BaseModel):
    id: UUID
    name_zh: str
    name_en: str
    type: ComponentType
    description: str
    trigger_tag: str
    data_schema: str
    sample_data: str
    compatibility: dict[str, Mode]
    version: str
    status: Literal["enabled", "draft", "disabled"]
    template_id: UUID | None
    template_name: str | None
    template_version: str | None  # the version installed
    template_latest: str | None  # set when the template has a newer version (CHN-042 “模板已更新 vX”)
    used_by: list[str]  # names of channels whose published snapshot holds it (RUL-013)
    row_version: int
    can_delete: bool
    updated_at: datetime


class ComponentList(BaseModel):
    items: list[ComponentResponse]


class TemplateResponse(BaseModel):
    id: UUID
    name_zh: str
    name_en: str
    type: ComponentType
    description: str
    trigger_tag: str
    data_schema: str
    sample_data: str
    compatibility: dict[str, Mode]
    version: str
    status: Literal["published", "draft", "deprecated"]
    copies: int  # tenant copies (all tenants for the platform, this tenant for a tenant)
    updated_at: datetime


class TemplateList(BaseModel):
    items: list[TemplateResponse]


class SyncPreview(BaseModel):
    from_version: str
    to_version: str
    changes: list[dict]


class TypeDefaults(BaseModel):
    trigger_tag: str
    data_schema: str
    sample_data: str
    compatibility: dict[str, Mode]
