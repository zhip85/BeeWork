"""Channel types (T-041, PLT-100–102, CHN-050/051).

The catalog below is the platform default of the five types (PRD 3.7.1). A ``channel_types`` row only exists once the platform
changed something (enabled flag, version, capability tags, default experience) and is laid over the catalog, so a database
without rows behaves like a fresh installation. Types cannot be created through the API: a type needs an adapter in code.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.entitlements import entitlement_value
from beework.models.channels import ChannelType

# Which types a plan's ``channels`` entitlement includes (PRD CHN-051: IM channels are not in the standard plan).
PLAN_TYPES = {
    "widget_api": ("website", "h5", "open-api"),
    "im": ("website", "h5", "open-api", "whatsapp", "wechat"),
    "all": ("website", "h5", "open-api", "whatsapp", "wechat"),
}

CATALOG: dict[str, dict[str, Any]] = {
    "website": {
        "name": "网站", "version": "1.0",
        "connection_params": [
            {"key": "allowed_origins", "label": "允许域名", "kind": "origins", "required": True},
            {"key": "sdk_key", "label": "SDK Key", "kind": "generated", "required": False},
        ],
        "capabilities": ["Markdown", "表格", "卡片", "表单", "流式"],
        "default_experience": {"primary_color": "#6558d9", "radius": 12, "welcome": "您好，请问有什么可以帮您？", "entry": "bottom-right", "streaming": True},
    },
    "h5": {
        "name": "H5", "version": "1.0",
        "connection_params": [
            {"key": "h5_path", "label": "H5 路径", "kind": "text", "required": True, "placeholder": "/h5/support"},
            {"key": "allowed_origins", "label": "允许域名", "kind": "origins", "required": True},
        ],
        "capabilities": ["Markdown", "卡片", "表单", "移动布局"],
        "default_experience": {"primary_color": "#3568d4", "radius": 16, "welcome": "您好，请描述您的问题", "entry": "fullscreen", "streaming": True},
    },
    "whatsapp": {
        "name": "WhatsApp", "version": "1.0",
        "connection_params": [
            {"key": "phone_number_id", "label": "Phone number ID", "kind": "text", "required": True},
            {"key": "access_token", "label": "Access token", "kind": "secret", "required": True},
            {"key": "verify_token", "label": "Webhook 校验 Token", "kind": "generated", "required": False},
        ],
        "capabilities": ["文本", "图片", "快捷回复", "模板卡片"],
        "default_experience": {"primary_color": "#168a64", "radius": 10, "welcome": "Hi, how can we help?", "entry": "conversation", "streaming": False},
    },
    "wechat": {
        "name": "微信", "version": "1.0",
        "connection_params": [
            {"key": "app_id", "label": "App ID", "kind": "text", "required": True},
            {"key": "app_secret", "label": "App secret", "kind": "secret", "required": True},
            {"key": "verify_token", "label": "服务器校验 Token", "kind": "generated", "required": False},
        ],
        "capabilities": ["文本", "图片", "富媒体", "小程序"],
        "default_experience": {"primary_color": "#168a64", "radius": 8, "welcome": "您好，欢迎关注", "entry": "official-account", "streaming": False},
    },
    "open-api": {
        "name": "开放 API", "version": "1.0",
        "connection_params": [
            {"key": "base_path", "label": "Base path", "kind": "text", "required": False, "placeholder": "/v1"},
            {"key": "api_key", "label": "API Key", "kind": "generated", "required": False},
        ],
        "capabilities": ["JSON", "SSE", "Metadata", "组件 payload"],
        "default_experience": {"entry": "endpoint", "streaming": True, "theme": {}},
    },
}
TYPE_CODES = tuple(CATALOG)


@dataclass(frozen=True)
class TypeView:
    code: str
    name: str
    version: str
    enabled: bool
    connection_params: list[dict[str, Any]]
    capabilities: list[str]
    default_experience: dict[str, Any]


def view_of(code: str, row: ChannelType | None) -> TypeView:
    base = CATALOG[code]
    return TypeView(
        code=code, name=base["name"], version=row.version if row else base["version"], enabled=row.enabled if row else True,
        connection_params=list(base["connection_params"]),  # the parameter definitions belong to the adapter, not to the platform editor
        capabilities=list(row.capabilities) if row and row.capabilities else list(base["capabilities"]),
        default_experience={**base["default_experience"], **(row.default_experience if row else {})},
    )


def list_types(db: Session) -> list[TypeView]:
    rows = {row.code: row for row in db.scalars(select(ChannelType))}
    return [view_of(code, rows.get(code)) for code in TYPE_CODES]


def get_type(db: Session, code: str) -> TypeView | None:
    if code not in CATALOG:
        return None
    return view_of(code, db.get(ChannelType, code))


def plan_includes(db: Session, tenant_id, code: str) -> bool:
    plan = entitlement_value(db, tenant_id, "channels")
    return code in PLAN_TYPES.get(str(plan), PLAN_TYPES["widget_api"])
