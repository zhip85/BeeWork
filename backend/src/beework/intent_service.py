"""L2 intent judgement around the database (T-161): the agent's scope and candidates from the capability registry (T-160), the executor
calls of path A, FAQ answers, and the conversation state a Dify Chatflow leaves behind.

Nothing outside the agent's scope is ever a candidate, a rule target or a call: the scope is what the agent mounted and ticked."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework import capability_registry as registry
from beework.models.capability_registry import CapabilityEntry
from beework.models.faq import Faq
from beework.models.runtime import Run
from beework.runtime.dsl import AssistantDSLv1
from beework.runtime.intent import Candidate

FAQ_MARGIN = 0.05  # an FAQ hit must lead the next one by this much to be answered directly


def tool_name_for(entry: CapabilityEntry) -> str:
    """The executor tool (T-071) that calls an entry. Sub-agents are called as ``agent:{id}``, Dify applications as ``connection:{id}``."""
    if entry.kind == "mcp_tool":
        return f"mcp:{entry.source_id}:{entry.identifier}"
    if entry.kind == "api_tool":
        return f"api:{entry.source_id}:{entry.identifier}"
    if entry.kind == "skill":
        return f"skill:{entry.source_id}"
    if entry.kind in ("dify_chatflow", "dify_workflow"):
        return f"connection:{entry.source_id}"
    if entry.kind == "agent":
        return f"agent:{entry.source_id}"
    return ""  # a knowledge base is not called as a tool


def candidate_of(entry: CapabilityEntry, score: float = 0.0) -> Candidate:
    return Candidate(
        id=str(entry.id), kind=entry.kind, name=entry.name, description=entry.description, intent_description=entry.intent_description,
        examples=tuple(entry.intent_examples or []), direct=entry.direct_enabled, writes=entry.writes, input_schema=entry.input_schema or {},
        recall_score=score, source_id=str(entry.source_id), tool=tool_name_for(entry), direct_output=entry.direct_output,
    )


def config_from_dsl(dsl: AssistantDSLv1) -> dict[str, Any]:
    """The registry's view of a frozen DSL: what the agent mounted (the DSL only carries sub-agent mounts while collaboration is on),
    the tools ticked per mount, and its knowledge bases."""
    mounts: dict[str, list[str]] = {}
    settings: dict[str, dict[str, Any]] = {}
    for mount in dsl.tools.mounts:
        mounts.setdefault(mount.kind, []).append(str(mount.capability_id))
        settings[str(mount.capability_id)] = {"tools": list(mount.tools)}
    if dsl.knowledge.bases:
        mounts["kb"] = [str(base.kb_id) for base in dsl.knowledge.bases]
    return {"mounts": mounts, "mount_settings": settings, "multi_agent": {"enabled": bool(mounts.get("agent"))}}


@dataclass
class Scope:
    """What the agent can see, read once per run."""

    entries: dict[str, CapabilityEntry]  # id -> entry (available ones only)

    def callable_by_id(self) -> dict[str, Candidate]:
        """What a priority rule may target and path A may call: directly enabled, available and not a write (a write needs the user's
        confirmation, which only the planner can ask for)."""
        return {i: candidate_of(e) for i, e in self.entries.items() if e.direct_enabled and not e.writes and e.kind != "kb"}


def load_scope(db: Session, tenant_id: UUID, config: dict[str, Any]) -> Scope:
    return Scope(entries={str(item.entry.id): item.entry for item in registry.visible_entries(db, tenant_id, config)})


def build_candidates(db: Session, tenant_id: UUID, config: dict[str, Any], scope: Scope, query: str, k: int) -> list[Candidate]:
    """The candidates the classifier sees. A small scope goes to the classifier whole (a paraphrase that shares no word with a
    description would otherwise be lost to the keyword recall); a large one is narrowed to the best ``k`` of the recall."""
    ranked = registry.recall(db, tenant_id, config, query, k=50)
    scores = {str(item.entry.id): item.score for item in ranked}
    if len(scope.entries) <= 2 * k:
        chosen = sorted(scope.entries.values(), key=lambda e: (-scores.get(str(e.id), 0.0), e.name.lower(), str(e.id)))
    else:
        chosen = [item.entry for item in ranked[:k]]
    return [candidate_of(entry, scores.get(str(entry.id), 0.0)) for entry in chosen]


def load_pending(db: Session, run: Run) -> str | None:
    """The registry entry id of a Chatflow that is waiting for this conversation's next message (T-163): recorded in the previous run's
    ``route.pending`` and kept alive by the connection — sticky, still usable, its Dify conversation bound and not idle for too long,
    and the new message not an exit word (an exit word closes the binding). Only the immediately preceding run counts."""
    from beework.flow_connection_service import sticky_connection
    from beework.models.capability_registry import CapabilityEntry

    if run.conversation_id is None:
        return None
    previous = db.scalar(
        select(Run).where(
            Run.conversation_id == run.conversation_id, Run.id != run.id, Run.status == "succeeded", Run.created_at <= run.created_at
        ).order_by(Run.created_at.desc(), Run.id.desc()).limit(1)
    )
    pending = ((previous.output or {}).get("route") or {}).get("pending") if previous is not None else None
    if not (isinstance(pending, dict) and pending.get("entry_id")):
        return None
    try:
        entry = db.get(CapabilityEntry, UUID(str(pending["entry_id"])))
    except ValueError:
        return None
    if entry is None or entry.kind != "dify_chatflow":
        return None
    text = str((run.input or {}).get("message") or "")
    conn = sticky_connection(db, run.tenant_id, entry.source_id, run.conversation_id, text)
    db.commit()  # an exit word closed the binding
    return str(entry.id) if conn is not None else None


def faq_for_document(db: Session, tenant_id: UUID, document_id: str) -> Faq | None:
    if not document_id:
        return None
    return db.scalar(select(Faq).where(Faq.tenant_id == tenant_id, Faq.engine_document_id == document_id, Faq.status == "published", Faq.deleted_at.is_(None)))
