"""Tenant glossary (T-053, SET-060–063): the tenant layer of the term inheritance.

Terms are never copied from the platform: an inherited term is read from the platform at query time, so platform edits
reach every tenant that has not overridden or hidden the term. Changes go through the “智能体规则变更” approval policy,
exactly like the tenant PII rules (T-052).
"""

from typing import Any
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.approvals import ApplyContext, approval_action
from beework.errors import ApiError
from beework.glossary import clean_term
from beework.governance import actor_name
from beework.governance.term_inheritance import (
    EffectiveTerm,
    Term,
    TermView,
    industry_terms,
    tenant_terms,
)
from beework.models import AuditEvent, Tenant
from beework.models.glossary import Industry, IndustryGlossary, PlatformTerm
from beework.models.tenant_glossary import TenantGlossarySettings, TenantTerm

ACTION = "tenant.glossary"
OBJECT_TYPE = "tenant_term"
EVENT = "rule"  # approval policy event “智能体规则变更”
MAX_TENANT_TERMS = 2000


def blocked(message: str) -> ApiError:
    return ApiError(409, "tenant_glossary.blocked", message)


# ---------------------------------------------------------------------------
# Reading.
# ---------------------------------------------------------------------------


def get_settings(db: Session, tenant_id: UUID) -> TenantGlossarySettings | None:
    return db.get(TenantGlossarySettings, tenant_id)


def selected_glossaries(db: Session, tenant_id: UUID) -> list[IndustryGlossary]:
    """The inherited glossaries in the tenant's order; ones that no longer exist are skipped."""
    row = get_settings(db, tenant_id)
    ids = [UUID(item) for item in (row.glossary_ids if row else [])]
    found = {g.id: g for g in db.scalars(select(IndustryGlossary).where(IndustryGlossary.id.in_(ids)))} if ids else {}
    return [found[i] for i in ids if i in found]


def _industry_term(term: PlatformTerm) -> Term:
    return Term(id=str(term.id), layer="industry", term=term.term, aliases=tuple(term.aliases or []), definition=term.definition, source=str(term.glossary_id))


def industry_layer(db: Session, tenant_id: UUID) -> list[Term]:
    glossaries = []
    for glossary in selected_glossaries(db, tenant_id):
        rows = db.scalars(select(PlatformTerm).where(PlatformTerm.glossary_id == glossary.id).order_by(PlatformTerm.term_key))
        glossaries.append([_industry_term(row) for row in rows])
    return industry_terms(glossaries)


def tenant_rows(db: Session, tenant_id: UUID) -> list[TenantTerm]:
    return list(db.scalars(select(TenantTerm).where(TenantTerm.tenant_id == tenant_id).order_by(TenantTerm.created_at, TenantTerm.id)))


def _tenant_term(row: TenantTerm) -> Term:
    return Term(
        id=str(row.id), layer="tenant", term=row.term, aliases=tuple(row.aliases or []), definition=row.definition,
        from_id=str(row.from_id) if row.from_id else None, hidden=row.hidden,
    )


def tenant_view(db: Session, tenant_id: UUID) -> TermView:
    return tenant_terms(industry_layer(db, tenant_id), [_tenant_term(row) for row in tenant_rows(db, tenant_id)])


def summary(db: Session, tenant_id: UUID) -> dict[str, Any]:
    """The numbers of the inheritance chain header (SET-060)."""
    glossaries = selected_glossaries(db, tenant_id)
    names = []
    for glossary in glossaries:
        industry = db.get(Industry, glossary.industry_code)
        names.append(industry.name_zh if industry else glossary.name)
    view = tenant_view(db, tenant_id)
    rows = tenant_rows(db, tenant_id)
    return {
        "industries": list(dict.fromkeys(names)), "industry_terms": len(industry_layer(db, tenant_id)),
        "inherited": sum(1 for item in view.terms if item.label != "tenant"),
        "tenant_level": sum(1 for row in rows if not row.hidden), "total": len(view.terms), "hidden": len(view.hidden),
    }


def inheriting_tenant_ids(db: Session, glossary_id: UUID) -> list[UUID]:
    return [row.tenant_id for row in db.scalars(select(TenantGlossarySettings)) if str(glossary_id) in (row.glossary_ids or [])]


