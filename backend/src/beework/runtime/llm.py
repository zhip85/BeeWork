from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from beework.config import settings
from beework.runtime.dsl import AssistantDSLv1


def build_chat_model(dsl: AssistantDSLv1, *, api_key: str, metadata: dict[str, Any]) -> ChatOpenAI:
    """ChatOpenAI pointed at LiteLLM with the tenant environment's virtual key.

    ``metadata`` (tenant, assistant, run, ...) is forwarded to the gateway so usage can be attributed.
    """
    return ChatOpenAI(
        model=dsl.model_alias,
        base_url=f"{settings.litellm_base_url.rstrip('/')}/v1",
        api_key=SecretStr(api_key),
        streaming=True,
        stream_usage=True,
        max_tokens=dsl.limits.max_tokens,
        timeout=dsl.limits.timeout_seconds,
        max_retries=0,
        extra_body={"metadata": metadata},
    )
