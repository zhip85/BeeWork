"""Capability centre — the list shared by Skill / MCP / API plugin and the MCP connection endpoints (T-072, PRD 3.5)."""

import secrets
from base64 import urlsafe_b64encode
from hashlib import sha256
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework import capability_service as svc
from beework.api.assistants import _actor_name
from beework.api.conventions import ApiError, check_row_version, normalize_name
from beework.approvals import ApplyContext, approval_action, pending_request, submit_change
from beework.auth import get_current_user, require_csrf
from beework.capability_catalog import references_to
from beework.config import settings
from beework.db import get_db
from beework.entitlements import ensure_entitlement
from beework.models import AuditEvent, Membership, TenantRole, User, utc_now
from beework.models.capabilities import Capability, McpConnection
from beework.rbac import flatten_permissions, permissions_for_role, require_tenant_permission
from beework.schemas.capabilities import (
    CapabilityCounts,
    CapabilityKind,
    CapabilityReferences,
    CapabilityResponse,
    McpDetail,
    McpToolEntry,
    McpWriteRequest,
    OAuthStartResponse,
    TestResult,
)
from beework.tools.executor import ToolError

router = APIRouter()

NAME_MAX = 50
DESCRIPTION_MAX = 500
TYPE_LABEL = {"skill": "Skill", "mcp": "MCP", "api": "API"}
FEATURE = {"skill": "skills", "mcp": "mcp", "api": "external_flows"}  # plan entitlement of each type (PLT-083)


def _can(db: Session, membership: Membership, permission: str) -> bool:
    if membership.role == "tenant_admin":
        return True
    if membership.custom_role_id is not None:
        role = db.scalar(
            select(TenantRole).where(
                TenantRole.id == membership.custom_role_id, TenantRole.tenant_id == membership.tenant_id,
                TenantRole.is_active.is_(True),
            )
        )
        return role is not None and permission in flatten_permissions(role.permissions)
    return permission in permissions_for_role(membership.role)


def _load(db: Session, membership: Membership, capability_id: UUID, kind: str | None = None) -> Capability:
    cap = db.get(Capability, capability_id)
    if cap is None or cap.deleted_at is not None or cap.tenant_id != membership.tenant_id or (kind and cap.kind != kind):
        raise ApiError(404, "not_found", "未找到指定能力")
    return cap


def _audit(db: Session, membership: Membership, action: str, cap: Capability, **extra: str) -> None:
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id, actor_user_id=membership.user_id, action=action,
            details={"capability_id": str(cap.id), "kind": cap.kind, "name": cap.name, **extra},
        )
    )


def _validate_name(db: Session, tenant_id: UUID, kind: str, value: str, exclude_id: UUID | None = None) -> str:
    name = normalize_name(value, NAME_MAX)
    statement = select(Capability.id).where(
        Capability.tenant_id == tenant_id, Capability.kind == kind, Capability.deleted_at.is_(None),
        func.lower(Capability.name) == name.lower(),
    )
    if exclude_id is not None:
        statement = statement.where(Capability.id != exclude_id)
    if db.scalar(statement.limit(1)) is not None:
        raise ApiError(409, "capability.name_taken", "该名称已被同类型的其他能力使用")
    return name


def _validate_url(value: str, field: str = "服务地址") -> str:
    url = value.strip()
    parsed = urlsplit(url)
    allowed = ("https", "http") if settings.allow_insecure_tool_urls else ("https",)
    if parsed.scheme not in allowed or not parsed.hostname or parsed.username or parsed.password:
        raise ApiError(422, "capability.invalid_url", f"{field}必须是 https 链接" + ("（开发环境允许 http）" if settings.allow_insecure_tool_urls else ""))
    return url