def preset_for_tenant(db: Session, tenant: Tenant) -> None:
    """Provisioning (T-100): a new tenant inherits the glossaries of its industry (SET-061)."""
    ids = [str(g.id) for g in db.scalars(select(IndustryGlossary).where(IndustryGlossary.industry_code == tenant.industry).order_by(IndustryGlossary.sort_order))]
    if ids and get_settings(db, tenant.id) is None:
        db.add(TenantGlossarySettings(tenant_id=tenant.id, glossary_ids=ids))


# ---------------------------------------------------------------------------
# Writing (shared by the direct path and the approval executor).
# ---------------------------------------------------------------------------


def _audit(db: Session, tenant_id: UUID, user_id: UUID | None, action: str, name: str, **details: str) -> None:
    db.add(AuditEvent(tenant_id=tenant_id, actor_user_id=user_id, action=action, details={"name": name, **details}))


def _inherited(db: Session, tenant_id: UUID) -> dict[str, Term]:
    return {term.id: term for term in industry_layer(db, tenant_id)}


def _ensure_unique(db: Session, tenant_id: UUID, term: str, exclude_key: str | None = None) -> None:
    """A standard term is unique among the terms the tenant has (inherited ones it kept, overrides and its own)."""
    lowered = term.strip().lower()
    for item in tenant_view(db, tenant_id).terms:
        if item.id != exclude_key and item.term.key == lowered:
            raise ApiError(409, "tenant_glossary.duplicate_term", "本租户术语表中已有同名标准术语")


def _marker(db: Session, tenant_id: UUID, platform_id: UUID) -> TenantTerm | None:
    return db.scalar(select(TenantTerm).where(TenantTerm.tenant_id == tenant_id, TenantTerm.from_id == platform_id))


def _own(db: Session, tenant_id: UUID, key: UUID) -> TenantTerm | None:
    return db.scalar(select(TenantTerm).where(TenantTerm.tenant_id == tenant_id, TenantTerm.id == key, TenantTerm.from_id.is_(None)))


def _fill(row: TenantTerm, term: str, aliases: list[str], definition: str, actor: str) -> None:
    row.term, row.term_key, row.aliases, row.definition = term, term.lower(), aliases, definition
    row.hidden = False
    row.row_version = (row.row_version or 0) + 1
    row.updated_by = actor


