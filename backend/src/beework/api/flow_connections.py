"""Flow connections — the Chatflow / Workflow tabs of the capability centre (T-075, 演进 PRD 8)."""

import re
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework import flow_connection_service as svc
from beework.api.assistants import _actor_name
from beework.api.capabilities import _can
from beework.api.conventions import (
    ApiError,
    check_row_version,
    normalize_name,
    validate_public_https_url,
)
from beework.auth import require_csrf
from beework.capability_catalog import references_to
from beework.db import get_db
from beework.entitlements import ensure_entitlement
from beework.model_secrets import encrypt_model_api_key
from beework.models import AuditEvent, Membership
from beework.models.agents import Agent
from beework.models.flow_connections import FlowConnection
from beework.rbac import require_tenant_permission
from beework.schemas.flow_connections import (
    AvailableFlow,
    FlowConnectionList,
    FlowConnectionReference,
    FlowConnectionReferences,
    FlowConnectionResponse,
    FlowConnectionTestResult,
    FlowConnectionWrite,
    FlowKind,
)

router = APIRouter()

NAME_MAX = 50
DESCRIPTION_MAX = 500
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,39}$")  # a Dify input variable / a tool argument name


def _load(db: Session, membership: Membership, connection_id: UUID) -> FlowConnection:
    conn = db.get(FlowConnection, connection_id)
    if conn is None or conn.deleted_at is not None or conn.tenant_id != membership.tenant_id:
        raise ApiError(404, "not_found", "未找到指定的流程连接")
    return conn


def _audit(db: Session, membership: Membership, action: str, conn: FlowConnection) -> None:
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id, actor_user_id=membership.user_id, action=action,
            details={"connection_id": str(conn.id), "kind": conn.kind, "name": conn.name, "source_type": conn.source_type},
        )
    )


def _references(db: Session, conn: FlowConnection):
    return references_to(db, conn.tenant_id, svc.REFERENCE_TARGET[conn.kind], conn.id)


def _response(db: Session, membership: Membership, conn: FlowConnection) -> FlowConnectionResponse:
    state, reason = svc.state_of(db, conn)
    flow = db.get(Agent, conn.flow_id) if conn.flow_id else None
    return FlowConnectionResponse(
        id=conn.id, kind=conn.kind, name=conn.name, description=conn.description, source_type=conn.source_type,
        flow_id=conn.flow_id, flow_name=flow.name if flow is not None and flow.deleted_at is None else None,
        base_url=conn.base_url, host=svc.host_of(conn.base_url), app_id=conn.app_id, credential_mask=svc.mask_of(conn),
        input_mapping=conn.input_mapping or {}, output_mapping=conn.output_mapping or {}, streaming=conn.streaming,
        status=conn.status, state=state, state_reason=reason, last_test_at=conn.last_test_at, last_test_ok=conn.last_test_ok,
        last_test_error=conn.last_test_error, last_test_ms=conn.last_test_ms, references=len(_references(db, conn)),
        row_version=conn.row_version, created_by=conn.created_by, updated_by=conn.updated_by, created_at=conn.created_at,
        updated_at=conn.updated_at, can_edit=_can(db, membership, "capabilities:edit"),
        can_delete=_can(db, membership, "capabilities:delete"),
    )


def _unique_name(db: Session, tenant_id: UUID, kind: str, value: str, exclude_id: UUID | None = None) -> str:
    name = normalize_name(value, NAME_MAX)
    statement = select(FlowConnection.id).where(
        FlowConnection.tenant_id == tenant_id, FlowConnection.kind == kind, FlowConnection.deleted_at.is_(None),
        func.lower(FlowConnection.name) == name.lower(),
    )
    if exclude_id is not None:
        statement = statement.where(FlowConnection.id != exclude_id)
    if db.scalar(statement.limit(1)) is not None:
        raise ApiError(409, "flow_connection.name_taken", "该名称已被同类型的其他连接使用")
    return name


def _mapping(raw: dict[str, str], label: str, keys_are_identifiers: bool) -> dict[str, str]:
    clean: dict[str, str] = {}
    for key, value in raw.items():
        key, value = key.strip(), value.strip()
        if not key or not value or (keys_are_identifiers and not IDENTIFIER.match(key)) or (label == "输入映射" and not IDENTIFIER.match(value)):
            raise ApiError(422, "validation.invalid", f"{label}中的名称只能包含字母、数字和下划线，且不能为空")
        clean[key] = value
    return clean


