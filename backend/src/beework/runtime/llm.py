from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from beework.config import settings
from beework.runtime.dsl import AssistantDSLv1


def build_chat_model(dsl: AssistantDSLv1, *, api_key: str, metadata: dict[str, Any]) -> ChatOpenAI:
    """ChatOpenAI pointed at LiteLLM with the tenant environment's virtual key.

    ``metadata`` (tenant, assistant, run, ...) is forwarded to the gateway so usage can be attributed. LiteLLM only
    persists request metadata placed under ``spend_logs_metadata``; usage metering (T-011) reads it back from there.
    """
    return ChatOpenAI(
        model=dsl.model_alias,
        base_url=f"{settings.litellm_base_url.rstrip('/')}/v1",
        api_key=SecretStr(api_key),
        streaming=True,
        stream_usage=True,
        max_tokens=dsl.limits.max_tokens,
        temperature=dsl.temperature,
        timeout=dsl.limits.timeout_seconds,
        max_retries=0,
        extra_body={"metadata": {**metadata, "spend_logs_metadata": dict(metadata)}},
    )


def build_rewrite_model(model_alias: str, *, api_key: str, metadata: dict[str, Any]) -> ChatOpenAI:
    """A short, non-streaming call for question rewriting (T-057): low temperature, small output, 1.5 s limit."""
    return ChatOpenAI(
        model=model_alias,
        base_url=f"{settings.litellm_base_url.rstrip('/')}/v1",
        api_key=SecretStr(api_key),
        streaming=False,
        max_tokens=500,
        temperature=0,
        timeout=1.5,
        max_retries=0,
        extra_body={"metadata": {**metadata, "spend_logs_metadata": dict(metadata)}},
    )