def apply_change(db: Session, tenant_id: UUID, user_id: UUID | None, operation: str, key: UUID | None, fields: dict[str, Any] | None) -> str:
    """Write one change; returns what happened (``created`` / ``override_created`` / ``updated`` / ``restored`` /
    ``hidden`` / ``deleted`` / ``restored_hidden`` / ``settings`` / ``imported``)."""
    actor = actor_name(db, user_id) if user_id else ""
    fields = fields or {}

    if operation == "settings":
        wanted = [str(item) for item in fields.get("glossary_ids", [])]
        known = {str(i) for i in db.scalars(select(IndustryGlossary.id))}
        if len(set(wanted)) != len(wanted) or any(item not in known for item in wanted):
            raise ApiError(422, "tenant_glossary.invalid", "所选行业术语库不存在或重复")
        row = get_settings(db, tenant_id)
        if row is None:
            db.add(TenantGlossarySettings(tenant_id=tenant_id, glossary_ids=wanted, updated_by=actor))
        else:
            row.glossary_ids, row.row_version, row.updated_by = wanted, row.row_version + 1, actor
        _audit(db, tenant_id, user_id, "tenant_glossary_settings.updated", "继承的行业术语表", glossaries=str(len(wanted)))
        return "settings"

    if operation == "create":
        term, aliases, definition = clean_term(fields.get("term", ""), fields.get("aliases", []), fields.get("definition", ""))
        _ensure_unique(db, tenant_id, term)
        if len(tenant_rows(db, tenant_id)) >= MAX_TENANT_TERMS:
            raise ApiError(422, "tenant_glossary.too_many", f"租户术语最多 {MAX_TENANT_TERMS} 条")
        row = TenantTerm(tenant_id=tenant_id, from_id=None)
        _fill(row, term, aliases, definition, actor)
        row.row_version = 1
        db.add(row)
        _audit(db, tenant_id, user_id, "tenant_term.created", term)
        return "created"

    if operation == "restore_hidden":
        markers = [r for r in tenant_rows(db, tenant_id) if r.hidden]
        for row in markers:
            db.delete(row)
        _audit(db, tenant_id, user_id, "tenant_term.restored", "全部隐藏的继承术语", count=str(len(markers)))
        return "restored_hidden"

    if operation == "import":
        return _import(db, tenant_id, user_id, actor, fields)

    assert key is not None
    inherited = _inherited(db, tenant_id)
    own = _own(db, tenant_id, key)
    platform = inherited.get(str(key))

    if operation == "update":
        term, aliases, definition = clean_term(fields.get("term", ""), fields.get("aliases", []), fields.get("definition", ""))
        if own is not None:
            _ensure_unique(db, tenant_id, term, exclude_key=str(own.id))
            _fill(own, term, aliases, definition, actor)
            _audit(db, tenant_id, user_id, "tenant_term.updated", term)
            return "updated"
        if platform is None:
            raise ApiError(404, "not_found", "未找到术语")
        _ensure_unique(db, tenant_id, term, exclude_key=str(key))
        existing = _marker(db, tenant_id, key)
        if existing is not None and not existing.hidden:
            _fill(existing, term, aliases, definition, actor)
            _audit(db, tenant_id, user_id, "tenant_term.updated", term, overrides=platform.term)
            return "updated"
        row = existing or TenantTerm(tenant_id=tenant_id, from_id=key)
        _fill(row, term, aliases, definition, actor)
        if existing is None:
            row.row_version = 1
            db.add(row)
        _audit(db, tenant_id, user_id, "tenant_term.overridden", term, overrides=platform.term)
        return "override_created"

    if operation == "restore":
        row = _marker(db, tenant_id, key)
        if row is None or row.hidden:
            raise ApiError(409, "tenant_glossary.nothing_to_restore", "该术语没有租户级修改")
        _audit(db, tenant_id, user_id, "tenant_term.restored", row.term)
        db.delete(row)
        return "restored"

    if operation == "delete":
        if own is not None:
            _audit(db, tenant_id, user_id, "tenant_term.deleted", own.term)
            db.delete(own)
            return "deleted"
        if platform is None:
            raise ApiError(404, "not_found", "未找到术语")
        # An inherited term (or the tenant's override of it) is hidden, never removed from the platform (OC-035).
        row = _marker(db, tenant_id, key)
        if row is None:
            row = TenantTerm(tenant_id=tenant_id, from_id=key, term=platform.term, term_key=platform.key, aliases=[], definition="", hidden=True, updated_by=actor, row_version=1)
            db.add(row)
        else:
            row.hidden = True
            row.row_version += 1
            row.updated_by = actor
        _audit(db, tenant_id, user_id, "tenant_term.hidden", platform.term)
        return "hidden"
    raise ValueError(f"Unknown operation: {operation}")


def _import(db: Session, tenant_id: UUID, user_id: UUID | None, actor: str, fields: dict[str, Any]) -> str:
    """SET-063: terms parsed by the shared parser become tenant terms; ones the tenant already has are skipped or overwritten.

    The tenant's view is computed once; new terms are added directly, only overwrites go through ``apply_change``.
    """
    on_duplicate = fields.get("on_duplicate", "skip")
    taken = {item.term.key: item for item in tenant_view(db, tenant_id).terms}
    capacity = MAX_TENANT_TERMS - len(tenant_rows(db, tenant_id))
    created = updated = 0
    for item in fields.get("rows", []):
        term, aliases, definition = clean_term(item["term"], item.get("aliases", []), item.get("definition", ""))
        current = taken.get(term.lower())
        if current is None:
            if created >= capacity:
                raise ApiError(422, "tenant_glossary.too_many", f"租户术语最多 {MAX_TENANT_TERMS} 条")
            row = TenantTerm(tenant_id=tenant_id, from_id=None)
            _fill(row, term, aliases, definition, actor)
            row.row_version = 1
            db.add(row)
            taken[term.lower()] = EffectiveTerm(id=str(row.id), term=Term(id=str(row.id), layer="tenant", term=term), label="tenant")
            created += 1
        elif on_duplicate == "overwrite":
            apply_change(db, tenant_id, user_id, "update", UUID(current.id), {"term": term, "aliases": aliases, "definition": definition})
            db.flush()
            updated += 1
    _audit(db, tenant_id, user_id, "tenant_term.imported", "导入租户术语", created=str(created), updated=str(updated))
    return "imported"


