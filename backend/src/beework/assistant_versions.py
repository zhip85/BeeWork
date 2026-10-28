"""Publishing, version snapshots, diffs, restore and rollback of assistants (T-024a).

Without an approval engine (T-022) a publish takes effect directly, which is what PRD OC-004 prescribes
for tenants that configured no approval flow. ``execute_publish`` is the executor an approval would call.
"""

import difflib
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.models import AuditEvent, utc_now
from beework.models.assistants import Assistant, AssistantVersion
from beework.schemas.assistants import AssistantDraftConfig

SNAPSHOT_VERSION = 1
BASIC_FIELDS = ("name", "description", "category", "icon", "color", "visibility", "visible_org_ids")
EXPERIENCE_FIELDS = ("greeting", "suggested_questions", "feedback_enabled", "default_language")


def build_snapshot(assistant: Assistant, visible_org_ids: list[UUID], dsl: dict[str, Any]) -> dict[str, Any]:
    """A complete, self-contained copy of what the assistant would run and show right now."""
    config = AssistantDraftConfig.model_validate(assistant.draft_config or {}).model_dump(mode="json")
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "name": assistant.name,
        "description": assistant.description,
        "category": assistant.category,
        "icon": assistant.icon,
        "color": assistant.color,
        "visibility": assistant.visibility,
        "visible_org_ids": sorted(str(item) for item in visible_org_ids),
        "model_id": config.get("model_id"),
        "config": config,
        "dsl": dsl,  # the frozen runtime configuration (AssistantDSLv1); runs execute this, never the draft
    }


# ---------------------------------------------------------------------------
# Diff.
# ---------------------------------------------------------------------------


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, child in value.items():
            out.update(_flatten(child, f"{prefix}.{key}" if prefix else key))
        return out
    return {prefix: value}


def _section(path: str) -> str | None:
    if path in BASIC_FIELDS:
        return "basics"
    if path == "config.prompt":
        return "prompt"
    if path in {"config.model_id", "model_id"}:
        return "model"
    if path.startswith("config.mounts") or path == "config.driven_by":
        return "capabilities"
    if path.startswith("config.oc."):
        parts = path.split(".")
        return f"oc.{parts[2]}" if parts[2] != "oc_schema_version" else None
    if path.removeprefix("config.") in EXPERIENCE_FIELDS and path.startswith("config."):
        return "experience"
    return None  # dsl, snapshot_version, dsl_version: derived or bookkeeping


def diff_snapshots(old: dict[str, Any] | None, new: dict[str, Any]) -> list[dict[str, Any]]:
    """Field-level differences grouped by section; prompt changes also carry a line diff."""
    before = _flatten(old or {})
    after = _flatten(new)
    changes: list[dict[str, Any]] = []
    for path in sorted(set(before) | set(after)):
        section = _section(path)
        if section is None or before.get(path) == after.get(path):
            continue
        change: dict[str, Any] = {
            "section": section,
            "field": path.removeprefix("config."),
            "from": before.get(path),
            "to": after.get(path),
        }
        if path == "config.prompt":
            change["lines"] = list(
                difflib.unified_diff(
                    str(before.get(path) or "").splitlines(),
                    str(after.get(path) or "").splitlines(),
                    lineterm="",
                    n=2,
                )
            )
        changes.append(change)
    return changes


# ---------------------------------------------------------------------------
# Versions.
# ---------------------------------------------------------------------------


def latest_version(db: Session, assistant_id: UUID) -> AssistantVersion | None:
    return db.scalar(
        select(AssistantVersion)
        .where(AssistantVersion.assistant_id == assistant_id)
        .order_by(AssistantVersion.version_seq.desc())
        .limit(1)
    )


def live_version(db: Session, assistant: Assistant) -> AssistantVersion | None:
    if assistant.live_version_id is None:
        return None
    return db.scalar(
        select(AssistantVersion).where(
            AssistantVersion.id == assistant.live_version_id, AssistantVersion.tenant_id == assistant.tenant_id
        )
    )


def version_label(seq: int) -> str:
    return f"1.{seq - 1}"  # vX.Y: 1.0 for the first publish, then 1.1, 1.2, ...


def execute_publish(
    db: Session,
    assistant: Assistant,
    snapshot: dict[str, Any],
    *,
    change_note: str,
    kind: str,
    actor_user_id: UUID,
    actor_name: str,
    source_version_id: UUID | None = None,
    approval_id: UUID | None = None,
) -> AssistantVersion:
    """Write the immutable version, make it live and mark the assistant published. Caller commits."""
    previous = latest_version(db, assistant.id)
    live = live_version(db, assistant)
    seq = (previous.version_seq if previous else 0) + 1
    version = AssistantVersion(
        tenant_id=assistant.tenant_id,
        assistant_id=assistant.id,
        version_seq=seq,
        version=version_label(seq),
        kind=kind,
        source_version_id=source_version_id,
        snapshot=snapshot,
        change_note=change_note.strip()[:500],
        change_diff=diff_snapshots(live.snapshot if live else None, snapshot),
        approval_id=approval_id,
        published_by=actor_name,
        published_by_user_id=actor_user_id,
        published_at=utc_now(),
    )
    db.add(version)
    db.flush()
    assistant.live_version_id = version.id
    assistant.status = "published"
    assistant.published_at = version.published_at
    assistant.draft_version = version_label(seq + 1)  # the next version the draft would become
    assistant.row_version += 1
    assistant.updated_by = actor_name
    db.add(
        AuditEvent(
            tenant_id=assistant.tenant_id,
            actor_user_id=actor_user_id,
            action="assistant.published" if kind == "publish" else "assistant.rolled_back",
            details={
                "assistant_id": str(assistant.id),
                "name": assistant.name,
                "version": version.version,
                "version_id": str(version.id),
                **({"from_version_id": str(source_version_id)} if source_version_id else {}),
            },
        )
    )
    return version