def _apply(db: Session, membership: Membership, conn: FlowConnection, payload: FlowConnectionWrite, creating: bool) -> None:
    """Validate the source-specific fields and copy them onto ``conn``; the other source's fields are cleared."""
    if payload.source_type == "native":
        flows = {f.id for f in svc.available_flows(db, membership.tenant_id, conn.kind)}
        if payload.flow_id is None:
            raise ApiError(422, "validation.invalid", "请选择一个已发布的流程")
        if payload.flow_id not in flows:
            raise ApiError(422, "flow_connection.flow_unavailable", "只能选择类型匹配且已发布的流程")
        conn.flow_id = payload.flow_id
        conn.base_url, conn.app_id, conn.credential_encrypted, conn.credential_last4 = "", "", None, None
        conn.input_mapping, conn.output_mapping, conn.streaming = {}, {}, False
        conn.source_type = "native"
        return
    ensure_entitlement(db, membership.tenant_id, "external_flows")
    base_url, app_id = payload.base_url.strip(), payload.app_id.strip()
    secret = (payload.credential or "").strip()
    missing = [label for label, value in (("Base URL", base_url), ("App ID", app_id)) if not value]
    if not secret and not conn.credential_encrypted:
        missing.append("API Key")
    if missing:
        raise ApiError(422, "validation.invalid", f"请填写：{'、'.join(missing)}")
    conn.base_url = _checked_url(base_url)
    conn.app_id = app_id
    if secret:
        conn.credential_encrypted = encrypt_model_api_key(secret)
        conn.credential_last4 = secret[-4:]
    conn.input_mapping = _mapping(payload.input_mapping, "输入映射", keys_are_identifiers=True)
    conn.output_mapping = _mapping(payload.output_mapping, "输出映射", keys_are_identifiers=False)
    conn.streaming = payload.streaming
    conn.source_type, conn.flow_id = "dify", None


def _checked_url(value: str) -> str:
    validate_public_https_url(value)  # NFR-009: a public https address only; a private one is a 400
    return value.rstrip("/")


# ---------------------------------------------------------------------------
# Endpoints.
# ---------------------------------------------------------------------------


@router.get("/flow-connections", response_model=FlowConnectionList)
def list_connections(
    kind: FlowKind | None = None,
    q: str | None = Query(default=None, max_length=100),
    membership: Membership = Depends(require_tenant_permission("capabilities:view")),
    db: Session = Depends(get_db),
) -> FlowConnectionList:
    statement = select(FlowConnection).where(FlowConnection.tenant_id == membership.tenant_id, FlowConnection.deleted_at.is_(None))
    if kind:
        statement = statement.where(FlowConnection.kind == kind)
    if q and q.strip():
        statement = statement.where(func.lower(FlowConnection.name).like(f"%{q.strip().lower()}%"))
    rows = list(db.scalars(statement.order_by(func.lower(FlowConnection.name), FlowConnection.id)))
    return FlowConnectionList(items=[_response(db, membership, c) for c in rows], total=len(rows))


@router.get("/flow-connections/available-flows", response_model=list[AvailableFlow])
def available_flows(
    kind: FlowKind,
    membership: Membership = Depends(require_tenant_permission("capabilities:edit")),
    db: Session = Depends(get_db),
) -> list[AvailableFlow]:
    """What a native connection can point at: published flows of the matching type. Empty list = the empty state."""
    return [
        AvailableFlow(id=f.id, name=f.name, kind=f.kind, version=(f.published_config or {}).get("version"))
        for f in svc.available_flows(db, membership.tenant_id, kind)
    ]


