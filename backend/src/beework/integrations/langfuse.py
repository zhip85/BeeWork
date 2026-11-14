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


# ---------------------------------------------------------------------------
# Reading and deleting traces (T-084). Measured against Langfuse Cloud on 2026-11-14:
#   GET    /api/public/v2/observations?traceId=…&fields=core,basic,time,io,usage,model&limit=…&cursor=…  (v2, cursor paging in ``meta.cursor``)
#   DELETE /api/public/traces        body ``{"traceIds": [...]}``      (batch; unknown ids are not an error)
#   GET    /api/public/traces/{id}   410 — the old read API is gone; /api/public/v2/traces/{id} is 404.
# ---------------------------------------------------------------------------

OBSERVATION_FIELDS = "core,basic,time,io,usage,model"
OBSERVATION_PAGE = 100
MAX_OBSERVATIONS = 1000  # a run with more spans than this shows the first part only


def _client(transport: httpx.BaseTransport | None) -> httpx.Client:
    if not langfuse_configured():
        raise TracingError("追踪服务（Langfuse）未配置")
    return httpx.Client(
        base_url=settings.langfuse_host.rstrip("/"),
        auth=(settings.langfuse_public_key, settings.langfuse_secret_key),
        timeout=settings.integration_timeout_seconds,
        transport=transport,
    )


def _check(response: httpx.Response) -> None:
    if response.status_code == 401:
        raise TracingError("追踪服务密钥无效")
    if response.status_code >= 400:
        raise TracingError(f"追踪服务返回 {response.status_code}")


def fetch_observations(trace_id: str, transport: httpx.BaseTransport | None = None) -> list[dict]:
    """All observations of a trace (spans, generations, events), oldest first as Langfuse returns them; ``[]`` when it has none."""
    found: list[dict] = []
    cursor: str | None = None
    try:
        with _client(transport) as client:
            while len(found) < MAX_OBSERVATIONS:
                params = {"traceId": trace_id, "limit": OBSERVATION_PAGE, "fields": OBSERVATION_FIELDS}
                if cursor:
                    params["cursor"] = cursor
                response = client.get("/api/public/v2/observations", params=params)
                _check(response)
                body = response.json()
                found.extend(body.get("data") or [])
                cursor = (body.get("meta") or {}).get("cursor")
                if not cursor:
                    break
    except httpx.HTTPError as exc:
        raise TracingError(f"无法连接追踪服务：{exc.__class__.__name__}") from exc
    except ValueError as exc:
        raise TracingError("追踪服务返回了无法解析的内容") from exc
    return found[:MAX_OBSERVATIONS]


def delete_traces(trace_ids: list[str], transport: httpx.BaseTransport | None = None) -> None:
    """Delete traces in Langfuse (retention). Unknown ids are fine; any failure raises so the caller keeps its records and retries."""
    if not trace_ids:
        return
    try:
        with _client(transport) as client:
            response = client.request("DELETE", "/api/public/traces", json={"traceIds": trace_ids})
            _check(response)
    except httpx.HTTPError as exc:
        raise TracingError(f"无法连接追踪服务：{exc.__class__.__name__}") from exc
