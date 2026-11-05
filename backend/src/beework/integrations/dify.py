"""Dify knowledge base (Dataset) API client.

Dify Cloud (``https://api.dify.ai/v1``) and self-hosted Dify expose the same API, authenticated with a workspace
"Knowledge API" key (``dataset-…``). Written from Dify's documentation; see T-030 for what has been verified.
"""

from typing import Any

import httpx

from beework.config import settings
from beework.integrations.knowledge_engine import KnowledgeEngineError

# Dify limits a knowledge base name to 40 characters.
MAX_NAME_LENGTH = 40
# Dify configures the embedding model per workspace/provider. BeeWork's options map to a provider model where Dify
# Cloud offers one; ``None`` means "use the workspace default" (e.g. bge-m3 needs a self-hosted provider).
EMBEDDING_MODELS: dict[str, tuple[str, str] | None] = {
    "bge-m3": None,
    "text-embedding-3-large": ("openai", "text-embedding-3-large"),
    "cohere-embed-v3": ("cohere", "embed-multilingual-v3.0"),
}


class DifyClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        base_url = base_url if base_url is not None else settings.dify_base_url
        api_key = api_key if api_key is not None else settings.dify_api_key
        if not base_url or not api_key:
            raise KnowledgeEngineError("知识引擎（Dify）未配置")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=settings.integration_timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DifyClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _request(self, method: str, path: str, *, tolerate_missing: bool = False, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise KnowledgeEngineError(f"无法连接知识引擎：{exc.__class__.__name__}") from exc
        if tolerate_missing and response.status_code == 404:
            return None
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            message = payload.get("message") if isinstance(payload, dict) else None
            raise KnowledgeEngineError(f"知识引擎错误（{response.status_code}）：{str(message or '未知错误')[:300]}")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise KnowledgeEngineError(f"知识引擎返回了无法识别的内容（{response.status_code}）") from exc

    def health(self) -> bool:
        self._request("GET", "/datasets", params={"page": 1, "limit": 1})
        return True

    def create_knowledge_dataset(self, name: str, *, faq: bool, chunking: str, embedding_model: str) -> str:
        """Dify applies chunking and the question–answer mode per *document* (``process_rule`` / ``doc_form``), so
        ``faq`` and ``chunking`` only matter when documents are added (T-031 / T-032); the dataset itself carries the
        embedding model."""
        if len(name) > MAX_NAME_LENGTH:
            raise KnowledgeEngineError(f"知识库名称超过 Dify 的 {MAX_NAME_LENGTH} 个字符限制")
        body: dict[str, Any] = {"name": name, "indexing_technique": "high_quality", "permission": "all_team_members"}
        embedding = EMBEDDING_MODELS.get(embedding_model)
        if embedding:
            body["embedding_model_provider"], body["embedding_model"] = embedding
        return self._request("POST", "/datasets", json=body)["id"]

    def find_dataset(self, name: str) -> str | None:
        data = self._request("GET", "/datasets", params={"keyword": name, "page": 1, "limit": 20})
        for dataset in (data or {}).get("data", []):
            if dataset.get("name") == name:
                return dataset["id"]
        return None

    def delete_datasets(self, dataset_ids: list[str]) -> None:
        for dataset_id in dataset_ids:  # Dify has no bulk delete
            self._request("DELETE", f"/datasets/{dataset_id}", tolerate_missing=True)
