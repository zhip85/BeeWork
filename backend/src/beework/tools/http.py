"""The only outbound HTTP client tools may use (NFR-009, T-071).

For every request: the host is resolved once, **every** address must be public, and the connection is pinned to a
validated address (the TLS server name and the ``Host`` header keep the original host) — so a name that resolves to a
public address now and to an internal one a moment later (DNS rebinding) cannot reach the network inside. Redirects
are never followed, the request times out, and a response larger than the limit is an error rather than a silent cut.
"""

import ipaddress
import socket
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

import httpx

from beework.config import settings

DEFAULT_TIMEOUT = 30.0
# The timeout the controlled executor set for the call in progress (T-165): providers keep calling ``safe_request`` without a timeout.
CALL_TIMEOUT: ContextVar[float | None] = ContextVar("call_timeout", default=None)
MAX_RESPONSE_BYTES = 1024 * 1024
Resolver = Callable[[str], list[str]]


class ToolHttpError(Exception):
    """A request the executor refused or could not complete; ``code`` is stable, ``message`` is safe to show."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status: int | None = None
        self.retry_after: float | None = None


@dataclass
class HttpResult:
    status: int
    headers: dict[str, str]
    body: bytes
    address: str = ""
    request_bytes: int = 0
    extra: dict = field(default_factory=dict)


def retry_after_of(headers: dict[str, str]) -> float | None:
    """The ``Retry-After`` of a response in seconds (the seconds form only; capped, so an upstream cannot park a run)."""
    raw = {k.lower(): v for k, v in headers.items()}.get("retry-after", "")
    return min(float(raw), 60.0) if raw.strip().replace(".", "", 1).isdigit() else None


def system_resolver(hostname: str) -> list[str]:
    return sorted({str(info[4][0]) for info in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)})


def _forbidden(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return (
        address.is_private or address.is_loopback or address.is_link_local or address.is_reserved
        or address.is_multicast or address.is_unspecified
    )


def resolve_public(hostname: str, resolver: Resolver | None, insecure: bool) -> list[str]:
    """Resolve ``hostname`` once and return its addresses, refusing the whole name when any of them is internal."""
    try:
        addresses = [str(ipaddress.ip_address(hostname.strip("[]")))]
    except ValueError:
        try:
            addresses = (resolver or system_resolver)(hostname)
        except OSError as exc:
            raise ToolHttpError("tool.unresolvable", "工具地址无法解析") from exc
    if not addresses:
        raise ToolHttpError("tool.unresolvable", "工具地址无法解析")
    clean = [raw.split("%")[0] for raw in addresses]
    if not insecure and any(_forbidden(ipaddress.ip_address(raw)) for raw in clean):
        raise ToolHttpError("tool.forbidden_address", "工具地址不得指向内网、回环或保留地址")
    return clean


def safe_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json: object | None = None,
    content: bytes | None = None,
    params: dict[str, str] | None = None,
    timeout: float | None = None,
    max_bytes: int = MAX_RESPONSE_BYTES,
    allowed_host: str | None = None,
    resolver: Resolver | None = None,
    transport: httpx.BaseTransport | None = None,
    on_chunk: Callable[[bytes], None] | None = None,
) -> HttpResult:
    timeout = timeout if timeout is not None else (CALL_TIMEOUT.get() or DEFAULT_TIMEOUT)
    insecure = settings.allow_insecure_tool_urls
    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    if parsed.scheme not in ("https", "http") or not hostname or (parsed.scheme == "http" and not insecure):
        raise ToolHttpError("tool.invalid_url", "工具地址必须是 https 链接")
    if parsed.username or parsed.password:
        raise ToolHttpError("tool.invalid_url", "工具地址不能包含用户名或密码")
    if allowed_host is not None and hostname.lower() != allowed_host.lower():
        raise ToolHttpError("tool.host_not_allowed", "请求的主机不在该工具允许的范围内")
    addresses = resolve_public(hostname, resolver, insecure)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    query = "&".join(part for part in (parsed.query, urlencode(params or {})) if part)
    if query:
        path = f"{path}?{query}"
    last_error: Exception | None = None
    for address in addresses:
        literal = f"[{address}]" if ":" in address else address
        target = f"{parsed.scheme}://{literal}:{port}{path}"
        request_headers = {**(headers or {}), "Host": parsed.netloc.split("@")[-1]}
        try:
            with httpx.Client(
                transport=transport, follow_redirects=False, timeout=timeout, trust_env=False,
            ) as client:
                request = client.build_request(
                    method, target, headers=request_headers, json=json, content=content,
                    extensions={"sni_hostname": hostname},
                )
                request_bytes = len(request.content or b"")
                response = client.send(request, stream=True)
                try:
                    if 300 <= response.status_code < 400:
                        raise ToolHttpError("tool.redirect_blocked", "工具返回了重定向，已拒绝跟随")
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise ToolHttpError("tool.response_too_large", "工具响应超过大小上限")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        if on_chunk is not None and 200 <= response.status_code < 300:
                            on_chunk(chunk)  # a streaming response is consumed as it arrives (T-163); may raise to stop reading
                        body += chunk
                        if len(body) > max_bytes:
                            raise ToolHttpError("tool.response_too_large", "工具响应超过大小上限")
                    return HttpResult(
                        status=response.status_code, headers=dict(response.headers), body=bytes(body),
                        address=address, request_bytes=request_bytes,
                    )
                finally:
                    response.close()
        except ToolHttpError:
            raise
        except httpx.TimeoutException as exc:
            raise ToolHttpError("tool.timeout", "工具调用超时") from exc
        except httpx.TransportError as exc:  # try the next validated address
            last_error = exc
    raise ToolHttpError("tool.unreachable", "工具无法连接") from last_error