def _mcp_detail(conn: McpConnection) -> McpDetail:
    ticked = set(conn.selected_tools or [])
    return McpDetail(
        transport=conn.transport, url=conn.url, host=svc.host_of(conn.url), auth_type=conn.auth_type,  # type: ignore[arg-type]
        token_mask=svc.mask(conn.secret_last4), oauth_authorize_url=conn.oauth_authorize_url, oauth_token_url=conn.oauth_token_url,
        oauth_client_id=conn.oauth_client_id, oauth_client_secret_mask=svc.mask(conn.oauth_client_secret_last4),
        oauth_scopes=conn.oauth_scopes, oauth_authorized=bool(conn.oauth_access_encrypted), oauth_expires_at=conn.oauth_expires_at,
        tools=[McpToolEntry(name=t["name"], description=t.get("description", ""), read_only=bool(t.get("read_only")), selected=t["name"] in ticked) for t in conn.tools or []],
        last_test_at=conn.last_test_at, last_test_ok=conn.last_test_ok, last_test_error=conn.last_test_error,
        health=conn.health,  # type: ignore[arg-type]
    )


def _response(db: Session, membership: Membership, cap: Capability) -> CapabilityResponse:
    conn = db.get(McpConnection, cap.id) if cap.kind == "mcp" else None
    pending = pending_request(db, cap.tenant_id, "capability", cap.id)
    base = CapabilityResponse.model_validate(cap)
    return base.model_copy(
        update={
            "meta": f"{svc.host_of(conn.url)} · MCP" if conn else "", "pending_approval": pending is not None,
            "approval_id": pending.id if pending else None, "mcp": _mcp_detail(conn) if conn else None,
            "can_edit": _can(db, membership, "capabilities:edit"), "can_publish": _can(db, membership, "capabilities:publish"),
            "can_delete": _can(db, membership, "capabilities:delete"),
        }
    )


def _request_enable(db: Session, membership: Membership, cap: Capability) -> None:
    """New and re-enabled capabilities go through the “启用第三方 Skill / MCP” approval policy (CAP-053, SET-071): the
    change is applied at once when the policy is off, otherwise the capability stays unavailable until it is approved."""
    if pending_request(db, cap.tenant_id, "capability", cap.id) is not None:
        return
    submit_change(
        db, tenant_id=cap.tenant_id, event="skill", action="capability.enable", object_type="capability", object_id=cap.id,
        title=f"接入 {cap.name}", requester_id=membership.user_id,
        description=f"{TYPE_LABEL[cap.kind]} 能力“{cap.name}”申请启用",
    )


def _try_request_enable(db: Session, membership: Membership, cap: Capability) -> str | None:
    """File the enable request without losing the work done so far: the connection stays a saved draft and the reason
    (for example “no other approver”) is handed back for the page to show."""
    try:
        with db.begin_nested():
            _request_enable(db, membership, cap)
    except ApiError as exc:
        return str(exc.detail)
    return None


@approval_action("capability.enable")
def _apply_enable(db: Session, ctx: ApplyContext) -> None:
    cap = db.get(Capability, ctx.object_id)
    if cap is None or cap.deleted_at is not None or cap.tenant_id != ctx.tenant_id:
        return  # deleted while waiting: nothing left to enable
    if cap.kind == "mcp":
        conn = db.get(McpConnection, cap.id)
        if conn is None or not svc.is_ready(conn):
            raise ApiError(409, "capability.not_ready", "能力尚未测试通过或没有勾选工具，无法启用")
    cap.status = "available"
    cap.row_version += 1
    db.add(
        AuditEvent(
            tenant_id=ctx.tenant_id, actor_user_id=ctx.requester_id, action="capability.enabled",
            details={"capability_id": str(cap.id), "kind": cap.kind, "name": cap.name, "approval": str(ctx.approval_id or "")},
        )
    )


# ---------------------------------------------------------------------------
# List and common actions.
# ---------------------------------------------------------------------------


@router.get("/capabilities/summary", response_model=CapabilityCounts)
def capability_summary(
    membership: Membership = Depends(require_tenant_permission("capabilities:view")),
    db: Session = Depends(get_db),
) -> CapabilityCounts:
    rows = db.execute(
        select(Capability.kind, func.count()).where(Capability.tenant_id == membership.tenant_id, Capability.deleted_at.is_(None)).group_by(Capability.kind)
    ).all()
    counts = dict(rows)
    return CapabilityCounts(skill=counts.get("skill", 0), mcp=counts.get("mcp", 0), api=counts.get("api", 0))


