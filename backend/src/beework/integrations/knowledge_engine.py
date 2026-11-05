"""The knowledge engine seam: BeeWork talks to RAGFlow or Dify through this one interface.

Parsing, chunking, embedding and retrieval run inside the engine. BeeWork keeps the business records and maps each
knowledge base to engine "datasets" (RAGFlow dataset / Dify knowledge base). ``KNOWLEDGE_ENGINE`` picks the engine
for new knowledge bases; each knowledge base remembers the engine its datasets live in, so changing the setting never
orphans existing ones.
"""

from typing import Protocol

from beework.config import settings

ENGINES = ("ragflow", "dify")


class KnowledgeEngineError(RuntimeError):
    pass


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


def default_engine() -> str:
    return settings.knowledge_engine if settings.knowledge_engine in ENGINES else "ragflow"


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
