"""A small MCP client over the Streamable HTTP transport (T-072, CAP-030/031).

Why not the official SDK's client: it is asynchronous and opens its own connections, while every outbound request of a
tool must go through ``safe_request`` (resolved once, pinned to a validated address, no redirects, size and time
limits). The protocol surface needed here is small — ``initialize``, ``tools/list`` and ``tools/call`` as JSON-RPC over
HTTP POST, answered either by one JSON body or by a Server-Sent-Events stream — so it is implemented directly on the
safe client. The legacy HTTP+SSE transport is not offered (deprecated by the MCP specification since 2025-03-26).
"""

import itertools
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from beework.tools.http import ToolHttpError, retry_after_of, safe_request

PROTOCOL_VERSION = "2025-03-26"
CLIENT_INFO = {"name": "beework", "version": "1.0"}
MAX_TOOL_PAGES = 20
# Tests replace these; production leaves them None so the system resolver and a real connection are used.
TRANSPORT: httpx.BaseTransport | None = None
RESOLVER: Callable[[str], list[str]] | None = None


class McpError(ToolHttpError):
    """A failure talking to an MCP server; ``code`` is stable (``mcp.auth_failed``, ``mcp.protocol``, ...)."""

    def __init__(self, code: str, message: str, *, status: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(code, message)
        self.status, self.retry_after = status, retry_after


@dataclass
class McpTool:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    read_only: bool = False  # annotations.readOnlyHint: such a tool never needs the end user's confirmation

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema, "read_only": self.read_only}


def _sse_messages(body: str) -> list[dict[str, Any]]:
    """The JSON-RPC messages carried by a ``text/event-stream`` response body."""
    messages: list[dict[str, Any]] = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in block.split("\n") if line.startswith("data:"))
        if data:
            try:
                parsed = json.loads(data)
            except ValueError:
                continue
            messages.extend(parsed if isinstance(parsed, list) else [parsed])
    return messages


class McpClient:
    def __init__(self, url: str, headers: dict[str, str] | None = None, *, timeout: float | None = None) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.session_id: str | None = None
        self._ids = itertools.count(1)
        self._initialized = False

    # -- transport ---------------------------------------------------------------

    def _post(self, payload: dict[str, Any], *, expect_response: bool = True) -> dict[str, Any] | None:
        headers = {
            **self.headers, "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            result = safe_request(
                "POST", self.url, headers=headers, json=payload, timeout=self.timeout, resolver=RESOLVER, transport=TRANSPORT
            )
        except McpError:
            raise
        except ToolHttpError as exc:
            raise McpError(exc.code, exc.message) from exc
        if result.status in (401, 403):
            raise McpError("mcp.auth_failed", "认证失败：服务拒绝了凭证")
        if result.status == 404 and self.session_id:
            raise McpError("mcp.session_expired", "MCP 会话已失效")
        if result.status >= 400:
            raise McpError("mcp.http_error", f"服务返回 HTTP {result.status}", status=result.status, retry_after=retry_after_of(result.headers))
        self.session_id = result.headers.get("mcp-session-id") or self.session_id
        if not expect_response:
            return None
        content_type = result.headers.get("content-type", "").split(";")[0].strip().lower()
        text = result.body.decode("utf-8", errors="replace")
        try:
            if content_type == "text/event-stream":
                candidates = _sse_messages(text)
            else:
                parsed = json.loads(text)
                candidates = parsed if isinstance(parsed, list) else [parsed]
        except ValueError as exc:
            raise McpError("mcp.protocol", "服务返回的内容不是 MCP 响应（协议不兼容）") from exc
        for message in candidates:
            if isinstance(message, dict) and message.get("id") == payload.get("id"):
                return message
        raise McpError("mcp.protocol", "服务没有返回对应的响应（协议不兼容）")

    def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        message = self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or {}})
        assert message is not None
        if "error" in message:
            error = message["error"] or {}
            raise McpError("mcp.rpc_error", str(error.get("message") or "服务返回错误")[:200])
        return message.get("result") or {}

    # -- protocol ----------------------------------------------------------------

    def initialize(self) -> dict[str, Any]:
        result = self._request(
            "initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO}
        )
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_response=False)
        self._initialized = True
        return result

    def list_tools(self) -> list[McpTool]:
        if not self._initialized:
            self.initialize()
        tools: list[McpTool] = []
        cursor: str | None = None
        for _ in range(MAX_TOOL_PAGES):
            result = self._request("tools/list", {"cursor": cursor} if cursor else {})
            for raw in result.get("tools") or []:
                if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
                    continue
                annotations = raw.get("annotations") if isinstance(raw.get("annotations"), dict) else {}
                tools.append(
                    McpTool(
                        name=raw["name"][:128], description=str(raw.get("description") or "")[:1000],
                        input_schema=raw.get("inputSchema") if isinstance(raw.get("inputSchema"), dict) else {"type": "object", "properties": {}},
                        read_only=bool(annotations.get("readOnlyHint")),
                    )
                )
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """(text for the model, whether the server flagged an error)."""
        if not self._initialized:
            self.initialize()
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        parts: list[str] = []
        for item in result.get("content") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif item.get("type") == "resource" and isinstance(item.get("resource"), dict):
                parts.append(str(item["resource"].get("text") or f"[resource {item['resource'].get('uri', '')}]"))
            else:
                parts.append(f"[{item.get('type', 'content')}]")
        if not parts and result.get("structuredContent") is not None:
            parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
        return "\n".join(parts), bool(result.get("isError"))
