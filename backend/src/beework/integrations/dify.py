"""Dify knowledge base (Dataset) API client.

Dify Cloud (``https://api.dify.ai/v1``) and self-hosted Dify expose the same API, authenticated with a workspace
"Knowledge API" key (``dataset-…``). Written from Dify's documentation; see T-030 for what has been verified.
"""

import json
from typing import Any

import httpx

from beework.config import settings
from beework.integrations.knowledge_engine import DocumentRef, DocumentState, KnowledgeEngineError

# Dify limits a knowledge base name to 40 characters.
MAX_NAME_LENGTH = 40
# Dify configures the embedding model per workspace/provider. BeeWork's options map to a provider model where Dify
# Cloud offers one; ``None`` means "use the workspace default" (e.g. bge-m3 needs a self-hosted provider).
EMBEDDING_MODELS: dict[str, tuple[str, str] | None] = {
    "bge-m3": None,
    "text-embedding-3-large": ("openai", "text-embedding-3-large"),
    "cohere-embed-v3": ("cohere", "embed-multilingual-v3.0"),
}


# Chunking strategies as Dify ``process_rule``s (applied per document). Taken from Dify's documentation.
PRE_PROCESSING = [
    {"id": "remove_extra_spaces", "enabled": True},
    {"id": "remove_urls_emails", "enabled": False},
]
PROCESS_RULES: dict[str, dict[str, Any]] = {
    "semantic": {"mode": "automatic"},
    "fixed_512": {
        "mode": "custom",
        "rules": {"pre_processing_rules": PRE_PROCESSING, "segmentation": {"separator": "\n", "max_tokens": 512}},
    },
    "by_heading": {
        "mode": "custom",
        "rules": {"pre_processing_rules": PRE_PROCESSING, "segmentation": {"separator": "\n#", "max_tokens": 1000}},
    },
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

    def add_document(self, dataset_id: str, filename: str, data: bytes, *, chunking: str) -> DocumentRef:
        """Upload a file; Dify starts indexing on its own. The returned ``batch`` is what its status call needs."""
        options = {
            "indexing_technique": "high_quality",
            "process_rule": PROCESS_RULES[chunking],
            "doc_form": "text_model",
            "doc_language": "Chinese Simplified",
        }
        created = self._request(
            "POST",
            f"/datasets/{dataset_id}/document/create-by-file",
            data={"data": json.dumps(options)},
            files={"file": (filename, data)},
        )
        return DocumentRef(created["document"]["id"], created.get("batch"))

    def document_state(self, dataset_id: str, ref: DocumentRef) -> DocumentState:
        if not ref.batch_id:
            raise KnowledgeEngineError("缺少 Dify 的索引批次，无法查询文档状态")
        payload = self._request("GET", f"/datasets/{dataset_id}/documents/{ref.batch_id}/indexing-status")
        entries = [item for item in (payload or {}).get("data", []) if item.get("id") == ref.document_id]
        if not entries:
            raise KnowledgeEngineError("知识引擎中未找到该文档")
        entry = entries[0]
        status = str(entry.get("indexing_status") or "")
        if status == "completed":
            return DocumentState("indexed", int(entry.get("total_segments") or entry.get("completed_segments") or 0))
        if status in ("error", "paused"):
            return DocumentState("failed", 0, str(entry.get("error") or "知识引擎索引失败")[:500])
        return DocumentState("processing")

    def delete_documents(self, dataset_id: str, document_ids: list[str]) -> None:
        for document_id in document_ids:
            self._request("DELETE", f"/datasets/{dataset_id}/documents/{document_id}", tolerate_missing=True)

    def list_chunks(self, dataset_id: str, document_id: str, *, page: int, size: int) -> tuple[list[str], int]:
        data = self._request(
            "GET", f"/datasets/{dataset_id}/documents/{document_id}/segments", params={"page": page, "limit": size}
        )
        items = (data or {}).get("data", [])
        return [str(item.get("content") or "") for item in items], int((data or {}).get("total") or len(items))

    def delete_datasets(self, dataset_ids: list[str]) -> None:
        for dataset_id in dataset_ids:  # Dify has no bulk delete
            self._request("DELETE", f"/datasets/{dataset_id}", tolerate_missing=True)