@router.get("/capabilities", response_model=list[CapabilityResponse])
def list_capabilities(
    kind: CapabilityKind = "mcp",
    q: str | None = Query(default=None, max_length=100),
    membership: Membership = Depends(require_tenant_permission("capabilities:view")),
    db: Session = Depends(get_db),
) -> list[CapabilityResponse]:
    statement = select(Capability).where(Capability.tenant_id == membership.tenant_id, Capability.kind == kind, Capability.deleted_at.is_(None))
    if q and q.strip():
        statement = statement.where(func.lower(Capability.name).like(f"%{q.strip().lower()}%"))
    return [_response(db, membership, cap) for cap in db.scalars(statement.order_by(func.lower(Capability.name), Capability.id))]


@router.get("/capabilities/{capability_id}", response_model=CapabilityResponse)
def get_capability(
    capability_id: UUID,
    membership: Membership = Depends(require_tenant_permission("capabilities:view")),
    db: Session = Depends(get_db),
) -> CapabilityResponse:
    return _response(db, membership, _load(db, membership, capability_id))


@router.get("/capabilities/{capability_id}/references", response_model=CapabilityReferences)
def capability_references(
    capability_id: UUID,
    membership: Membership = Depends(require_tenant_permission("capabilities:view")),
    db: Session = Depends(get_db),
) -> CapabilityReferences:
    cap = _load(db, membership, capability_id)
    refs = references_to(db, cap.tenant_id, cap.kind, cap.id)
    return CapabilityReferences(total=len(refs), items=[{"type": r.type, "id": str(r.id), "name": r.name, "status": r.status} for r in refs])


