"""Tenant glossary (T-053, SET-060–063): inherit industry glossaries, override / hide / add terms, import."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.api.conventions import ApiError, check_row_version
from beework.approvals import ensure_policies, pending_request, submit_change
from beework.auth import require_csrf
from beework.db import get_db
from beework.glossary import clean_term
from beework.glossary_parser import parse_terms
from beework.governance import tenant_glossary as service
from beework.governance.term_inheritance import EffectiveTerm
from beework.models import Membership, Tenant
from beework.models.glossary import Industry, IndustryGlossary
from beework.rbac import require_tenant_permission
from beework.schemas.glossary import ImportPreviewRow, ImportRowError
from beework.schemas.tenant_glossary import (
    GlossaryTag,
    TenantGlossaryChain,
    TenantGlossaryMutation,
    TenantGlossarySettingsWrite,
    TenantGlossaryState,
    TenantImportResponse,
    TenantTermResponse,
    TenantTermWrite,
)
from beework.schemas.versions import ApprovalInfo

router = APIRouter()
MAX_IMPORT_BYTES = 1_000_000
MAX_IMPORT_ROWS = 500  # the tenant import is applied row by row inside one request


class _Absent:
    """Stand-in for a tenant that never saved its inheritance: version 0 (``check_row_version`` only reads it)."""

    row_version = 0


def _item(db: Session, tenant_id: UUID, effective: EffectiveTerm) -> TenantTermResponse:
    key = UUID(effective.id)
    pending = pending_request(db, tenant_id, service.OBJECT_TYPE, service.object_id_for(tenant_id, key))
    return TenantTermResponse(
        key=key, term=effective.term.term, aliases=list(effective.term.aliases), definition=effective.term.definition,
        label=effective.label if effective.label in ("industry_inherited", "tenant", "tenant_modified") else "tenant",
        restorable=effective.override_id is not None, pending_approval_id=pending.id if pending else None,
    )


def _state(db: Session, membership: Membership, scope: str, q: str | None) -> TenantGlossaryState:
    tenant_id = membership.tenant_id
    tenant = db.get(Tenant, tenant_id)
    settings_row = service.get_settings(db, tenant_id)
    selected = [str(item) for item in (settings_row.glossary_ids if settings_row else [])]
    tags = []
    from sqlalchemy import func

    from beework.models.glossary import PlatformTerm

    for glossary in db.scalars(select(IndustryGlossary).order_by(IndustryGlossary.sort_order, IndustryGlossary.created_at)):
        industry = db.get(Industry, glossary.industry_code)
        count = db.scalar(select(func.count()).select_from(PlatformTerm).where(PlatformTerm.glossary_id == glossary.id)) or 0
        gid = str(glossary.id)
        tags.append(GlossaryTag(id=glossary.id, name=glossary.name, industry_name=industry.name_zh if industry else glossary.industry_code, term_count=count, selected=gid in selected, position=selected.index(gid) + 1 if gid in selected else None))
    items = service.tenant_view(db, tenant_id).terms
    if scope == "tenant":
        items = [item for item in items if item.label in ("tenant", "tenant_modified")]
    if q and q.strip():
        needle = q.strip().lower()
        items = [item for item in items if needle in item.term.key or any(needle in alias.lower() for alias in item.term.aliases)]
    items = sorted(items, key=lambda item: item.term.key)
    policy = ensure_policies(db, tenant_id)[service.EVENT]
    return TenantGlossaryState(
        tenant_name=tenant.name if tenant else "", chain=TenantGlossaryChain(**service.summary(db, tenant_id)), glossaries=tags,
        settings_version=settings_row.row_version if settings_row else 0, items=[_item(db, tenant_id, item) for item in items],
        approval=ApprovalInfo(required=policy.enabled, levels=policy.levels, approver_role=policy.approver_role, pending_approval_id=None),
    )


def _submit(
    db: Session, membership: Membership, response: Response, *, operation: str, key: UUID | None, fields: dict[str, Any] | None,
    title: str, kind: str | None = None,
) -> TenantGlossaryMutation:
    """Apply now when the “智能体规则变更” policy is off, otherwise open an approval request (as for the PII rules)."""
    service.precheck(db, membership.tenant_id, operation, key, fields)
    result = submit_change(
        db, tenant_id=membership.tenant_id, event=service.EVENT, action=service.ACTION, object_type=service.OBJECT_TYPE,
        object_id=service.object_id_for(membership.tenant_id, key, operation), title=title[:200],
        payload={"operation": operation, "key": str(key) if key else None, "fields": fields},
        change_diff=service.diff_for(db, membership.tenant_id, operation, key, fields),
        checks=[{"label": "只影响本租户的术语表", "level": "ok"}], requester_id=membership.user_id,
    )
    db.commit()
    kind = kind or {"create": "created", "update": "updated", "restore": "restored", "delete": "deleted", "settings": "settings", "restore_hidden": "restored_hidden", "import": "imported"}[operation]
    if result.approval is not None:
        response.status_code = status.HTTP_202_ACCEPTED
        return TenantGlossaryMutation(outcome="pending", kind=kind, approval_id=result.approval.id)
    return TenantGlossaryMutation(outcome="applied", kind=kind)


@router.get("/tenant/glossary", response_model=TenantGlossaryState)
def get_tenant_glossary(
    scope: str = Query(default="all", pattern="^(all|tenant)$"),
    q: str | None = Query(default=None, max_length=100),
    membership: Membership = Depends(require_tenant_permission("settings:view")),
    db: Session = Depends(get_db),
) -> TenantGlossaryState:
    state = _state(db, membership, scope, q)
    db.commit()  # persists lazily created default approval policies
    return state


@router.put("/tenant/glossary/settings", response_model=TenantGlossaryMutation)
def update_inherited_glossaries(
    payload: TenantGlossarySettingsWrite,
    response: Response,
    membership: Membership = Depends(require_tenant_permission("settings:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> TenantGlossaryMutation:
    row = service.get_settings(db, membership.tenant_id)
    check_row_version(row if row is not None else _Absent(), payload.row_version, db)
    return _submit(db, membership, response, operation="settings", key=None, fields={"glossary_ids": [str(i) for i in payload.glossary_ids]}, title="租户术语表 · 继承的行业术语库")


@router.post("/tenant/glossary/terms", response_model=TenantGlossaryMutation, status_code=status.HTTP_201_CREATED)
def create_tenant_term(
    payload: TenantTermWrite,
    response: Response,
    membership: Membership = Depends(require_tenant_permission("settings:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> TenantGlossaryMutation:
    return _submit(db, membership, response, operation="create", key=None, fields=payload.model_dump(), title=f"租户术语变更 · 新增 {payload.term.strip()}")


@router.put("/tenant/glossary/terms/{key}", response_model=TenantGlossaryMutation)
def update_tenant_term(
    key: UUID,
    payload: TenantTermWrite,
    response: Response,
    membership: Membership = Depends(require_tenant_permission("settings:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> TenantGlossaryMutation:
    """Edit an own term, or an inherited one: that creates (or updates) the tenant's override of it."""
    creates_override = service._own(db, membership.tenant_id, key) is None and (service._marker(db, membership.tenant_id, key) is None or service._marker(db, membership.tenant_id, key).hidden)
    return _submit(
        db, membership, response, operation="update", key=key, fields=payload.model_dump(), title=f"租户术语变更 · 修改 {payload.term.strip()}",
        kind="override_created" if creates_override else "updated",
    )


