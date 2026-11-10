"""Platform industry dictionary, industry glossaries and their terms (T-051, PRD 3.16.1)."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from beework import glossary as service
from beework.api.conventions import ApiError, check_row_version, require_platform_admin
from beework.auth import require_csrf
from beework.db import get_db
from beework.glossary_parser import parse_terms
from beework.models import User
from beework.models.glossary import Industry, IndustryGlossary, PlatformTerm
from beework.schemas.glossary import (
    GlossaryResponse,
    GlossaryWrite,
    ImportPreviewRow,
    ImportRowError,
    IndustryResponse,
    IndustryWrite,
    TermImportResponse,
    TermListResponse,
    TermResponse,
    TermWrite,
)

router = APIRouter()

MAX_IMPORT_BYTES = 1_000_000


# ---------------------------------------------------------------------------
# Industry dictionary.
# ---------------------------------------------------------------------------


def _industry_response(db: Session, row: Industry) -> IndustryResponse:
    tenants, glossaries = service.industry_usage(db, row.code)
    return IndustryResponse(
        code=row.code, name_zh=row.name_zh, name_en=row.name_en, sort_order=row.sort_order, status=row.status,
        tenant_count=tenants, glossary_count=glossaries,
    )


@router.get("/platform/industries", response_model=list[IndustryResponse])
def list_industries(_: User = Depends(require_platform_admin), db: Session = Depends(get_db)) -> list[IndustryResponse]:
    rows = db.scalars(select(Industry).order_by(Industry.sort_order, Industry.code))
    return [_industry_response(db, row) for row in rows]


@router.post("/platform/industries", response_model=IndustryResponse, status_code=status.HTTP_201_CREATED)
def create_industry(
    payload: IndustryWrite,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> IndustryResponse:
    if not payload.code:
        raise service.invalid("请填写行业代码（小写字母、数字、下划线，字母开头）")
    if db.get(Industry, payload.code) is not None:
        raise ApiError(409, "industry.duplicate", "行业代码已存在")
    row = Industry(code=payload.code, name_zh=payload.name_zh.strip(), name_en=payload.name_en.strip(), sort_order=payload.sort_order, status="active")
    db.add(row)
    service.audit(db, user.id, "industry.created", name=f"{row.name_zh}（{row.code}）")
    db.commit()
    return _industry_response(db, row)


@router.put("/platform/industries/{code}", response_model=IndustryResponse)
def update_industry(
    code: str,
    payload: IndustryWrite,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> IndustryResponse:
    row = db.get(Industry, code)
    if row is None:
        raise ApiError(404, "not_found", "未找到行业")
    if payload.status == "disabled" and code == service.DEFAULT_INDUSTRY:
        raise ApiError(409, "industry.protected", "默认行业“通用办公”不能停用")
    row.name_zh, row.name_en = payload.name_zh.strip(), payload.name_en.strip()
    row.sort_order, row.status = payload.sort_order, payload.status
    service.audit(db, user.id, "industry.updated", name=f"{row.name_zh}（{row.code}）", status=row.status)
    db.commit()
    return _industry_response(db, row)


@router.delete("/platform/industries/{code}", status_code=status.HTTP_204_NO_CONTENT)
def delete_industry(
    code: str,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    row = db.get(Industry, code)
    if row is None:
        raise ApiError(404, "not_found", "未找到行业")
    tenants, glossaries = service.industry_usage(db, code)
    if code == service.DEFAULT_INDUSTRY or tenants or glossaries:
        raise ApiError(409, "industry.in_use", f"该行业仍被 {tenants} 个租户、{glossaries} 个术语库引用，不能删除（可以停用）")
    service.audit(db, user.id, "industry.deleted", name=f"{row.name_zh}（{row.code}）")
    db.delete(row)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Glossaries.
# ---------------------------------------------------------------------------


def _glossary_response(db: Session, glossary: IndustryGlossary) -> GlossaryResponse:
    industry = db.get(Industry, glossary.industry_code)
    return GlossaryResponse(
        id=glossary.id, name=glossary.name, description=glossary.description, industry_code=glossary.industry_code,
        industry_name=industry.name_zh if industry else glossary.industry_code,
        term_count=db.scalar(select(func.count()).select_from(PlatformTerm).where(PlatformTerm.glossary_id == glossary.id)) or 0,
        tenant_count=service.inheriting_tenant_count(db, glossary), row_version=glossary.row_version, updated_at=glossary.updated_at,
    )


def _load_glossary(db: Session, glossary_id: UUID, *, lock: bool = False) -> IndustryGlossary:
    statement = select(IndustryGlossary).where(IndustryGlossary.id == glossary_id)
    glossary = db.scalar(statement.with_for_update() if lock else statement)
    if glossary is None:
        raise ApiError(404, "not_found", "未找到行业术语库")
    return glossary


@router.get("/platform/glossaries", response_model=list[GlossaryResponse])
def list_glossaries(_: User = Depends(require_platform_admin), db: Session = Depends(get_db)) -> list[GlossaryResponse]:
    rows = db.scalars(select(IndustryGlossary).order_by(IndustryGlossary.sort_order, IndustryGlossary.created_at))
    return [_glossary_response(db, row) for row in rows]


@router.post("/platform/glossaries", response_model=GlossaryResponse, status_code=status.HTTP_201_CREATED)
def create_glossary(
    payload: GlossaryWrite,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> GlossaryResponse:
    name = service.clean_name(payload.name, "术语库名称")
    service.ensure_unique_glossary_name(db, name)
    industry = service.ensure_industry(db, payload.industry_code)
    last = db.scalar(select(IndustryGlossary.sort_order).order_by(IndustryGlossary.sort_order.desc()).limit(1)) or 0
    glossary = IndustryGlossary(name=name, description=payload.description.strip(), industry_code=industry, sort_order=last + 1, updated_by=user.display_name)
    db.add(glossary)
    db.flush()
    service.audit(db, user.id, "glossary.created", name=name)
    db.commit()
    return _glossary_response(db, glossary)


@router.put("/platform/glossaries/{glossary_id}", response_model=GlossaryResponse)
def update_glossary(
    glossary_id: UUID,
    payload: GlossaryWrite,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> GlossaryResponse:
    if payload.row_version is None:
        raise ApiError(422, "validation.invalid", "缺少 row_version")
    glossary = _load_glossary(db, glossary_id, lock=True)
    check_row_version(glossary, payload.row_version, db)
    name = service.clean_name(payload.name, "术语库名称")
    service.ensure_unique_glossary_name(db, name, exclude_id=glossary.id)
    if payload.industry_code != glossary.industry_code:
        service.ensure_industry(db, payload.industry_code)
    glossary.name, glossary.description, glossary.industry_code = name, payload.description.strip(), payload.industry_code
    glossary.row_version += 1
    glossary.updated_by = user.display_name
    service.audit(db, user.id, "glossary.updated", name=name)
    db.commit()
    return _glossary_response(db, glossary)


@router.delete("/platform/glossaries/{glossary_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_glossary(
    glossary_id: UUID,
    confirm: bool = Query(default=False),
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    """Deleting a glossary that tenants inherit needs ``confirm=true`` (PLT-125); the 409 names how many tenants."""
    glossary = _load_glossary(db, glossary_id, lock=True)
    tenants = service.inheriting_tenant_count(db, glossary)
    if tenants and not confirm:
        raise ApiError(409, "glossary.in_use", f"{tenants} 个租户正在继承，删除后这些租户将失去继承的术语")
    service.audit(db, user.id, "glossary.deleted", name=glossary.name, tenants=str(tenants))
    for term_id in db.scalars(select(PlatformTerm.id).where(PlatformTerm.glossary_id == glossary.id)):
        for hook in service.PLATFORM_TERM_DELETE_HOOKS:
            hook(db, term_id)
    for hook in service.PLATFORM_GLOSSARY_DELETE_HOOKS:
        hook(db, glossary.id)
    db.execute(delete(PlatformTerm).where(PlatformTerm.glossary_id == glossary.id))
    db.delete(glossary)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Terms.
# ---------------------------------------------------------------------------


def _term_response(term: PlatformTerm) -> TermResponse:
    return TermResponse(
        id=term.id, glossary_id=term.glossary_id, term=term.term, aliases=list(term.aliases or []), definition=term.definition,
        row_version=term.row_version, updated_by=term.updated_by, updated_at=term.updated_at,
    )


@router.get("/platform/glossaries/{glossary_id}/terms", response_model=TermListResponse)
def list_terms(
    glossary_id: UUID,
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> TermListResponse:
    _load_glossary(db, glossary_id)
    statement = select(PlatformTerm).where(PlatformTerm.glossary_id == glossary_id)
    if q and q.strip():
        needle = q.strip().lower()
        # Aliases live in a JSON column, so matching them is done on the loaded rows (glossaries are small).
        rows = [
            term for term in db.scalars(statement.order_by(PlatformTerm.term_key))
            if needle in term.term_key or any(needle in alias.lower() for alias in term.aliases or [])
        ]
        total = len(rows)
        rows = rows[offset: offset + limit]
    else:
        total = db.scalar(select(func.count()).select_from(PlatformTerm).where(PlatformTerm.glossary_id == glossary_id)) or 0
        rows = list(db.scalars(statement.order_by(PlatformTerm.term_key).limit(limit).offset(offset)))
    return TermListResponse(items=[_term_response(term) for term in rows], total=total, limit=limit, offset=offset)


@router.post("/platform/glossaries/{glossary_id}/terms", response_model=TermResponse, status_code=status.HTTP_201_CREATED)
def create_term(
    glossary_id: UUID,
    payload: TermWrite,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> TermResponse:
    glossary = _load_glossary(db, glossary_id, lock=True)
    term, aliases, definition = service.clean_term(payload.term, payload.aliases, payload.definition)
    if service.find_term(db, glossary.id, term) is not None:
        raise ApiError(409, "glossary.duplicate_term", "该术语库中已有同名标准术语")
    row = PlatformTerm(glossary_id=glossary.id, term=term, term_key=term.lower(), aliases=aliases, definition=definition, updated_by=user.display_name)
    db.add(row)
    db.flush()
    service.audit(db, user.id, "glossary_term.created", name=term, glossary=glossary.name)
    db.commit()
    return _term_response(row)


def _load_term(db: Session, glossary_id: UUID, term_id: UUID) -> PlatformTerm:
    term = db.scalar(select(PlatformTerm).where(PlatformTerm.id == term_id, PlatformTerm.glossary_id == glossary_id).with_for_update())
    if term is None:
        raise ApiError(404, "not_found", "未找到术语")
    return term


@router.put("/platform/glossaries/{glossary_id}/terms/{term_id}", response_model=TermResponse)
def update_term(
    glossary_id: UUID,
    term_id: UUID,
    payload: TermWrite,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> TermResponse:
    if payload.row_version is None:
        raise ApiError(422, "validation.invalid", "缺少 row_version")
    glossary = _load_glossary(db, glossary_id)
    row = _load_term(db, glossary_id, term_id)
    check_row_version(row, payload.row_version, db)
    term, aliases, definition = service.clean_term(payload.term, payload.aliases, payload.definition)
    other = service.find_term(db, glossary.id, term)
    if other is not None and other.id != row.id:
        raise ApiError(409, "glossary.duplicate_term", "该术语库中已有同名标准术语")
    row.term, row.term_key, row.aliases, row.definition = term, term.lower(), aliases, definition
    row.row_version += 1
    row.updated_by = user.display_name
    service.audit(db, user.id, "glossary_term.updated", name=term, glossary=glossary.name)
    db.commit()
    return _term_response(row)


@router.delete("/platform/glossaries/{glossary_id}/terms/{term_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_term(
    glossary_id: UUID,
    term_id: UUID,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    glossary = _load_glossary(db, glossary_id)
    row = _load_term(db, glossary_id, term_id)
    service.audit(db, user.id, "glossary_term.deleted", name=row.term, glossary=glossary.name)
    for hook in service.PLATFORM_TERM_DELETE_HOOKS:
        hook(db, row.id)
    db.delete(row)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/platform/glossaries/{glossary_id}/terms/import", response_model=TermImportResponse)
async def import_glossary_terms(
    glossary_id: UUID,
    request: Request,
    dry_run: bool = Query(default=False),
    on_duplicate: str = Query(default="skip", pattern="^(skip|overwrite)$"),
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> TermImportResponse:
    """Plain-text body (CSV / TXT, PLT-124). ``dry_run`` reports what would be imported without changing anything."""
    glossary = _load_glossary(db, glossary_id, lock=True)
    data = await request.body()
    if len(data) > MAX_IMPORT_BYTES:
        raise ApiError(413, "glossary.file_too_large", "文件超过 1 MB")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise service.invalid("文件编码无法识别，请使用 UTF-8 保存") from exc
    parsed = parse_terms(text)
    if not parsed.terms and not parsed.errors:
        raise service.invalid("没有识别到术语")
    outcome = service.import_terms(db, glossary, parsed, on_duplicate=on_duplicate, dry_run=dry_run, actor=user.display_name)
    if not dry_run:
        service.audit(db, user.id, "glossary_term.imported", name=glossary.name, created=str(outcome["created"]), updated=str(outcome["updated"]))
        db.commit()
    return TermImportResponse(
        dry_run=dry_run, recognized=len(parsed.terms), duplicates=outcome["duplicates"],
        created=0 if dry_run else outcome["created"], updated=0 if dry_run else outcome["updated"],
        skipped=0 if dry_run else outcome["skipped"], errors=[ImportRowError(**e) for e in parsed.errors],
        preview=[ImportPreviewRow(**row) for row in outcome["preview"]],
    )

