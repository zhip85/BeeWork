"""Langfuse connectivity check.

Traces are sent by the engines themselves (LiteLLM's langfuse callback and the
Langfuse LangGraph callback handler); BeeWork only verifies the configuration.
"""

import httpx

from beework.config import settings


class TracingError(RuntimeError):
    pass


def langfuse_configured() -> bool:
    return bool(
        settings.langfuse_host and settings.langfuse_public_key and settings.langfuse_secret_key
    )


def check_langfuse(transport: httpx.BaseTransport | None = None) -> bool:
    if not langfuse_configured():
        raise TracingError("追踪服务（Langfuse）未配置")
    try:
        with httpx.Client(
            base_url=settings.langfuse_host.rstrip("/"),
            auth=(settings.langfuse_public_key, settings.langfuse_secret_key),
            timeout=settings.integration_timeout_seconds,
            transport=transport,
        ) as client:
            response = client.get("/api/public/projects")
    except httpx.HTTPError as exc:
        raise TracingError(f"无法连接追踪服务：{exc.__class__.__name__}") from exc
    if response.status_code == 401:
        raise TracingError("追踪服务密钥无效")
    if response.status_code >= 400:
        raise TracingError(f"追踪服务返回 {response.status_code}")
    return True
