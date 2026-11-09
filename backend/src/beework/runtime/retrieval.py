"""Knowledge retrieval for the runtime (T-033): FAQ first, then documents, through the knowledge engine.

The runtime depends only on ``Retriever``. ``EngineRetriever`` searches the datasets of the assistant's mounted
knowledge bases in RAGFlow or Dify; ``StubRetriever`` (tests) contacts nothing and marks every result ``mode="stub"``.
``mode`` says what produced a result: ``none`` (no knowledge base mounted), an engine name, or ``stub``.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from beework.integrations.knowledge_engine import EngineChunk, KnowledgeEngine, open_engine
from beework.runtime.dsl import KnowledgeBaseRef


@dataclass(frozen=True)
class RetrievedChunk:
    document_name: str
    chunk_id: str
    text: str
    score: float
    kind: str = "document"  # "faq": a standard answer; "document": a passage
    kb_name: str = ""
    document_id: str = ""  # the engine document it came from (an FAQ is one document)


@dataclass(frozen=True)
class RetrievalResult:
    chunks: list[RetrievedChunk] = field(default_factory=list)
    mode: str = "none"


class Retriever(Protocol):
    def retrieve(
        self, *, tenant_id: UUID, query: str, bases: Sequence[KnowledgeBaseRef], top_k: int
    ) -> RetrievalResult: ...


class StubRetriever:
    """Test double: no backend call, no chunks."""

    def retrieve(
        self, *, tenant_id: UUID, query: str, bases: Sequence[KnowledgeBaseRef], top_k: int
    ) -> RetrievalResult:
        return RetrievalResult(chunks=[], mode="stub")


class EngineRetriever:
    """Searches only the datasets of the knowledge bases it is given (which the caller resolved for the tenant)."""

    def __init__(self, open_client: Callable[[str], KnowledgeEngine] = open_engine) -> None:
        self._open = open_client

    def retrieve(
        self, *, tenant_id: UUID, query: str, bases: Sequence[KnowledgeBaseRef], top_k: int
    ) -> RetrievalResult:
        if not bases:
            return RetrievalResult(chunks=[], mode="none")
        faq_hits: list[RetrievedChunk] = []
        doc_hits: list[RetrievedChunk] = []
        engines: list[str] = []
        for engine in dict.fromkeys(base.engine for base in bases):
            engines.append(engine)
            with self._open(engine) as client:
                for base in (b for b in bases if b.engine == engine):
                    if base.faq_dataset_id:
                        faq_hits += self._search(client, base, base.faq_dataset_id, query, top_k, "faq")
                    if base.dataset_id:
                        doc_hits += self._search(client, base, base.dataset_id, query, top_k, "document")
        # A confident FAQ match is the standard answer; passages are only the fallback.
        chosen = faq_hits or doc_hits
        chosen.sort(key=lambda chunk: chunk.score, reverse=True)
        return RetrievalResult(chunks=chosen[:top_k], mode="+".join(engines))

    @staticmethod
    def _search(
        client: KnowledgeEngine, base: KnowledgeBaseRef, dataset_id: str, query: str, top_k: int, kind: str
    ) -> list[RetrievedChunk]:
        found: list[EngineChunk] = client.search([dataset_id], query, top_k=top_k, threshold=base.threshold)
        return [
            RetrievedChunk(
                document_name=chunk.document_name, chunk_id=chunk.chunk_id, text=chunk.text,
                score=chunk.score, kind=kind, kb_name=base.name, document_id=chunk.document_id,
            )
            for chunk in found
        ]
