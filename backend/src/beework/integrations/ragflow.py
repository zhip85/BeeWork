"""RAGFlow HTTP API client.

Knowledge parsing, chunking, embedding and retrieval run inside RAGFlow. BeeWork
keeps the business records (knowledge base, document, status, permissions) and
maps each BeeWork knowledge base to one RAGFlow dataset.
"""

from typing import Any, BinaryIO

import httpx

from beework.config import settings


class KnowledgeEngineError(RuntimeError):
    pass


def ragflow_configured() -> bool:
    return bool(settings.ragflow_base_url and settings.ragflow_api_key)


class RAGFlowClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        base_url = base_url if base_url is not None else settings.ragflow_base_url
        api_key = api_key if api_key is not None else settings.ragflow_api_key
        if not base_url or not api_key:
            raise KnowledgeEngineError("知识引擎（RAGFlow）未配置")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/api/v1",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=settings.integration_timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "RAGFlowClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise KnowledgeEngineError(f"无法连接知识引擎：{exc.__class__.__name__}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise KnowledgeEngineError(f"知识引擎返回 {response.status_code}") from exc
        # RAGFlow reports errors as {"code": != 0, "message": ...}, often with HTTP 200.
        if response.status_code >= 400 or payload.get("code", 0) != 0:
            message = payload.get("message") or response.status_code
            raise KnowledgeEngineError(f"知识引擎错误：{str(message)[:300]}")
        return payload.get("data")

    def health(self) -> bool:
        self._request("GET", "/datasets", params={"page": 1, "page_size": 1})
        return True

    def create_dataset(
        self, *, name: str, chunk_method: str = "naive", embedding_model: str | None = None
    ) -> str:
        body: dict[str, Any] = {"name": name, "chunk_method": chunk_method}
        if embedding_model:
            body["embedding_model"] = embedding_model
        return self._request("POST", "/datasets", json=body)["id"]

    def delete_datasets(self, dataset_ids: list[str]) -> None:
        self._request("DELETE", "/datasets", json={"ids": dataset_ids})

    def upload_document(self, dataset_id: str, filename: str, content: BinaryIO) -> str:
        data = self._request(
            "POST", f"/datasets/{dataset_id}/documents", files={"file": (filename, content)}
        )
        return data[0]["id"]

    def start_parsing(self, dataset_id: str, document_ids: list[str]) -> None:
        self._request(
            "POST", f"/datasets/{dataset_id}/chunks", json={"document_ids": document_ids}
        )

    def document_status(self, dataset_id: str, document_id: str) -> dict[str, Any]:
        data = self._request(
            "GET", f"/datasets/{dataset_id}/documents", params={"id": document_id}
        )
        documents = data.get("docs", []) if isinstance(data, dict) else []
        if not documents:
            raise KnowledgeEngineError("知识引擎中未找到该文档")
        document = documents[0]
        return {
            "run": document.get("run"),
            "progress": document.get("progress"),
            "chunk_count": document.get("chunk_count"),
            "message": document.get("progress_msg"),
        }

    def delete_documents(self, dataset_id: str, document_ids: list[str]) -> None:
        self._request("DELETE", f"/datasets/{dataset_id}/documents", json={"ids": document_ids})

    def retrieve(
        self,
        *,
        question: str,
        dataset_ids: list[str],
        top_k: int = 8,
        similarity_threshold: float = 0.2,
    ) -> list[dict[str, Any]]:
        data = self._request(
            "POST",
            "/retrieval",
            json={
                "question": question,
                "dataset_ids": dataset_ids,
                "top_k": top_k,
                "similarity_threshold": similarity_threshold,
            },
        )
        return [
            {
                "chunk_id": chunk.get("id"),
                "document_id": chunk.get("document_id"),
                "document_name": chunk.get("document_keyword"),
                "content": chunk.get("content"),
                "similarity": chunk.get("similarity"),
            }
            for chunk in (data or {}).get("chunks", [])
        ]