def object_id_for(tenant_id: UUID, key: UUID | None, operation: str = "update") -> UUID:
    """One pending request per existing term; creations, imports and the settings do not block each other."""
    if operation in ("create", "import"):
        return uuid4()
    return uuid5(tenant_id, str(key) if key else operation)


def precheck(db: Session, tenant_id: UUID, operation: str, key: UUID | None, fields: dict[str, Any] | None) -> None:
    """Validate before queueing so an approver is never asked to approve something that cannot apply."""
    fields = fields or {}
    if operation in ("update", "restore", "delete") and key is not None:
        if _own(db, tenant_id, key) is None and str(key) not in _inherited(db, tenant_id):
            raise ApiError(404, "not_found", "未找到术语")
    if operation in ("create", "update"):
        term, _aliases, _definition = clean_term(fields.get("term", ""), fields.get("aliases", []), fields.get("definition", ""))
        _ensure_unique(db, tenant_id, term, exclude_key=str(key) if operation == "update" and key else None)


def diff_for(db: Session, tenant_id: UUID, operation: str, key: UUID | None, fields: dict[str, Any] | None) -> list[dict[str, Any]]:
    fields = fields or {}
    if operation == "settings":
        return [{"section": "tenant_glossary.settings", "field": "glossary_ids", "from": [str(g.id) for g in selected_glossaries(db, tenant_id)], "to": fields.get("glossary_ids")}]
    if operation == "restore_hidden":
        return [{"section": "tenant_glossary.term", "field": "hidden", "from": len(tenant_view(db, tenant_id).hidden), "to": 0}]
    if operation == "import":
        return [{"section": "tenant_glossary.term", "field": "import", "from": None, "to": f"{len(fields.get('rows', []))} 条（{fields.get('on_duplicate', 'skip')}）"}]
    before: dict[str, Any] = {}
    if key is not None:
        current = next((i for i in tenant_view(db, tenant_id).terms if i.id == str(key)), None)
        if current is not None:
            before = {"term": current.term.term, "aliases": list(current.term.aliases), "definition": current.term.definition}
    if operation in ("delete", "restore"):
        return [{"section": "tenant_glossary.term", "field": "term", "from": before.get("term"), "to": None}]
    after = {"term": fields.get("term", "").strip(), "aliases": fields.get("aliases", []), "definition": fields.get("definition", "").strip()}
    return [{"section": "tenant_glossary.term", "field": name, "from": before.get(name), "to": after[name]} for name in after if before.get(name) != after[name]] or [
        {"section": "tenant_glossary.term", "field": "term", "from": None, "to": after["term"]}
    ]


@approval_action(ACTION)
def apply_tenant_glossary_change(db: Session, context: ApplyContext) -> None:
    payload = context.payload
    key = UUID(payload["key"]) if payload.get("key") else None
    try:
        apply_change(db, context.tenant_id, context.requester_id, payload["operation"], key, payload.get("fields"))
    except ApiError as exc:
        if context.approval_id is None:
            raise
        raise blocked(exc.detail) from exc


# PLT-125 and PLT-133 for terms: deleting a platform term turns tenant overrides into the tenants' own terms and drops
# their hidden markers; removing a glossary drops it from every tenant's inheritance.
def _on_term_deleted(db: Session, term_id: UUID) -> None:
    for row in db.scalars(select(TenantTerm).where(TenantTerm.from_id == term_id)):
        if row.hidden:
            db.delete(row)
        else:
            row.from_id = None


def _on_glossary_deleted(db: Session, glossary_id: UUID) -> None:
    for row in db.scalars(select(TenantGlossarySettings)):
        if str(glossary_id) in (row.glossary_ids or []):
            row.glossary_ids = [item for item in row.glossary_ids if item != str(glossary_id)]
            row.row_version += 1


def register_hooks() -> None:
    from beework import glossary

    if _on_term_deleted not in glossary.PLATFORM_TERM_DELETE_HOOKS:
        glossary.PLATFORM_TERM_DELETE_HOOKS.append(_on_term_deleted)
    if _on_glossary_deleted not in glossary.PLATFORM_GLOSSARY_DELETE_HOOKS:
        glossary.PLATFORM_GLOSSARY_DELETE_HOOKS.append(_on_glossary_deleted)


register_hooks()
