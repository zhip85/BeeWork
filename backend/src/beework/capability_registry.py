"""The unified capability registry (T-160, 演进 PRD 23.2): one tool-level list of everything an agent can use.

* **Derived, not duplicated.** MCP tools, API tools, Skills and Dify connections come from the capability centre; sub-agents
  and knowledge bases (FAQ included) come from their own pages. None of those sources changes shape: a flush listener
  recomputes the entries of a source in the same transaction whenever it changes, so every write path is covered and nothing
  has to remember to call a hook (the pattern of ``capability_catalog``).
* **Identity and ownership.** An entry is matched by ``(source_id, identifier)`` so its id never changes; the facts the
  source owns are overwritten on recomputation, the fields only the registry holds (intent description and examples,
  ``direct_enabled`` / ``direct_output``, timeout, retry) are never touched by it. A vanished source only marks the entry removed.
* **Status is read, not stored.** Whether an entry can be used now comes from the source (the mount resolvers of T-070 and the
  ticked tools), so it can never be stale.
* **Scope.** What an agent sees is what it mounted (and ticked); sub-agents only while collaboration is on. Recall works
  inside that scope.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import event, insert, inspect, select, update
from sqlalchemy.orm import Session

from beework.capabilities import MOUNT_RESOLVERS
from beework.config import settings
from beework.models import utc_now
from beework.models.assistants import Assistant
from beework.models.capabilities import ApiPlugin, Capability, McpConnection, Skill
from beework.models.capability_registry import CapabilityEntry
from beework.models.flow_connections import FlowConnection
from beework.models.knowledge import KnowledgeBase

KINDS = ("mcp_tool", "api_tool", "skill", "dify_workflow", "dify_chatflow", "agent", "kb")
# The mount key of an assistant config that each kind comes from (the same keys as T-070).
MOUNT_KEY = {
    "mcp_tool": "mcp", "api_tool": "api", "skill": "skill", "dify_workflow": "flow", "dify_chatflow": "chatflow", "agent": "agent", "kb": "kb",
}
# Path A (L2) may call these directly unless the administrator turns it off; a sub-agent / knowledge base only when turned on.
DIRECT_DEFAULT = {"mcp_tool": True, "api_tool": True, "skill": True, "dify_workflow": True, "dify_chatflow": True, "agent": False, "kb": False}
# Where the owner of an entry is managed — the registry only shows and links to it (sub-agents and knowledge bases have their own pages).
OWNER_PAGE = {"mcp_tool": "capabilities", "api_tool": "capabilities", "skill": "capabilities", "dify_workflow": "capabilities", "dify_chatflow": "capabilities", "agent": "assistants", "kb": "knowledge"}
RETRY_ON = ("network", "timeout", "5xx", "429")
def default_policy() -> dict[str, Any]:
    """The platform default of a call's policy (``settings.tool_*``, read when asked so a changed setting applies to every path)."""
    return {"timeout_ms": settings.tool_timeout_ms, "max_attempts": settings.tool_max_attempts, "backoff_ms": settings.tool_backoff_ms, "retry_on": []}
MAX_EXAMPLES = 10


@dataclass
class Desired:
    """What a source says an entry looks like right now."""

    kind: str
    identifier: str
    name: str
    description: str
    input_schema: dict[str, Any]
    writes: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


def _clip(value: Any, size: int) -> str:
    return str(value or "")[:size]


# ---------------------------------------------------------------------------
# What each source offers.
# ---------------------------------------------------------------------------


def _from_capability(session: Session, cap: Capability) -> list[Desired]:
    if cap.deleted_at is not None:
        return []
    if cap.kind == "mcp":
        conn = session.get(McpConnection, cap.id)
        tools = (conn.tools if conn else None) or []
        return [
            Desired("mcp_tool", _clip(t["name"], 120), _clip(t["name"], 200), _clip(t.get("description"), 500), t.get("input_schema") or {},
                    writes=not t.get("read_only", False), meta={"source_name": cap.name})
            for t in tools if t.get("name")
        ]
    if cap.kind == "api":
        plugin = session.get(ApiPlugin, cap.id)
        tools = (plugin.tools if plugin else None) or []
        return [
            Desired("api_tool", _clip(t["name"], 120), _clip(t["name"], 200), _clip(t.get("description"), 500), t.get("input_schema") or {},
                    writes=bool(t.get("writes")), meta={"source_name": cap.name, "api_type": plugin.api_type if plugin else "rest"})
            for t in tools if t.get("name")
        ]
    if cap.kind == "skill":
        skill = session.get(Skill, cap.id)
        return [
            Desired("skill", "", _clip(cap.name, 200), _clip(cap.description, 500),
                    {"type": "object", "properties": {"resource": {"type": "string", "maxLength": 300}}, "additionalProperties": False},
                    meta={"source_name": cap.name, "builtin": bool(skill and skill.platform_skill_id)})
        ]
    return []


