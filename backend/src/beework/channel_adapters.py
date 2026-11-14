"""Channel adapters (T-041, CHN-035).

An adapter knows one channel type: how to validate its connection parameters, how to check it is healthy, how to render a reply for the
type's capabilities and what a customer has to paste to connect. Website and open API are implemented here; H5 / WhatsApp / WeChat
register their own adapter in T-044–T-046. Until then they use ``GenericAdapter``: parameters are validated from the type's
definition, there is nothing to check remotely and the snippet is the webhook address.
"""

import re
from dataclasses import dataclass
from typing import Any, Protocol

from beework.channel_publish import normalize_origins
from beework.channel_types import CATALOG
from beework.config import settings
from beework.models.channels import ChannelInstance


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    detail: str = ""


class ChannelAdapter(Protocol):
    code: str

    def validate_connection(self, config: dict[str, Any], secrets: dict[str, str]) -> list[str]:
        """Problems with the connection parameters, as messages for the administrator (empty = complete)."""

    def health_check(self, channel: ChannelInstance) -> HealthResult | None:
        """``None``: this adapter cannot judge health (nothing to call)."""

    def render(self, text: str, capabilities: set[str]) -> str:
        """The reply as this channel can show it."""

    def integration_snippet(self, channel: ChannelInstance, base_url: str, tenant_slug: str = "") -> dict[str, str]:
        """What to paste or open to connect: ``{"kind": ..., "code": ...}`` plus optional extras."""


TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def tables_to_lists(text: str) -> str:
    """Markdown tables become ``- a：b`` lines (CHN-035: the “表格” capability is off)."""
    lines, out, header = text.split("\n"), [], None
    for index, line in enumerate(lines):
        if TABLE_ROW.match(line) and index + 1 < len(lines) and TABLE_SEPARATOR.match(lines[index + 1]) and header is None:
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            continue
        if header is not None and TABLE_SEPARATOR.match(line):
            continue
        if header is not None and TABLE_ROW.match(line):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            out.append("- " + "；".join(f"{h}：{c}" for h, c in zip(header, cells, strict=False)))
            continue
        header = None
        out.append(line)
    return "\n".join(out)


class GenericAdapter:
    code = ""

    def __init__(self, code: str) -> None:
        self.code = code

    def validate_connection(self, config: dict[str, Any], secrets: dict[str, str]) -> list[str]:
        problems: list[str] = []
        for param in CATALOG[self.code]["connection_params"]:
            if not param.get("required"):
                continue
            key, kind = param["key"], param["kind"]
            present = bool(secrets.get(key)) if kind == "secret" else bool(config.get(key))
            if not present:
                problems.append(f"请填写{param['label']}")
        return problems

    def health_check(self, channel: ChannelInstance) -> HealthResult | None:
        return None

    def render(self, text: str, capabilities: set[str]) -> str:
        return text if "表格" in capabilities else tables_to_lists(text)

    def integration_snippet(self, channel: ChannelInstance, base_url: str, tenant_slug: str = "") -> dict[str, str]:
        return {"kind": "webhook", "code": f"{base_url}/v1/channels/{channel.type}/{channel.site_key}/webhook"}


class WebsiteAdapter(GenericAdapter):
    def __init__(self) -> None:
        super().__init__("website")

    def validate_connection(self, config: dict[str, Any], secrets: dict[str, str]) -> list[str]:
        origins = config.get("allowed_origins") or []
        try:
            normalize_origins(list(origins))
        except ValueError as exc:
            return [str(exc)]
        return [] if origins else ["请至少填写一个允许嵌入的域名"]

    def health_check(self, channel: ChannelInstance) -> HealthResult | None:
        snapshot = channel.published_snapshot
        if not snapshot:
            return None
        if not snapshot.get("allowed_origins"):
            return HealthResult(False, "已发布的快照没有允许域名")
        return HealthResult(True)

    def integration_snippet(self, channel: ChannelInstance, base_url: str, tenant_slug: str = "") -> dict[str, str]:
        return {"kind": "script", "code": f'<script src="{base_url}/v1/widget.js" data-site-key="{channel.site_key}" async></script>'}


class OpenApiAdapter(GenericAdapter):
    def __init__(self) -> None:
        super().__init__("open-api")

    def health_check(self, channel: ChannelInstance) -> HealthResult | None:
        return HealthResult(True) if channel.published_snapshot else None

    def render(self, text: str, capabilities: set[str]) -> str:
        return text  # JSON clients get the Markdown as written

    def integration_snippet(self, channel: ChannelInstance, base_url: str, tenant_slug: str = "") -> dict[str, str]:
        path = (channel.config or {}).get("base_path") or "/v1"
        code = (
            f"curl -X POST {base_url}{path}/chat \\\n"
            "  -H 'Authorization: Bearer <租户 API Key>' -H 'Content-Type: application/json' \\\n"
            f"  -d '{{\"assistant_id\": \"{channel.assistant_id}\", \"message\": \"你好\", \"stream\": false}}'"
        )
        return {"kind": "curl", "code": code}


class H5Adapter(GenericAdapter):
    def __init__(self) -> None:
        super().__init__("h5")

    def validate_connection(self, config: dict[str, Any], secrets: dict[str, str]) -> list[str]:
        problems = super().validate_connection(config, secrets)
        path = str(config.get("h5_path") or "")
        if path and not re.fullmatch(r"/[A-Za-z0-9._~/-]{1,120}", path):
            problems.append("H5 路径应以 / 开头，只含字母、数字和 . _ ~ / -，且不能只有 /")
        try:
            normalize_origins(list(config.get("allowed_origins") or []))
        except ValueError as exc:
            problems.append(str(exc))
        return problems

    def integration_snippet(self, channel: ChannelInstance, base_url: str, tenant_slug: str = "") -> dict[str, str]:
        path = (channel.config or {}).get("h5_path") or "/"
        return {"kind": "link", "code": f"{base_url.rstrip('/')}/h5/{tenant_slug}{path}", "embed": f'<iframe src="{base_url.rstrip("/")}/h5/{tenant_slug}{path}" style="border:0;width:100%;height:100%" allow="clipboard-write"></iframe>'}


_ADAPTERS: dict[str, ChannelAdapter] = {
    "website": WebsiteAdapter(), "h5": H5Adapter(), "open-api": OpenApiAdapter(),
    "whatsapp": GenericAdapter("whatsapp"), "wechat": GenericAdapter("wechat"),
}


def adapter_for(code: str) -> ChannelAdapter:
    return _ADAPTERS[code]


def register_adapter(adapter: ChannelAdapter) -> None:
    """Later channel tasks (T-044–T-046) replace the generic adapter of their type."""
    _ADAPTERS[adapter.code] = adapter


def public_base_url(request_base: str) -> str:
    return (settings.public_api_url or request_base).rstrip("/")
