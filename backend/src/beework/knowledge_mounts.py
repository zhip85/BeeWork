"""Knowledge bases as an assistant capability (T-033): mount resolution, reference counting and runtime lookup.

``mounts.kb`` on an assistant holds knowledge base ids. At run time the ids are re-resolved against the current
rows of the tenant, so the datasets used are always the tenant's own, whatever an old snapshot recorded.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.capabilities import MOUNT_RESOLVERS, MountItem
from beework.knowledge import REFERENCE_COUNTERS, content_stats, derive_status
from beework.models.assistants import Assistant, AssistantVersion
from beework.models.knowledge import KnowledgeBase
from beework.runtime.dsl import KnowledgeBaseRef


def _live(db: Session, tenant_id: UUID, ids: list[UUID]) -> dict[UUID, KnowledgeBase]:
    if not ids:
        return {}
    return {
        kb.id: kb
        for kb in db.scalars(
            select(KnowledgeBase).where(
                KnowledgeBase.tenant_id == tenant_id, KnowledgeBase.id.in_(ids), KnowledgeBase.deleted_at.is_(None)
            )
        )
    }


def resolve_kb_mounts(db: Session, tenant_id: UUID, ids: list[UUID]) -> dict[UUID, MountItem]:
    """GLB-083: a knowledge base that was deleted is not returned (the check reports it); one with no indexed
    content is ``disabled``; anything else can serve."""
    items: dict[UUID, MountItem] = {}
    for kb_id, kb in _live(db, tenant_id, ids).items():
        stats = content_stats(db, kb)
        status = derive_status(kb, stats)
        items[kb_id] = MountItem(
            id=kb_id,
            kind="kb",
            name=kb.name,
            state="disabled" if status == "draft" else "available",
            meta={"kb_type": kb.kb_type, "documents": stats.documents, "faqs": stats.faqs, "status": status},
        )
    return items


MOUNT_RESOLVERS["kb"] = resolve_kb_mounts


def mounted_kb_ids(config: dict | None) -> set[str]:
    return {str(value) for value in ((config or {}).get("mounts") or {}).get("kb") or []}


def count_references(db: Session, kb: KnowledgeBase) -> int:
    """Assistants whose draft or live version mounts the knowledge base."""
    target = str(kb.id)
    count = 0
    for assistant in db.scalars(
        select(Assistant).where(Assistant.tenant_id == kb.tenant_id, Assistant.deleted_at.is_(None))
    ):
        in_live = False
        if assistant.live_version_id is not None:
            live = db.get(AssistantVersion, assistant.live_version_id)
            in_live = bool(live and target in mounted_kb_ids((live.snapshot or {}).get("config")))
        if target in mounted_kb_ids(assistant.draft_config) or in_live:
            count += 1
    return count


REFERENCE_COUNTERS.append(count_references)


def resolve_knowledge_bases(db: Session, tenant_id: UUID, kb_ids: list[UUID]) -> list[KnowledgeBaseRef]:
    """The tenant's current knowledge bases for ``kb_ids`` that can be searched (datasets exist), in the given order.

    Ids that belong to another tenant, were deleted or have not been synced to the engine yet are simply absent.
    A dataset with nothing indexed in it is left out too: Dify answers a search on an empty dataset with an error
    (``400 … 404 Not Found``), and there is nothing to find there anyway. So every standard knowledge base no longer
    makes a pointless FAQ call just because its FAQ dataset exists.
    """
    found = _live(db, tenant_id, kb_ids)
    refs: list[KnowledgeBaseRef] = []
    for kb_id in kb_ids:
        kb = found.get(kb_id)
        if kb is None:
            continue
        stats = content_stats(db, kb)
        dataset_id = kb.engine_dataset_id if stats.indexed_documents > 0 else None
        faq_dataset_id = kb.engine_faq_dataset_id if stats.published_faqs > 0 else None
        if not (dataset_id or faq_dataset_id):
            continue
        refs.append(
            KnowledgeBaseRef(
                kb_id=kb.id,
                name=kb.name,
                kb_type=kb.kb_type,
                engine=kb.engine,
                dataset_id=dataset_id,
                faq_dataset_id=faq_dataset_id,
                threshold=float(kb.retrieval_threshold),
            )
        )
    return refs
