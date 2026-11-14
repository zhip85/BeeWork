from typing import Any

from pydantic import BaseModel, Field


class ExperienceWrite(BaseModel):
    """The overrides of one layer. A key that is missing (or empty) inherits; ``row_version`` guards the agent and the channel."""

    values: dict[str, Any] = Field(default_factory=dict, max_length=20)
    row_version: int | None = None


class ExperienceField(BaseModel):
    key: str
    kind: str  # color | choice | text | switch
    options: list[Any]


class ExperienceView(BaseModel):
    """Everything the editor shows for one type at one place: the field list, each layer's own overrides and the merged result."""

    channel_type: str
    channel_type_name: str
    fields: list[ExperienceField]
    layers: dict[str, dict[str, Any]]  # platform | tenant | assistant | instance -> that layer's own values
    values: dict[str, Any]  # merged
    sources: dict[str, str]
    greeting: str  # the agent's greeting: its welcome text unless its channel settings have one (the editor merges the same way)
    row_version: int | None  # of the agent / channel when one is addressed
