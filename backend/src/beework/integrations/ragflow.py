"""RAGFlow HTTP API client.

Knowledge parsing, chunking, embedding and retrieval run inside RAGFlow. BeeWork
keeps the business records (knowledge base, document, status, permissions) and
maps each BeeWork knowledge base to one RAGFlow dataset.
"""

import io
from typing import Any, BinaryIO

import httpx

from beework.config import settings
from beework.integrations.knowledge_engine import DocumentRef, DocumentState, KnowledgeEngineError

# How the PRD's chunking strategies map to RAGFlow ``chunk_method`` / ``parser_config``. Taken from RAGFlow's
# documentation, NOT yet checked against a running RAGFlow (T-030 step 0).
CHUNK_METHODS: dict[str, tuple[str, dict[str, Any]]] = {
    "semantic": ("naive", {}),
    "fixed_512": ("naive", {"chunk_token_num": 512}),
    "by_heading": ("book", {}),
}
FAQ_CHUNK_METHOD = "qa"
NOT_FOUND = "don't own the dataset"


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
        self,
        *,
        name: str,
        chunk_method: str = "naive",
        embedding_model: str | None = None,
        parser_config: dict[str, Any] | None = None,
    ) -> str:
        body: dict[str, Any] = {"name": name, "chunk_method": chunk_method}
        if embedding_model:
            body["embedding_model"] = embedding_model
        if parser_config:
            body["parser_config"] = parser_config
        return self._request("POST", "/datasets", json=body)["id"]

    def find_dataset(self, name: str) -> str | None:
        """Id of the dataset with exactly this name, so a retried creation reuses what an earlier attempt made."""
        try:
            data = self._request("GET", "/datasets", params={"name": name, "page": 1, "page_size": 5})
        except KnowledgeEngineError as exc:
            # RAGFlow answers a name filter that matches nothing with an error rather than an empty list.
            if NOT_FOUND in str(exc):
                return None
            raise
        for dataset in data or []:
            if dataset.get("name") == name:
                return dataset["id"]
        return None

    def create_knowledge_dataset(self, name: str, *, faq: bool, chunking: str, embedding_model: str) -> str:
        if faq:
            return self.create_dataset(name=name, chunk_method=FAQ_CHUNK_METHOD, embedding_model=embedding_model)
        method, parser_config = CHUNK_METHODS[chunking]
        return self.create_dataset(
            name=name, chunk_method=method, embedding_model=embedding_model, parser_config=parser_config or None
        )

    def delete_datasets(self, dataset_ids: list[str]) -> None:
        try:
            self._request("DELETE", "/datasets", json={"ids": dataset_ids})
        except KnowledgeEngineError as exc:
            if NOT_FOUND not in str(exc):
                raise
            # Some are already gone (an earlier job or a retry got there first): delete the rest one by one.
            for dataset_id in dataset_ids:
                try:
                    self._request("DELETE", "/datasets", json={"ids": [dataset_id]})
                except KnowledgeEngineError as single:
                    if NOT_FOUND not in str(single):
                        raise

    def add_document(self, dataset_id: str, filename: str, data: bytes, *, chunking: str) -> DocumentRef:
        # RAGFlow applies chunking per dataset (chosen when it was created), so ``chunking`` is not needed here.
        document_id = self.upload_document(dataset_id, filename, io.BytesIO(data))
        self.start_parsing(dataset_id, [document_id])
        return DocumentRef(document_id)

    def document_state(self, dataset_id: str, ref: DocumentRef) -> DocumentState:
        info = self.document_status(dataset_id, ref.document_id)
        run = str(info.get("run") or "").upper()
        if run in ("DONE", "3"):
            return DocumentState("indexed", int(info.get("chunk_count") or 0))
        if run in ("FAIL", "4", "CANCEL", "2"):
            return DocumentState("failed", 0, str(info.get("message") or "知识引擎解析失败")[:500])
        return DocumentState("processing")

    def list_chunks(self, dataset_id: str, document_id: str, *, page: int, size: int) -> tuple[list[str], int]:
        data = self._request(
            "GET", f"/datasets/{dataset_id}/documents/{document_id}/chunks", params={"page": page, "page_size": size}
        )
        chunks = [str(chunk.get("content") or "") for chunk in (data or {}).get("chunks", [])]
        return chunks, int((data or {}).get("total") or len(chunks))

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
        for document_id in document_ids:  # one by one: a missing id fails the whole bulk call
            try:
                self._request("DELETE", f"/datasets/{dataset_id}/documents", json={"ids": [document_id]})
            except KnowledgeEngineError as exc:
                if NOT_FOUND not in str(exc) and "not found" not in str(exc).lower():
                    raise  # a document that is already gone is fine; anything else is a real failure

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
