"""Knowledge retrieval seam between the runtime and RAGFlow (T-033).

The runtime only depends on ``Retriever``. Until knowledge bases exist (T-030/T-033) the only
implementation is ``StubRetriever``: it contacts nothing and every result is marked ``mode="stub"``
so tests, events, run records and the UI can say that real RAGFlow retrieval is NOT connected.
BeeWork must not grow its own document search.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol
from uuid import UUID


@dataclass(frozen=True)
class RetrievedChunk:
    document_name: str
    chunk_id: str
    text: str
    score: float


@dataclass(frozen=True)
class RetrievalResult:
    chunks: list[RetrievedChunk] = field(default_factory=list)
    mode: Literal["ragflow", "stub"] = "stub"


class Retriever(Protocol):
    def retrieve(
        self, *, tenant_id: UUID, query: str, dataset_ids: Sequence[str], top_k: int
    ) -> RetrievalResult: ...


class StubRetriever:
    """Placeholder retriever: no backend call, no chunks."""

    def retrieve(
        self, *, tenant_id: UUID, query: str, dataset_ids: Sequence[str], top_k: int
    ) -> RetrievalResult:
        return RetrievalResult(chunks=[], mode="stub")


def tenant_dataset_ids(tenant_id: UUID, requested: Sequence[str]) -> list[str]:
    """Keep only dataset ids that belong to ``tenant_id``.

    There is no knowledge-base table yet, so no dataset can be proven to belong to the tenant and
    none is allowed. T-033 replaces this with a lookup of the tenant's ``ragflow_dataset_id``s.
    """
    return []
