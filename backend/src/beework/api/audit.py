import csv
import io
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from beework.api.conventions import require_platform_admin
from beework.audit_maintenance import AUDIT_RETENTION_DAYS, verify_audit_chain
from beework.db import get_db
from beework.models import (
    AUDIT_ACTION_DESCRIPTIONS,
    AuditEvent,
    Membership,
    Tenant,
    User,
)
from beework.rbac import (
    require_tenant_permission,
)
from beework.schemas import (
    AuditEventListResponse,
    AuditEventResponse,
    AuditIntegrityResponse,
)

router = APIRouter()


AUDIT_CATEGORIES = {
    "assistants": "助手",
    "security": "安全",
    "members_permissions": "成员与权限",
    "billing": "计费",
    "developer": "开发者",
}

def _audit_query(
    tenant_id: UUID | None,
    *,
    scope: str,
    category: str | None,
    search: str | None,
    result: str | None,
    start: datetime | None,
    end: datetime | None,
):
    statement = (
        select(AuditEvent, User)
        .outerjoin(User, User.id == AuditEvent.actor_user_id)
        .where(
            AuditEvent.scope == scope,
            AuditEvent.tenant_id == tenant_id
            if tenant_id is not None
            else AuditEvent.tenant_id.is_(None),
            AuditEvent.created_at
            >= datetime.now(UTC) - timedelta(days=AUDIT_RETENTION_DAYS),
        )
    )
    if category:
        statement = statement.where(AuditEvent.category == category)
    if result:
        statement = statement.where(AuditEvent.result == result)
    if start:
        statement = statement.where(AuditEvent.created_at >= start)
    if end:
        statement = statement.where(AuditEvent.created_at <= end)
    if search:
        pattern = f"%{search.strip()}%"
        statement = statement.where(
            or_(
                User.display_name.ilike(pattern),
                User.email.ilike(pattern),
                AuditEvent.action.ilike(pattern),
                AuditEvent.description.ilike(pattern),
                cast(AuditEvent.details, String).ilike(pattern),
            )
        )
    return statement.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())


def _csv_safe_cell(value: str) -> str:
    if value.lstrip()[:1] in {"=", "+", "-", "@", "\t", "\r"}:
        return f"'{value}"
    return value


def _audit_response(
    rows,
    *,
    total: int,
    limit: int,
    offset: int,
) -> AuditEventListResponse:
    return AuditEventListResponse(
        events=[
            AuditEventResponse(
                id=audit.id,
                category=audit.category,
                result=audit.result,
                created_at=audit.created_at,
                actor_name=actor.display_name if actor else "系统",
                actor_email=actor.email if actor else None,
                description=(
                    AUDIT_ACTION_DESCRIPTIONS.get(audit.action, audit.action)
                    if audit.description == audit.action
                    else audit.description
                ),
                source_ip=audit.source_ip,
                details=audit.details,
            )
            for audit, actor in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/audit/events", response_model=AuditEventListResponse)
def list_audit_events(
    category: str | None = Query(default=None, pattern="^(assistants|security|members_permissions|billing|developer)$"),
    search: str | None = Query(default=None, max_length=200),
    result: str | None = Query(default=None, pattern="^(success|warning|failure)$"),
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100000),
    membership: Membership = Depends(require_tenant_permission("audit:view")),
    db: Session = Depends(get_db),
) -> AuditEventListResponse:
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="开始时间不能晚于结束时间")
    statement = _audit_query(
        membership.tenant_id,
        scope="tenant",
        category=category,
        search=search,
        result=result,
        start=start,
        end=end,
    )
    total = db.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.scope == "tenant",
            AuditEvent.tenant_id == membership.tenant_id,
            AuditEvent.created_at
            >= datetime.now(UTC) - timedelta(days=AUDIT_RETENTION_DAYS),
        )
    ) or 0
    if category or search or result or start or end:
        total = db.scalar(
            select(func.count()).select_from(
                statement.with_only_columns(AuditEvent.id).order_by(None).subquery()
            )
        ) or 0
    rows = db.execute(statement.limit(limit).offset(offset)).all()
    return _audit_response(rows, total=total, limit=limit, offset=offset)