@router.post("/capabilities/{capability_id}/enable", response_model=CapabilityResponse)
def enable_capability(
    capability_id: UUID,
    membership: Membership = Depends(require_tenant_permission("capabilities:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> CapabilityResponse:
    cap = _load(db, membership, capability_id)
    ensure_entitlement(db, cap.tenant_id, FEATURE[cap.kind])
    if cap.status == "available":
        raise ApiError(409, "capability.already_available", "该能力已经是可用状态")
    if cap.kind == "mcp":
        conn = db.get(McpConnection, cap.id)
        if conn is None or not svc.is_ready(conn):
            raise ApiError(409, "capability.not_ready", "请先测试连接成功并至少勾选一个工具")
    _request_enable(db, membership, cap)
    _audit(db, membership, "capability.updated", cap, change="enable_requested")
    db.commit()
    return _response(db, membership, cap)


@router.post("/capabilities/{capability_id}/disable", response_model=CapabilityResponse)
def disable_capability(
    capability_id: UUID,
    membership: Membership = Depends(require_tenant_permission("capabilities:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> CapabilityResponse:
    """Agents can no longer start new calls; versions and run records keep their trace (演进 PRD 7.6)."""
    cap = _load(db, membership, capability_id)
    if cap.status != "available":
        raise ApiError(409, "capability.not_available", "只有可用的能力可以停用")
    references = len(references_to(db, cap.tenant_id, cap.kind, cap.id))
    cap.status = "disabled"
    cap.row_version += 1
    cap.updated_by = _actor_name(db, membership)
    _audit(db, membership, "capability.disabled", cap, references=str(references))
    db.commit()
    return _response(db, membership, cap)


@router.delete("/capabilities/{capability_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_capability(
    capability_id: UUID,
    membership: Membership = Depends(require_tenant_permission("capabilities:delete")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    """Soft delete. Things that mounted it keep the reference and show it as invalid (GLB-083)."""
    cap = _load(db, membership, capability_id)
    references = len(references_to(db, cap.tenant_id, cap.kind, cap.id))
    cap.deleted_at = utc_now()
    cap.updated_by = _actor_name(db, membership)
    _audit(db, membership, "capability.deleted", cap, references=str(references))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/capabilities/{capability_id}/copy", response_model=CapabilityResponse, status_code=status.HTTP_201_CREATED)
def copy_capability(
    capability_id: UUID,
    membership: Membership = Depends(require_tenant_permission("capabilities:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> CapabilityResponse:
    """“复制为草稿”: the copy is a draft with “（副本）” in its name; credentials are carried over, OAuth tokens and the
    test result are not (the copy has to be tested and approved on its own)."""
    source = _load(db, membership, capability_id)
    ensure_entitlement(db, source.tenant_id, FEATURE[source.kind])
    base, name, index = source.name[: NAME_MAX - 6], "", 1
    while True:
        name = f"{base}（副本）" if index == 1 else f"{base}（副本 {index}）"
        if not db.scalar(select(Capability.id).where(Capability.tenant_id == source.tenant_id, Capability.kind == source.kind, Capability.deleted_at.is_(None), func.lower(Capability.name) == name.lower())):
            break
        index += 1
    actor = _actor_name(db, membership)
    copy = Capability(tenant_id=source.tenant_id, kind=source.kind, name=name, description=source.description, status="draft", created_by=actor, updated_by=actor)
    db.add(copy)
    db.flush()
    conn = db.get(McpConnection, source.id)
    if conn is not None:
        db.add(
            McpConnection(
                capability_id=copy.id, tenant_id=copy.tenant_id, transport=conn.transport, url=conn.url, auth_type=conn.auth_type,
                secret_encrypted=conn.secret_encrypted, secret_last4=conn.secret_last4, oauth_authorize_url=conn.oauth_authorize_url,
                oauth_token_url=conn.oauth_token_url, oauth_client_id=conn.oauth_client_id,
                oauth_client_secret_encrypted=conn.oauth_client_secret_encrypted, oauth_client_secret_last4=conn.oauth_client_secret_last4,
                oauth_scopes=conn.oauth_scopes,
            )
        )
    _audit(db, membership, "capability.copied", copy, source_id=str(source.id))
    db.commit()
    return _response(db, membership, copy)


# ---------------------------------------------------------------------------
# MCP.
# ---------------------------------------------------------------------------


def _apply_mcp_fields(conn: McpConnection, payload: McpWriteRequest, *, creating: bool) -> bool:
    """Copy the form onto the connection; returns whether anything that affects connecting changed (the old test result
    and OAuth tokens no longer describe the new configuration then)."""
    url = _validate_url(payload.url) if payload.url.strip() else ""
    if not url:
        raise ApiError(422, "capability.invalid_url", "请填写服务地址")
    changed = creating or url != conn.url or payload.auth_type != conn.auth_type
    conn.transport, conn.url, conn.auth_type = payload.transport, url, payload.auth_type
    if payload.auth_type == "bearer":
        token = (payload.token or "").strip()
        if token:
            conn.secret_encrypted, conn.secret_last4, changed = svc.encrypt(token), token[-4:], True
        elif not conn.secret_encrypted:
            raise ApiError(422, "capability.token_required", "请填写访问令牌")
    else:
        conn.secret_encrypted = conn.secret_last4 = None
    if payload.auth_type == "oauth":
        authorize = _validate_url(payload.oauth_authorize_url, "授权地址")
        token_url = _validate_url(payload.oauth_token_url, "令牌地址")
        client_id = payload.oauth_client_id.strip()
        if not client_id:
            raise ApiError(422, "capability.oauth_client_required", "请填写 OAuth 客户端 ID")
        if (authorize, token_url, client_id, payload.oauth_scopes.strip()) != (conn.oauth_authorize_url, conn.oauth_token_url, conn.oauth_client_id, conn.oauth_scopes):
            changed = True
        conn.oauth_authorize_url, conn.oauth_token_url, conn.oauth_client_id, conn.oauth_scopes = authorize, token_url, client_id, payload.oauth_scopes.strip()
        secret = (payload.oauth_client_secret or "").strip()
        if secret:
            conn.oauth_client_secret_encrypted, conn.oauth_client_secret_last4, changed = svc.encrypt(secret), secret[-4:], True
    else:
        conn.oauth_authorize_url = conn.oauth_token_url = conn.oauth_client_id = conn.oauth_scopes = ""
        conn.oauth_client_secret_encrypted = conn.oauth_client_secret_last4 = None
    if changed:
        conn.oauth_access_encrypted = conn.oauth_refresh_encrypted = None
        conn.oauth_expires_at = None
        conn.last_test_ok, conn.last_test_at, conn.last_test_error = False, None, None
        conn.tools, conn.selected_tools = [], []
        conn.health, conn.health_failures = "unknown", 0
    return changed


@router.post("/capabilities/mcp", response_model=TestResult, status_code=status.HTTP_201_CREATED)
def create_mcp(
    payload: McpWriteRequest,
    membership: Membership = Depends(require_tenant_permission("capabilities:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> TestResult:
    """“连接”: saves a draft, tests the connection and, when it works with at least one tool, asks for the capability
    to be enabled (subject to the approval policy). OAuth connections have to be authorized first."""
    ensure_entitlement(db, membership.tenant_id, "mcp")
    name = _validate_name(db, membership.tenant_id, "mcp", payload.name)
    if len(payload.description.strip()) > DESCRIPTION_MAX:
        raise ApiError(400, "validation.invalid_description", f"描述不能超过 {DESCRIPTION_MAX} 字")
    actor = _actor_name(db, membership)
    cap = Capability(tenant_id=membership.tenant_id, kind="mcp", name=name, description=payload.description.strip(), status="draft", created_by=actor, updated_by=actor)
    db.add(cap)
    db.flush()
    conn = McpConnection(capability_id=cap.id, tenant_id=cap.tenant_id)
    db.add(conn)
    _apply_mcp_fields(conn, payload, creating=True)
    _audit(db, membership, "capability.created", cap)
    ok, error = (False, "请先完成 OAuth 授权后再测试连接") if payload.auth_type == "oauth" else svc.run_test(conn)
    warning = _try_request_enable(db, membership, cap) if ok and svc.is_ready(conn) else None
    db.commit()
    return TestResult(ok=ok, error=error, tool_count=len(conn.tools or []), capability=_response(db, membership, cap).model_copy(update={"approval_warning": warning}))


@router.put("/capabilities/mcp/{capability_id}", response_model=CapabilityResponse)
def update_mcp(
    capability_id: UUID,
    payload: McpWriteRequest,
    membership: Membership = Depends(require_tenant_permission("capabilities:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> CapabilityResponse:
    cap = _load(db, membership, capability_id, "mcp")
    conn = db.get(McpConnection, cap.id)
    assert conn is not None
    if payload.row_version is None:
        raise ApiError(422, "validation.invalid", "缺少 row_version")
    check_row_version(cap, payload.row_version, db)
    cap.name = _validate_name(db, cap.tenant_id, "mcp", payload.name, exclude_id=cap.id)
    if len(payload.description.strip()) > DESCRIPTION_MAX:
        raise ApiError(400, "validation.invalid_description", f"描述不能超过 {DESCRIPTION_MAX} 字")
    cap.description = payload.description.strip()
    changed = _apply_mcp_fields(conn, payload, creating=False)
    if payload.selected_tools is not None and not changed:
        offered = {t["name"] for t in conn.tools or []}
        unknown = set(payload.selected_tools) - offered
        if unknown:
            raise ApiError(422, "capability.unknown_tool", "所选工具不在最近一次测试返回的列表中，请重新测试连接")
        conn.selected_tools = [t["name"] for t in conn.tools if t["name"] in set(payload.selected_tools)]
    # CAP-032: the capability is usable only while the last test passed and a tool is ticked.
    if cap.status == "available" and not svc.is_ready(conn):
        cap.status = "draft"
    warning = None
    if cap.status == "draft" and svc.is_ready(conn):
        ensure_entitlement(db, cap.tenant_id, "mcp")
        warning = _try_request_enable(db, membership, cap)
    cap.row_version += 1
    cap.updated_by = _actor_name(db, membership)
    _audit(db, membership, "capability.updated", cap)
    db.commit()
    return _response(db, membership, cap).model_copy(update={"approval_warning": warning})


@router.post("/capabilities/mcp/{capability_id}/test", response_model=TestResult)
def test_mcp(
    capability_id: UUID,
    membership: Membership = Depends(require_tenant_permission("capabilities:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> TestResult:
    """CAP-030 / CAP-031. A failed test records the reason; an available connection is not disabled by it."""
    ensure_entitlement(db, membership.tenant_id, "mcp")
    cap = _load(db, membership, capability_id, "mcp")
    conn = db.get(McpConnection, cap.id)
    assert conn is not None
    ok, error = svc.run_test(conn)
    warning = _try_request_enable(db, membership, cap) if ok and cap.status == "draft" and svc.is_ready(conn) else None
    _audit(db, membership, "capability.tested", cap, result="ok" if ok else "failed")
    db.commit()
    return TestResult(ok=ok, error=error, tool_count=len(conn.tools or []), capability=_response(db, membership, cap).model_copy(update={"approval_warning": warning}))


@router.post("/capabilities/mcp/{capability_id}/oauth/start", response_model=OAuthStartResponse)
def oauth_start(
    capability_id: UUID,
    membership: Membership = Depends(require_tenant_permission("capabilities:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> OAuthStartResponse:
    """The address to send the administrator to (authorization code + PKCE). ``state`` carries tenant, user, the
    capability and the PKCE verifier, signed and valid for ten minutes."""
    ensure_entitlement(db, membership.tenant_id, "mcp")
    cap = _load(db, membership, capability_id, "mcp")
    conn = db.get(McpConnection, cap.id)
    if conn is None or conn.auth_type != "oauth" or not conn.oauth_authorize_url:
        raise ApiError(409, "capability.not_oauth", "该连接没有配置 OAuth 授权")
    verifier = secrets.token_urlsafe(48)
    challenge = urlsafe_b64encode(sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = svc.make_state(membership.tenant_id, membership.user_id, cap.id, verifier)
    return OAuthStartResponse(authorize_url=svc.authorize_url(conn, state, challenge))


@router.get("/capabilities/mcp/oauth/callback")
def oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """The authorization server sends the browser back here. The state must be ours, fresh and issued to the signed-in
    user; the code is exchanged server-side and the tokens are stored encrypted."""
    target = f"{settings.app_origin.rstrip('/')}/capabilities"
    try:
        claims = svc.read_state(state or "")
        if claims["u"] != str(user.id):
            raise ToolError("mcp.oauth_state", "授权请求不属于当前登录用户")
        cap_id, tenant_id = UUID(claims["c"]), UUID(claims["t"])
        cap = db.get(Capability, cap_id)
        conn = db.get(McpConnection, cap_id)
        membership = db.scalar(select(Membership).where(Membership.user_id == user.id, Membership.tenant_id == tenant_id, Membership.status == "active"))
        if cap is None or conn is None or cap.deleted_at is not None or cap.tenant_id != tenant_id or membership is None or not _can(db, membership, "capabilities:edit"):
            raise ToolError("mcp.oauth_state", "授权请求无效")
        target = f"{target}/mcp/{cap.id}"
        if error or not code:
            raise ToolError("mcp.oauth_denied", "授权被拒绝或失败")
        svc.exchange_code(conn, code, claims["v"])
        ok, _ = svc.run_test(conn)
        _audit(db, membership, "capability.authorized", cap, result="ok" if ok else "failed")
        if ok and cap.status == "draft" and svc.is_ready(conn):
            _try_request_enable(db, membership, cap)
        db.commit()
        return RedirectResponse(f"{target}?oauth=ok", status_code=status.HTTP_302_FOUND)
    except (ToolError, svc.McpError) as exc:
        db.rollback()
        return RedirectResponse(f"{target}?oauth=error&reason={exc.code}", status_code=status.HTTP_302_FOUND)