@router.post("/tenant/glossary/terms/{key}/restore", response_model=TenantGlossaryMutation)
def restore_term(
    key: UUID,
    response: Response,
    membership: Membership = Depends(require_tenant_permission("settings:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> TenantGlossaryMutation:
    """OC-036: delete the tenant's override so the inherited value applies again."""
    row = service._marker(db, membership.tenant_id, key)
    if row is None or row.hidden:
        raise ApiError(409, "tenant_glossary.nothing_to_restore", "该术语没有租户级修改")
    return _submit(db, membership, response, operation="restore", key=key, fields=None, title=f"租户术语变更 · 恢复 {row.term}")


@router.delete("/tenant/glossary/terms/{key}", response_model=TenantGlossaryMutation)
def delete_tenant_term(
    key: UUID,
    response: Response,
    membership: Membership = Depends(require_tenant_permission("settings:delete")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> TenantGlossaryMutation:
    """An own term is deleted; an inherited one (or its override) is hidden for this tenant (OC-035)."""
    own = service._own(db, membership.tenant_id, key)
    inherited = service._inherited(db, membership.tenant_id).get(str(key))
    if own is None and inherited is None:
        raise ApiError(404, "not_found", "未找到术语")
    name = own.term if own else inherited.term
    return _submit(db, membership, response, operation="delete", key=key, fields=None, title=f"租户术语变更 · 删除 {name}", kind="deleted" if own else "hidden")


@router.post("/tenant/glossary/restore-hidden", response_model=TenantGlossaryMutation)
def restore_hidden_terms(
    response: Response,
    membership: Membership = Depends(require_tenant_permission("settings:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> TenantGlossaryMutation:
    """“全部恢复”: bring back every inherited term this tenant deleted."""
    if not service.tenant_view(db, membership.tenant_id).hidden:
        raise ApiError(409, "tenant_glossary.nothing_to_restore", "没有被删除的继承术语")
    return _submit(db, membership, response, operation="restore_hidden", key=None, fields=None, title="租户术语变更 · 全部恢复被删除的继承术语")


@router.post("/tenant/glossary/terms/import", response_model=TenantImportResponse)
async def import_tenant_terms(
    request: Request,
    response: Response,
    dry_run: bool = Query(default=False),
    on_duplicate: str = Query(default="skip", pattern="^(skip|overwrite)$"),
    membership: Membership = Depends(require_tenant_permission("settings:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> TenantImportResponse:
    """CSV / TXT body, parsed by the same parser as the platform import (SET-063). ``dry_run`` only reports."""
    data = await request.body()
    if len(data) > MAX_IMPORT_BYTES:
        raise ApiError(413, "tenant_glossary.file_too_large", "文件超过 1 MB")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise ApiError(422, "tenant_glossary.invalid", "文件编码无法识别，请使用 UTF-8 保存") from exc
    parsed = parse_terms(text, max_rows=MAX_IMPORT_ROWS)
    if not parsed.terms and not parsed.errors:
        raise ApiError(422, "tenant_glossary.invalid", "没有识别到术语")
    existing = {item.term.key for item in service.tenant_view(db, membership.tenant_id).terms}
    preview = [ImportPreviewRow(line=t.line, term=t.term, aliases=t.aliases, definition=t.definition, exists=t.term.lower() in existing) for t in parsed.terms[:5]]
    duplicates = sum(1 for t in parsed.terms if t.term.lower() in existing)
    base = {"dry_run": dry_run, "recognized": len(parsed.terms), "duplicates": duplicates, "errors": [ImportRowError(**e) for e in parsed.errors], "preview": preview}
    if dry_run or not parsed.terms:
        return TenantImportResponse(**base, outcome="preview")
    for item in parsed.terms:
        clean_term(item.term, list(item.aliases), item.definition)  # the parser already limits these; keeps one source of truth
    rows = [{"term": t.term, "aliases": list(t.aliases), "definition": t.definition} for t in parsed.terms]
    result = _submit(
        db, membership, response, operation="import", key=None, fields={"rows": rows, "on_duplicate": on_duplicate},
        title=f"租户术语变更 · 导入 {len(rows)} 条",
    )
    return TenantImportResponse(**base, outcome=result.outcome, approval_id=result.approval_id)