@router.get("/audit/events/export")
def export_audit_events(
    category: str | None = Query(default=None, pattern="^(assistants|security|members_permissions|billing|developer)$"),
    search: str | None = Query(default=None, max_length=200),
    result: str | None = Query(default=None, pattern="^(success|warning|failure)$"),
    start: datetime | None = None,
    end: datetime | None = None,
    membership: Membership = Depends(require_tenant_permission("audit:export")),
    db: Session = Depends(get_db),
) -> Response:
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="开始时间不能晚于结束时间")
    statement = _audit_query(
        membership.tenant_id,
        scope="tenant",
        category=category,
        search=search,
        result=result,
        start=start,
        end=end,
    )
    rows = db.execute(statement).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["结果", "时间", "分类", "操作人", "描述", "来源 IP", "变更详情"])
    for audit, actor in rows:
        values = [
            audit.result,
            audit.created_at.isoformat(),
            AUDIT_CATEGORIES.get(audit.category, audit.category),
            actor.display_name if actor else "系统",
            (
                AUDIT_ACTION_DESCRIPTIONS.get(audit.action, audit.action)
                if audit.description == audit.action
                else audit.description
            ),
            audit.source_ip or "",
            json.dumps(audit.details, ensure_ascii=False, sort_keys=True),
        ]
        writer.writerow([_csv_safe_cell(value) for value in values])
    content = output.getvalue()
    output.close()
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            action="audit.exported",
            details={
                "row_count": str(len(rows)),
                "category": category or "",
                "search": search or "",
                "result": result or "",
            },
        )
    )
    db.commit()
    return Response(
        content=content.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="beework-audit.csv"'},
    )


@router.get("/audit/integrity", response_model=AuditIntegrityResponse)
def check_tenant_audit_integrity(
    membership: Membership = Depends(require_tenant_permission("audit:view")),
    db: Session = Depends(get_db),
) -> AuditIntegrityResponse:
    result = verify_audit_chain(db, f"tenant:{membership.tenant_id}")
    return AuditIntegrityResponse(**result)


@router.get("/platform/audit/events", response_model=AuditEventListResponse)
def list_platform_audit_events(
    category: str | None = Query(default=None, pattern="^(assistants|security|members_permissions|billing|developer)$"),
    search: str | None = Query(default=None, max_length=200),
    result: str | None = Query(default=None, pattern="^(success|warning|failure)$"),
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100000),
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> AuditEventListResponse:
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="开始时间不能晚于结束时间")
    statement = _audit_query(
        None,
        scope="platform",
        category=category,
        search=search,
        result=result,
        start=start,
        end=end,
    )
    total = db.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.scope == "platform",
            AuditEvent.tenant_id.is_(None),
            AuditEvent.created_at
            >= datetime.now(UTC) - timedelta(days=AUDIT_RETENTION_DAYS),
        )
    ) or 0
    if category or search or result or start or end:
        total = db.scalar(
            select(func.count()).select_from(
                statement.with_only_columns(AuditEvent.id).order_by(None).subquery()
            )
        ) or 0
    rows = db.execute(statement.limit(limit).offset(offset)).all()
    return _audit_response(rows, total=total, limit=limit, offset=offset)


@router.get("/platform/audit/events/export")
def export_platform_audit_events(
    category: str | None = Query(default=None, pattern="^(assistants|security|members_permissions|billing|developer)$"),
    search: str | None = Query(default=None, max_length=200),
    result: str | None = Query(default=None, pattern="^(success|warning|failure)$"),
    start: datetime | None = None,
    end: datetime | None = None,
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> Response:
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="开始时间不能晚于结束时间")
    statement = _audit_query(
        None,
        scope="platform",
        category=category,
        search=search,
        result=result,
        start=start,
        end=end,
    )
    rows = db.execute(statement).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["结果", "时间", "分类", "操作人", "描述", "来源 IP", "操作详情"])
    for audit, actor in rows:
        writer.writerow(
            [
                _csv_safe_cell(value)
                for value in (
                    audit.result,
                    audit.created_at.isoformat(),
                    AUDIT_CATEGORIES.get(audit.category, audit.category),
                    actor.display_name if actor else "系统",
                    audit.description,
                    audit.source_ip or "",
                    json.dumps(audit.details, ensure_ascii=False, sort_keys=True),
                )
            ]
        )
    content = output.getvalue().encode("utf-8-sig")
    output.close()
    db.add(
        AuditEvent(
            tenant_id=None,
            actor_user_id=user.id,
            action="audit.exported",
            details={"scope": "platform", "row_count": str(len(rows))},
        )
    )
    db.commit()
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="beework-platform-audit.csv"'},
    )


@router.get("/platform/audit/integrity", response_model=AuditIntegrityResponse)
def check_platform_audit_integrity(
    tenant_id: UUID | None = None,
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> AuditIntegrityResponse:
    if tenant_id is None:
        chain_scope = "platform"
    else:
        if db.get(Tenant, tenant_id) is None:
            raise HTTPException(status_code=404, detail="未找到企业工作区")
        chain_scope = f"tenant:{tenant_id}"
    return AuditIntegrityResponse(**verify_audit_chain(db, chain_scope))
