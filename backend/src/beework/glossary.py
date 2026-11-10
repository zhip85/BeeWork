"""Industry dictionary and platform glossaries (T-051)."""

from collections.abc import Callable
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework.errors import ApiError
from beework.glossary_parser import (
    MAX_ALIAS,
    MAX_ALIASES,
    MAX_DEFINITION,
    MAX_TERM,
    ParseResult,
    split_aliases,
)
from beework.models import AuditEvent, Tenant
from beework.models.glossary import Industry, IndustryGlossary, PlatformTerm

DEFAULT_INDUSTRY = "general_office"


def invalid(message: str) -> ApiError:
    return ApiError(422, "glossary.invalid", message)


def active_industry_codes(db: Session) -> set[str]:
    return set(db.scalars(select(Industry.code).where(Industry.status == "active")))


def ensure_industry(db: Session, code: str) -> str:
    """The code of an active dictionary entry (tenants, glossaries and templates all reference the dictionary)."""
    if code != DEFAULT_INDUSTRY and code not in active_industry_codes(db):  # the default entry can never be disabled
        raise invalid("所选行业不存在或已停用")
    return code


def industry_usage(db: Session, code: str) -> tuple[int, int]:
    tenants = db.scalar(select(func.count()).select_from(Tenant).where(Tenant.industry == code)) or 0
    glossaries = db.scalar(select(func.count()).select_from(IndustryGlossary).where(IndustryGlossary.industry_code == code)) or 0
    return tenants, glossaries


# Called with (db, id) when a platform term / glossary is deleted, so tenants' overrides, hidden markers and inherited
# glossary selections can follow (PLT-125, PLT-133). The tenant glossary (T-053) registers here; the assistant layer later.
PLATFORM_TERM_DELETE_HOOKS: list[Callable[[Session, UUID], None]] = []
PLATFORM_GLOSSARY_DELETE_HOOKS: list[Callable[[Session, UUID], None]] = []


def inheriting_tenant_count(db: Session, glossary: IndustryGlossary) -> int:
    """Tenants that inherit the glossary: those that selected it (SET-061)."""
    from beework.models.tenant_glossary import TenantGlossarySettings

    return sum(1 for row in db.scalars(select(TenantGlossarySettings)) if str(glossary.id) in (row.glossary_ids or []))


def clean_name(value: str, label: str, limit: int = 80) -> str:
    value = (value or "").strip()
    if not value:
        raise invalid(f"请填写{label}")
    if len(value) > limit:
        raise invalid(f"{label}不能超过 {limit} 个字符")
    return value


def ensure_unique_glossary_name(db: Session, name: str, exclude_id: UUID | None = None) -> None:
    for row in db.execute(select(IndustryGlossary.id, IndustryGlossary.name)):
        if row.name.lower() == name.lower() and row.id != exclude_id:
            raise ApiError(409, "glossary.duplicate_name", "术语库名称已被使用")


def clean_term(term: str, aliases: list[str], definition: str) -> tuple[str, list[str], str]:
    term = clean_name(term, "标准术语", MAX_TERM)
    cleaned = split_aliases(";".join(item.replace(";", " ").replace("；", " ") for item in aliases), term)
    if len(cleaned) > MAX_ALIASES:
        raise invalid(f"别名最多 {MAX_ALIASES} 个")
    if any(len(alias) > MAX_ALIAS for alias in cleaned):
        raise invalid(f"每个别名不能超过 {MAX_ALIAS} 个字符")
    definition = (definition or "").strip()
    if len(definition) > MAX_DEFINITION:
        raise invalid(f"定义不能超过 {MAX_DEFINITION} 个字符")
    return term, cleaned, definition


def find_term(db: Session, glossary_id: UUID, term: str) -> PlatformTerm | None:
    return db.scalar(select(PlatformTerm).where(PlatformTerm.glossary_id == glossary_id, PlatformTerm.term_key == term.lower()))


def import_terms(
    db: Session, glossary: IndustryGlossary, parsed: ParseResult, *, on_duplicate: str, dry_run: bool, actor: str
) -> dict:
    """Create the parsed terms; existing ones are skipped or overwritten as asked. ``dry_run`` only counts."""
    existing = {t.term_key: t for t in db.scalars(select(PlatformTerm).where(PlatformTerm.glossary_id == glossary.id))}
    created = updated = skipped = 0
    preview = []
    for item in parsed.terms:
        current = existing.get(item.term.lower())
        if len(preview) < 5:
            preview.append({"line": item.line, "term": item.term, "aliases": item.aliases, "definition": item.definition, "exists": current is not None})
        if current is None:
            created += 1
            if not dry_run:
                db.add(PlatformTerm(glossary_id=glossary.id, term=item.term, term_key=item.term.lower(), aliases=item.aliases, definition=item.definition, updated_by=actor))
        elif on_duplicate == "overwrite":
            updated += 1
            if not dry_run:
                current.aliases, current.definition = item.aliases, item.definition
                current.row_version += 1
                current.updated_by = actor
        else:
            skipped += 1
    duplicates = sum(1 for item in parsed.terms if item.term.lower() in existing)
    return {"created": created, "updated": updated, "skipped": skipped, "duplicates": duplicates, "preview": preview}


def audit(db: Session, user_id: UUID | None, action: str, **details: str) -> None:
    db.add(AuditEvent(tenant_id=None, actor_user_id=user_id, action=action, details=details))
