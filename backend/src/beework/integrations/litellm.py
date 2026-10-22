"""LiteLLM proxy (AI gateway) client.

BeeWork does not call model providers itself: platform catalog entries are
registered as LiteLLM deployments, and each tenant environment gets a LiteLLM
team + virtual key restricted to the models that environment has enabled.
LiteLLM then enforces the allowlist, RPM limits and records spend per team.
"""

from typing import Any
from uuid import UUID

import httpx

from beework.config import settings

PROVIDER_PREFIXES = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google": "gemini",
    "azure_openai": "azure",
    "alibaba_qwen": "dashscope",
    # Private deployments are expected to expose an OpenAI-compatible API.
    "private": "openai",
}

# LiteLLM treats an empty model list as "all models"; this alias never exists.
NO_MODELS_ALIAS = "bw-no-models"


class GatewayError(RuntimeError):
    pass


def gateway_configured() -> bool:
    return bool(settings.litellm_base_url and settings.litellm_master_key)


def model_alias(model_id: UUID) -> str:
    """Stable gateway name for a platform catalog entry."""
    return f"bw-{model_id}"


def team_alias(tenant_id: UUID, environment: str) -> str:
    return f"beework:{tenant_id}:{environment}"


class LiteLLMClient:
    def __init__(
        self,
        base_url: str | None = None,
        master_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        base_url = base_url if base_url is not None else settings.litellm_base_url
        master_key = master_key if master_key is not None else settings.litellm_master_key
        if not base_url or not master_key:
            raise GatewayError("AI 网关（LiteLLM）未配置")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {master_key}"},
            timeout=settings.integration_timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LiteLLMClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise GatewayError(f"无法连接 AI 网关：{exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            raise GatewayError(_error_message(response))
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def health(self) -> bool:
        self._request("GET", "/health/liveliness")
        return True

    def upsert_model(
        self,
        *,
        model_id: UUID,
        provider: str,
        provider_model_id: str,
        endpoint_url: str,
        api_key: str,
        api_version: str | None,
        rpm_limit: int | None,
        model_type: str,
    ) -> None:
        """Register (or replace) a catalog entry as a LiteLLM deployment."""
        litellm_params: dict[str, Any] = {
            "model": f"{PROVIDER_PREFIXES[provider]}/{provider_model_id}",
            "api_base": endpoint_url,
            "api_key": api_key,
        }
        if api_version:
            litellm_params["api_version"] = api_version
        if rpm_limit:
            litellm_params["rpm"] = rpm_limit
        self.delete_model(model_id)
        self._request(
            "POST",
            "/model/new",
            json={
                "model_name": model_alias(model_id),
                "litellm_params": litellm_params,
                "model_info": {"id": str(model_id), "mode": _litellm_mode(model_type)},
            },
        )

    def delete_model(self, model_id: UUID) -> None:
        try:
            self._request("POST", "/model/delete", json={"id": str(model_id)})
        except GatewayError as exc:
            if "not found" not in str(exc).lower() and "404" not in str(exc):
                raise

    def ensure_team(self, *, team_id: str | None, alias: str, models: list[str]) -> str:
        allowed = models or [NO_MODELS_ALIAS]
        if team_id:
            self._request("POST", "/team/update", json={"team_id": team_id, "models": allowed})
            return team_id
        created = self._request(
            "POST", "/team/new", json={"team_alias": alias, "models": allowed}
        )
        return created["team_id"]

    def generate_key(self, *, team_id: str, alias: str) -> str:
        created = self._request(
            "POST",
            "/key/generate",
            json={"team_id": team_id, "key_alias": alias, "metadata": {"source": "beework"}},
        )
        return created["key"]

    def accessible_models(self, *, api_key: str) -> list[str]:
        """Model aliases the given key may call; validates the key without a provider call."""
        payload = self._request(
            "GET", "/v1/models", headers={"Authorization": f"Bearer {api_key}"}
        )
        return [item["id"] for item in (payload or {}).get("data", [])]

    def chat_completion(
        self,
        *,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int = 64,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Call a model through the gateway with a tenant virtual key."""
        body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if metadata:
            body["metadata"] = metadata
        return self._request(
            "POST",
            "/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=settings.model_call_timeout_seconds,
        )


def _litellm_mode(model_type: str) -> str:
    return {"embedding": "embedding", "rerank": "rerank"}.get(model_type, "chat")


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"AI 网关返回 {response.status_code}"
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        message = error.get("message") or error
    else:
        message = error or payload.get("detail") if isinstance(payload, dict) else payload
    return f"AI 网关返回 {response.status_code}：{str(message)[:300]}"
