from time import perf_counter

from beework.integrations.litellm import GatewayError, LiteLLMClient
from beework.schemas import ModelTestRequest, ModelTestResponse


def run_model_test(*, api_key: str, model: str, payload: ModelTestRequest) -> ModelTestResponse:
    """Check a model through the gateway.

    access_only=True verifies the key and the model allowlist via /v1/models without
    contacting the provider; otherwise one short real chat request is sent.
    """
    started = perf_counter()

    def elapsed() -> int:
        return round((perf_counter() - started) * 1000)

    try:
        with LiteLLMClient() as client:
            if payload.access_only:
                allowed = client.accessible_models(api_key=api_key)
                if model not in allowed:
                    return ModelTestResponse(
                        ok=False, model=model, latency_ms=elapsed(), access_only=True,
                        error="网关凭证无权使用该模型",
                    )
                return ModelTestResponse(
                    ok=True, model=model, latency_ms=elapsed(), access_only=True,
                    content=f"网关凭证有效，可调用 {len(allowed)} 个模型（未调用模型供应商）",
                )
            completion = client.chat_completion(
                api_key=api_key,
                model=model,
                messages=[{"role": "user", "content": payload.message}],
                max_tokens=64,
                metadata={"source": "beework-model-test"},
            )
    except GatewayError as exc:
        return ModelTestResponse(
            ok=False, model=model, latency_ms=elapsed(), access_only=payload.access_only, error=str(exc)
        )
    usage = completion.get("usage") or {}
    choices = completion.get("choices") or [{}]
    return ModelTestResponse(
        ok=True,
        model=model,
        content=(choices[0].get("message") or {}).get("content"),
        latency_ms=elapsed(),
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        access_only=False,
    )
