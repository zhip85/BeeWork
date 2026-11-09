"""The knowledge engine seam: BeeWork talks to RAGFlow or Dify through this one interface.

Parsing, chunking, embedding and retrieval run inside the engine. BeeWork keeps the business records and maps each
knowledge base to engine "datasets" (RAGFlow dataset / Dify knowledge base). ``KNOWLEDGE_ENGINE`` picks the engine
for new knowledge bases; each knowledge base remembers the engine its datasets live in, so changing the setting never
orphans existing ones.
"""

from dataclasses import dataclass
from typing import Literal, Protocol

from beework.config import settings

ENGINES = ("ragflow", "dify")


class KnowledgeEngineError(RuntimeError):
    pass


@dataclass(frozen=True)
class DocumentRef:
    """Where a document lives in the engine; ``batch_id`` is the extra handle some engines need to poll it."""

    document_id: str
    batch_id: str | None = None


@dataclass(frozen=True)
class DocumentState:
    status: Literal["processing", "indexed", "failed"]
    chunks: int = 0
    message: str | None = None


def faq_text(question: str, similar_questions: list[str], answer: str) -> str:
    """The text an FAQ is indexed as: question, similar phrasings, then the answer."""
    similar = "；".join(similar_questions)
    return f"{question}\n{similar}\n\n答：{answer}" if similar else f"{question}\n\n答：{answer}"


@dataclass(frozen=True)
class EngineChunk:
    """One retrieved passage; ``score`` is the engine's similarity in 0..1."""

    dataset_id: str
    chunk_id: str
    document_name: str
    text: str
    score: float
    document_id: str = ""  # lets BeeWork map a hit back to the FAQ (or document) it came from


class KnowledgeEngine(Protocol):
    """What knowledge bases need from an engine. Every method raises ``KnowledgeEngineError`` on failure."""

    def __enter__(self) -> "KnowledgeEngine": ...

    def __exit__(self, *_exc: object) -> None: ...

    def health(self) -> bool: ...

    def create_knowledge_dataset(self, name: str, *, faq: bool, chunking: str, embedding_model: str) -> str:
        """Create a dataset for documents (``faq=False``) or question–answer pairs and return its id."""
        ...

    def find_dataset(self, name: str) -> str | None:
        """Id of the dataset with exactly this name, so a retried creation reuses what an earlier attempt made."""
        ...

    def delete_datasets(self, dataset_ids: list[str]) -> None:
        """Delete datasets; ones that are already gone are not an error, so the call can be repeated."""
        ...

    def add_document(self, dataset_id: str, filename: str, data: bytes, *, chunking: str) -> DocumentRef:
        """Upload a file and start parsing / indexing it with the given chunking strategy."""
        ...

    def document_state(self, dataset_id: str, ref: DocumentRef) -> DocumentState: ...

    def delete_documents(self, dataset_id: str, document_ids: list[str]) -> None:
        """Remove documents; ones that are already gone are not an error."""
        ...

    def list_chunks(self, dataset_id: str, document_id: str, *, page: int, size: int) -> tuple[list[str], int]:
        """One page of a document's chunk texts and the total chunk count (for the chunk preview)."""
        ...

    def add_faq(self, dataset_id: str, question: str, similar_questions: list[str], answer: str) -> DocumentRef:
        """Push one FAQ as a single-chunk document of an FAQ dataset (the engine indexes it for retrieval).

        Dify's segment API is a paid-plan feature, so each FAQ is its own small document; ``delete_documents`` removes
        it. The question and its similar phrasings come first so they dominate the match; the answer follows.
        """
        ...

    def search(self, dataset_ids: list[str], query: str, *, top_k: int, threshold: float) -> list[EngineChunk]:
        """Passages of the given datasets most similar to ``query`` with ``score >= threshold``, best first."""
        ...


def default_engine() -> str:
    return settings.knowledge_engine if settings.knowledge_engine in ENGINES else "dify"


def engine_configured(engine: str | None = None) -> bool:
    engine = engine or default_engine()
    if engine == "dify":
        return bool(settings.dify_base_url and settings.dify_api_key)
    return bool(settings.ragflow_base_url and settings.ragflow_api_key)


def open_engine(engine: str | None = None) -> KnowledgeEngine:
    engine = engine or default_engine()
    if engine == "dify":
        from beework.integrations.dify import DifyClient

        return DifyClient()
    from beework.integrations.ragflow import RAGFlowClient

    return RAGFlowClient()