@router.post("/flow-connections", response_model=FlowConnectionResponse, status_code=status.HTTP_201_CREATED)
def create_connection(
    payload: FlowConnectionWrite,
    membership: Membership = Depends(require_tenant_permission("capabilities:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> FlowConnectionResponse:
    if payload.kind is None:
        raise ApiError(422, "validation.invalid", "请选择连接类型（Chatflow / Workflow）")
    if len(payload.description.strip()) > DESCRIPTION_MAX:
        raise ApiError(400, "validation.invalid_description", f"说明不能超过 {DESCRIPTION_MAX} 字")
    actor = _actor_name(db, membership)
    conn = FlowConnection(
        tenant_id=membership.tenant_id, kind=payload.kind, status="enabled", created_by=actor, updated_by=actor,
        name=_unique_name(db, membership.tenant_id, payload.kind, payload.name), description=payload.description.strip(),
    )
    _apply(db, membership, conn, payload, creating=True)
    db.add(conn)
    db.flush()
    _audit(db, membership, "flow_connection.created", conn)
    db.commit()
    return _response(db, membership, conn)


@router.get("/flow-connections/{connection_id}", response_model=FlowConnectionResponse)
def get_connection(
    connection_id: UUID,
    membership: Membership = Depends(require_tenant_permission("capabilities:view")),
    db: Session = Depends(get_db),
) -> FlowConnectionResponse:
    return _response(db, membership, _load(db, membership, connection_id))


@router.put("/flow-connections/{connection_id}", response_model=FlowConnectionResponse)
def update_connection(
    connection_id: UUID,
    payload: FlowConnectionWrite,
    membership: Membership = Depends(require_tenant_permission("capabilities:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> FlowConnectionResponse:
    conn = _load(db, membership, connection_id)
    if payload.row_version is None:
        raise ApiError(422, "validation.invalid", "缺少 row_version")
    check_row_version(conn, payload.row_version, db)
    if payload.kind is not None and payload.kind != conn.kind:
        raise ApiError(422, "flow_connection.kind_immutable", "连接类型创建后不能修改")
    if len(payload.description.strip()) > DESCRIPTION_MAX:
        raise ApiError(400, "validation.invalid_description", f"说明不能超过 {DESCRIPTION_MAX} 字")
    conn.name = _unique_name(db, conn.tenant_id, conn.kind, payload.name, exclude_id=conn.id)
    conn.description = payload.description.strip()
    _apply(db, membership, conn, payload, creating=False)
    conn.row_version += 1
    conn.updated_by = _actor_name(db, membership)
    _audit(db, membership, "flow_connection.updated", conn)
    db.commit()
    return _response(db, membership, conn)


@router.post("/flow-connections/{connection_id}/test", response_model=FlowConnectionTestResult)
def test_connection(
    connection_id: UUID,
    membership: Membership = Depends(require_tenant_permission("capabilities:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> FlowConnectionTestResult:
    conn = _load(db, membership, connection_id)
    ok, error, category, ms = svc.run_test(db, conn)
    _audit(db, membership, "flow_connection.tested", conn)
    db.commit()
    return FlowConnectionTestResult(ok=ok, error=error, error_category=category, duration_ms=ms, connection=_response(db, membership, conn))


def _set_status(db: Session, membership: Membership, connection_id: UUID, new: str) -> FlowConnectionResponse:
    conn = _load(db, membership, connection_id)
    if conn.status != new:
        conn.status = new
        conn.row_version += 1
        conn.updated_by = _actor_name(db, membership)
        _audit(db, membership, f"flow_connection.{'enabled' if new == 'enabled' else 'disabled'}", conn)
        db.commit()
    return _response(db, membership, conn)


@router.post("/flow-connections/{connection_id}/enable", response_model=FlowConnectionResponse)
def enable_connection(
    connection_id: UUID,
    membership: Membership = Depends(require_tenant_permission("capabilities:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> FlowConnectionResponse:
    return _set_status(db, membership, connection_id, "enabled")


@router.post("/flow-connections/{connection_id}/disable", response_model=FlowConnectionResponse)
def disable_connection(
    connection_id: UUID,
    membership: Membership = Depends(require_tenant_permission("capabilities:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> FlowConnectionResponse:
    return _set_status(db, membership, connection_id, "disabled")


@router.get("/flow-connections/{connection_id}/references", response_model=FlowConnectionReferences)
def connection_references(
    connection_id: UUID,
    membership: Membership = Depends(require_tenant_permission("capabilities:view")),
    db: Session = Depends(get_db),
) -> FlowConnectionReferences:
    conn = _load(db, membership, connection_id)
    items = [
        FlowConnectionReference(type=r.type, id=r.id, name=r.name, status=r.status, layers=sorted(r.layers))
        for r in _references(db, conn)
    ]
    return FlowConnectionReferences(total=len(items), items=items)


@router.delete("/flow-connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connection(
    connection_id: UUID,
    membership: Membership = Depends(require_tenant_permission("capabilities:delete")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    """Allowed with references (the confirmation shows how many); whoever referenced it then sees a dangling reference
    in its own configuration check — nothing is silently ignored (演进 PRD 15.2)."""
    from datetime import UTC, datetime

    conn = _load(db, membership, connection_id)
    _audit(db, membership, "flow_connection.deleted", conn)
    conn.deleted_at = datetime.now(UTC)
    conn.credential_encrypted, conn.credential_last4 = None, None  # nothing to recover a key from
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
