"""Channel experience: four layers, one merge (T-042, CHN-030–035, PLT-101).

``platform`` (``channel_types.default_experience``) → ``tenant`` (``tenant_channel_styles``) → ``assistant`` (``draft_config.channel_experience[type]``,
with the assistant's greeting as its welcome text unless it sets one) → ``instance`` (``channel_instances.experience``). Every layer stores only
the fields that differ from the layer above; an absent key inherits. ``resolve`` returns the value of every applicable field and the layer
it came from, so the editor can show a source badge and a “恢复继承” button.

A published channel keeps the values frozen in its snapshot (CHN-035 / 8.3#9: changes need a new publish); nothing here is read at run time.
"""

from dataclasses import dataclass
from typing import Any

from beework.errors import ApiError

LAYERS = ("platform", "tenant", "assistant", "instance")
RADII = (0, 8, 12, 16)
THEMES = ("light", "dark", "auto")
ENTRIES = {
    "website": ("bottom-right", "bottom-left"),
    "h5": ("fullscreen", "drawer"),
    "whatsapp": ("conversation",),
    "wechat": ("official-account",),
    "open-api": ("endpoint",),
}
SWITCHES = ("quick_replies", "cards", "tables", "forms", "streaming")
MAX_WELCOME = 200


@dataclass(frozen=True)
class Field:
    key: str
    kind: str  # color | choice | text | switch
    options: tuple[Any, ...] = ()


def fields_for(channel_type: str) -> list[Field]:
    """The settings that apply to a type (CHN-033: the open API has no colour, radius, entry or welcome text)."""
    visual = channel_type != "open-api"
    fields: list[Field] = []
    if visual:
        fields += [Field("primary_color", "color"), Field("theme", "choice", THEMES), Field("radius", "choice", RADII), Field("entry", "choice", ENTRIES[channel_type]), Field("welcome", "text")]
    fields += [Field(key, "switch") for key in SWITCHES]
    return fields


def _check_color(value: Any) -> str:
    import re

    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError("主色应为 #RRGGBB 格式")
    return value.lower()


def validate_layer(channel_type: str, values: dict[str, Any], *, allow: tuple[str, ...] = ()) -> dict[str, Any]:
    """The cleaned overrides of one layer. Unknown or non-applicable fields are an error (422); ``allow`` lists extra keys a layer may carry
    (the instance layer keeps the agent's display name)."""
    known = {f.key: f for f in fields_for(channel_type)}
    clean: dict[str, Any] = {}
    for key, value in values.items():
        if value is None or value == "":
            continue  # an empty value is “inherit”
        if key in allow:
            if not isinstance(value, str) or len(value) > 50:
                raise ApiError(422, "experience.invalid", f"{key} 应是不超过 50 个字符的文本")
            clean[key] = value.strip()
            continue
        field = known.get(key)
        if field is None:
            raise ApiError(422, "experience.invalid", f"“{channel_type}”渠道没有体验项 {key}")
        try:
            if field.kind == "color":
                clean[key] = _check_color(value)
            elif field.kind == "choice":
                if value not in field.options:
                    raise ValueError(f"{key} 只能是 {' / '.join(map(str, field.options))}")
                clean[key] = value
            elif field.kind == "text":
                if not isinstance(value, str) or len(value.strip()) > MAX_WELCOME:
                    raise ValueError(f"欢迎语不能超过 {MAX_WELCOME} 个字符")
                clean[key] = value.strip()
            else:
                if not isinstance(value, bool):
                    raise ValueError(f"{key} 应是开关")
                clean[key] = value
        except ValueError as exc:
            raise ApiError(422, "experience.invalid", str(exc)) from exc
    return clean


def resolve(
    channel_type: str,
    *,
    platform: dict[str, Any],
    tenant: dict[str, Any] | None = None,
    assistant: dict[str, Any] | None = None,
    instance: dict[str, Any] | None = None,
    greeting: str = "",
) -> tuple[dict[str, Any], dict[str, str]]:
    """``(values, sources)`` for every applicable field. The assistant's greeting is its welcome text unless its channel configuration has one."""
    layers: list[tuple[str, dict[str, Any]]] = [("platform", platform), ("tenant", tenant or {})]
    assistant_layer = dict(assistant or {})
    if greeting.strip() and "welcome" not in assistant_layer:
        assistant_layer["welcome"] = greeting.strip()
    layers += [("assistant", assistant_layer), ("instance", instance or {})]
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for field in fields_for(channel_type):
        for name, layer in layers:
            value = layer.get(field.key)
            if value is not None and value != "":
                values[field.key] = value
                sources[field.key] = name
    return values, sources