def _from_connection(conn: FlowConnection) -> list[Desired]:
    if conn.deleted_at is not None:
        return []
    from beework.flow_connection_service import _spec  # the argument schema the tool already has

    kind = "dify_chatflow" if conn.kind == "chatflow" else "dify_workflow"
    return [Desired(kind, "", _clip(conn.name, 200), _clip(conn.description, 500), _spec(conn).parameters, meta={"source_name": conn.name})]


def _from_assistant(session: Session, assistant: Assistant) -> list[Desired]:
    """A sub-agent is an entry while it can be called as a leaf (published, not a team): that is the only way it is ever used."""
    from beework.agent_teams import leaf_state

    if assistant.deleted_at is not None or leaf_state(session, assistant)[0] != "available":
        return []
    return [
        Desired("agent", "", _clip(assistant.name, 200), _clip(assistant.description, 500),
                {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}, meta={"source_name": assistant.name})
    ]


def _from_kb(kb: KnowledgeBase) -> list[Desired]:
    if kb.deleted_at is not None:
        return []
    return [
        Desired("kb", "", _clip(kb.name, 200), _clip(kb.description, 500),
                {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                meta={"source_name": kb.name, "kb_type": kb.kb_type})
    ]


# ---------------------------------------------------------------------------
# Reconciliation (runs inside the flush that changed the source).
# ---------------------------------------------------------------------------

_FACTS = ("name", "description", "input_schema", "writes", "meta")


def reconcile_source(session: Session, tenant_id: UUID, source_id: UUID, kinds: tuple[str, ...], desired: list[Desired]) -> None:
    """Make the entries of one source match ``desired``: add what is new, refresh the facts the source owns, mark the vanished.
    Core statements only, so it is safe inside ``after_flush``."""
    existing = {
        (row.kind, row.identifier): row
        for row in session.execute(
            select(CapabilityEntry.id, CapabilityEntry.kind, CapabilityEntry.identifier, CapabilityEntry.removed_at,
                   CapabilityEntry.name, CapabilityEntry.description, CapabilityEntry.input_schema, CapabilityEntry.writes, CapabilityEntry.meta)
            .where(CapabilityEntry.tenant_id == tenant_id, CapabilityEntry.source_id == source_id, CapabilityEntry.kind.in_(kinds))
        )
    }
    now = utc_now()
    wanted = set()
    for item in desired:
        key = (item.kind, item.identifier)
        wanted.add(key)
        row = existing.get(key)
        facts = {"name": item.name, "description": item.description, "input_schema": item.input_schema, "writes": item.writes, "meta": item.meta}
        if row is None:
            session.execute(
                insert(CapabilityEntry).values(
                    id=uuid4(), tenant_id=tenant_id, kind=item.kind, source_id=source_id, identifier=item.identifier, **facts,
                    intent_description="", intent_examples=[], direct_enabled=DIRECT_DEFAULT[item.kind], direct_output=False,
                    row_version=1, created_at=now, updated_at=now,
                )
            )
        elif row.removed_at is not None or any(getattr(row, name) != value for name, value in facts.items()):
            session.execute(update(CapabilityEntry).where(CapabilityEntry.id == row.id).values(removed_at=None, updated_at=now, **facts))
    for key, row in existing.items():
        if key not in wanted and row.removed_at is None:
            session.execute(update(CapabilityEntry).where(CapabilityEntry.id == row.id).values(removed_at=now, updated_at=now))


CAPABILITY_KINDS = {"mcp": ("mcp_tool",), "api": ("api_tool",), "skill": ("skill",)}


def sync_capability(session: Session, cap: Capability) -> None:
    if cap.kind in CAPABILITY_KINDS:
        reconcile_source(session, cap.tenant_id, cap.id, CAPABILITY_KINDS[cap.kind], _from_capability(session, cap))


def sync_connection(session: Session, conn: FlowConnection) -> None:
    reconcile_source(session, conn.tenant_id, conn.id, ("dify_workflow", "dify_chatflow"), _from_connection(conn))


def sync_assistant(session: Session, assistant: Assistant) -> None:
    reconcile_source(session, assistant.tenant_id, assistant.id, ("agent",), _from_assistant(session, assistant))


def sync_kb(session: Session, kb: KnowledgeBase) -> None:
    reconcile_source(session, kb.tenant_id, kb.id, ("kb",), _from_kb(kb))


def reconcile_tenant(db: Session, tenant_id: UUID) -> int:
    """Recompute every entry of the tenant (the backfill of existing data, and the repair when something was changed behind the
    application's back). Returns the number of live entries afterwards."""
    for cap in db.scalars(select(Capability).where(Capability.tenant_id == tenant_id, Capability.kind.in_(tuple(CAPABILITY_KINDS)))):
        sync_capability(db, cap)
    for conn in db.scalars(select(FlowConnection).where(FlowConnection.tenant_id == tenant_id)):
        sync_connection(db, conn)
    for assistant in db.scalars(select(Assistant).where(Assistant.tenant_id == tenant_id)):
        sync_assistant(db, assistant)
    for kb in db.scalars(select(KnowledgeBase).where(KnowledgeBase.tenant_id == tenant_id)):
        sync_kb(db, kb)
    db.flush()
    return len(db.scalars(select(CapabilityEntry.id).where(CapabilityEntry.tenant_id == tenant_id, CapabilityEntry.removed_at.is_(None))).all())


def ensure_backfilled(db: Session, tenant_id: UUID) -> None:
    """A tenant that has no entry at all has never been reconciled (it predates the registry): do it now. A tenant that truly has
    nothing to register just pays one cheap pass."""
    if db.scalar(select(CapabilityEntry.id).where(CapabilityEntry.tenant_id == tenant_id).limit(1)) is None:
        reconcile_tenant(db, tenant_id)


def _changed(obj: object, *attributes: str) -> bool:
    state = inspect(obj)
    return any(state.attrs[name].history.has_changes() for name in attributes)


@event.listens_for(Session, "after_flush")
def _sync_after_flush(session: Session, _context) -> None:
    capabilities: dict[UUID, Capability] = {}
    for obj in [*session.new, *session.dirty]:
        if isinstance(obj, Capability) and obj.kind in CAPABILITY_KINDS:
            if obj in session.new or _changed(obj, "name", "description", "deleted_at"):
                capabilities[obj.id] = obj
        elif isinstance(obj, (McpConnection, ApiPlugin, Skill)):
            if obj in session.new or _changed(obj, *(("tools",) if not isinstance(obj, Skill) else ("platform_skill_id",))):
                cap = session.get(Capability, obj.capability_id)
                if cap is not None:
                    capabilities[cap.id] = cap
        elif isinstance(obj, FlowConnection):
            if obj in session.new or _changed(obj, "name", "description", "kind", "input_mapping", "parameters", "deleted_at"):
                sync_connection(session, obj)
        elif isinstance(obj, Assistant):
            if obj in session.new or _changed(obj, "name", "description", "status", "live_version_id", "deleted_at", "draft_config"):
                sync_assistant(session, obj)
        elif isinstance(obj, KnowledgeBase) and (obj in session.new or _changed(obj, "name", "description", "kb_type", "deleted_at")):
            sync_kb(session, obj)
    for cap in capabilities.values():
        sync_capability(session, cap)


# ---------------------------------------------------------------------------
# Reading: status, scope, recall, policy.
# ---------------------------------------------------------------------------


@dataclass
class Seen:
    """An entry with whether it can be used right now (read from its source)."""

    entry: CapabilityEntry
    status: str  # available | disabled | invalid


def effective_status(db: Session, tenant_id: UUID, entries: list[CapabilityEntry]) -> dict[UUID, str]:
    """``available`` when the source says so (the T-070 mount resolver) and, for an MCP / API tool, the tool is ticked; ``disabled``
    when the source exists but cannot serve; ``invalid`` when the source is gone."""
    result: dict[UUID, str] = {}
    by_kind: dict[str, list[CapabilityEntry]] = defaultdict(list)
    for entry in entries:
        by_kind[entry.kind].append(entry)
    for kind, group in by_kind.items():
        resolver = MOUNT_RESOLVERS.get(MOUNT_KEY[kind])
        ids = list({entry.source_id for entry in group})
        found = resolver(db, tenant_id, ids) if resolver else {}
        ticked: dict[UUID, set[str]] = {}
        if kind in ("mcp_tool", "api_tool"):
            model = McpConnection if kind == "mcp_tool" else ApiPlugin
            ticked = {row[0]: set(row[1] or []) for row in db.execute(select(model.capability_id, model.selected_tools).where(model.capability_id.in_(ids)))}
        for entry in group:
            if entry.removed_at is not None:
                result[entry.id] = "invalid"
                continue
            item = found.get(entry.source_id)
            if item is None:
                result[entry.id] = "invalid"
            elif item.state != "available":
                result[entry.id] = "disabled"
            elif kind in ("mcp_tool", "api_tool") and entry.identifier not in ticked.get(entry.source_id, set()):
                result[entry.id] = "disabled"
            else:
                result[entry.id] = "available"
    return result


def visible_entries(db: Session, tenant_id: UUID, config: dict[str, Any] | None, *, only_available: bool = True) -> list[Seen]:
    """What an assistant config can see: its mounts (sub-agents only while collaboration is on), narrowed by the tools ticked for
    each mount. This is the scope of path A and of L3 discovery."""
    ensure_backfilled(db, tenant_id)
    config = config or {}
    mounts = config.get("mounts") or {}
    settings = config.get("mount_settings") or {}
    team = bool((config.get("multi_agent") or {}).get("enabled"))
    entries: list[CapabilityEntry] = []
    for kind in KINDS:
        if kind == "agent" and not team:
            continue
        ids = []
        for raw in mounts.get(MOUNT_KEY[kind]) or []:
            try:
                ids.append(UUID(str(raw)))
            except ValueError:
                continue
        if not ids:
            continue
        rows = db.scalars(
            select(CapabilityEntry).where(
                CapabilityEntry.tenant_id == tenant_id, CapabilityEntry.kind == kind, CapabilityEntry.source_id.in_(ids), CapabilityEntry.removed_at.is_(None),
            ).order_by(CapabilityEntry.name, CapabilityEntry.id)
        )
        for entry in rows:
            allowed = (settings.get(str(entry.source_id)) or {}).get("tools") or []
            if kind in ("mcp_tool", "api_tool") and allowed and entry.identifier not in allowed:
                continue  # the mount ticks only some tools for this agent
            entries.append(entry)
    statuses = effective_status(db, tenant_id, entries)
    seen = [Seen(entry, statuses[entry.id]) for entry in entries]
    return [item for item in seen if item.status == "available"] if only_available else seen


CJK_RUN = re.compile(r"[㐀-鿿]+")
WORD = re.compile(r"[a-z0-9_]{2,}")


def tokens(text: str) -> set[str]:
    """Words and, for Chinese, every character pair (and a lone character): enough to match a short question to a short
    description without a segmenter."""
    text = (text or "").lower()
    found = set(WORD.findall(text))
    for run in CJK_RUN.findall(text):
        found.update(run[i : i + 2] for i in range(len(run) - 1))
        if len(run) == 1:
            found.add(run)
    return found


FIELD_WEIGHTS = (("name", 3.0), ("examples", 3.0), ("intent", 2.0), ("description", 1.0))


@dataclass
class Recalled:
    entry: CapabilityEntry
    score: float  # 0–1: how much of the query the entry explains, weighted by where it matched


def score_entry(query_tokens: set[str], query_text: str, entry: CapabilityEntry) -> float:
    if not query_tokens:
        return 0.0
    fields = {
        "name": tokens(entry.name), "examples": tokens(" ".join(entry.intent_examples or [])),
        "intent": tokens(entry.intent_description), "description": tokens(entry.description),
    }
    total = sum(max((weight for name, weight in FIELD_WEIGHTS if token in fields[name]), default=0.0) for token in query_tokens)
    score = total / (len(query_tokens) * FIELD_WEIGHTS[0][1])
    if entry.name and entry.name.lower() in query_text.lower():
        score = min(1.0, score + 0.2)  # the question names it
    return min(1.0, score)


def recall(db: Session, tenant_id: UUID, config: dict[str, Any] | None, query: str, k: int = 8) -> list[Recalled]:
    """The ``k`` entries of the assistant's scope that best fit ``query`` (only available ones), best first. A keyword match over name,
    examples, intent description and description — the interface a vector recall can replace later."""
    query_tokens = tokens(query)
    scored = [Recalled(item.entry, score_entry(query_tokens, query, item.entry)) for item in visible_entries(db, tenant_id, config)]
    scored = [item for item in scored if item.score > 0]
    scored.sort(key=lambda item: (-item.score, item.entry.name.lower(), str(item.entry.id)))
    return scored[: max(1, min(k, 50))]


def resolve_policy(entry: CapabilityEntry) -> dict[str, Any]:
    """Timeout and retry of an entry: its own settings over the platform default (``tools.policy`` applies it, T-165)."""
    policy = default_policy()
    if entry.timeout_ms:
        policy["timeout_ms"] = entry.timeout_ms
    if entry.retry:
        policy.update({key: entry.retry[key] for key in ("max_attempts", "backoff_ms", "retry_on") if key in entry.retry})
    return policy
