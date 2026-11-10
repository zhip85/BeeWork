"""Flow studio API (T-060): Chatflows and Workflows. Paths keep the historical ``/agents`` prefix; the product's
“智能体” lives under ``/assistants`` (T-067)."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework import agent_service
from beework.agent_service import ConfigCorrupted, ReferenceCycle
from beework.api.assistants import _actor_name
from beework.api.conventions import (
    ApiError,
    Page,
    Pagination,
    check_row_version,
    ensure_resource_in_scope,
    get_pagination,
    normalize_name,
    paginate,
)
from beework.auth import require_csrf
from beework.db import get_db
from beework.models import AuditEvent, Membership, TenantRole, utc_now
from beework.models.agents import Agent, FlowVersion
from beework.rbac import flatten_permissions, permissions_for_role, require_tenant_permission
from beework.schemas.agents import (
    DEFAULT_ICONS,
    AgentChecksResponse,
    AgentCountsResponse,
    AgentCreateRequest,
    AgentKind,
    AgentReferenceEntry,
    AgentReferencesResponse,
    AgentResponse,
    AgentSummaryResponse,
    AgentUpdateRequest,
    FlowConfigV1,
    FlowVersionResponse,
    PublishRequest,
)

router = APIRouter()

NAME_MAX = 50
DESCRIPTION_MAX = 200
CORRUPTED = "该配置损坏，已保留原始数据，请恢复后再编辑。"


def _validate_name(value: str | None) -> str:
    return normalize_name(value, NAME_MAX)


def _validate_description(value: str) -> str:
    text = (value or "").strip()
    if len(text) > DESCRIPTION_MAX:
        raise ApiError(400, "validation.invalid_description", f"描述不能超过 {DESCRIPTION_MAX} 字")
    return text


def _ensure_unique(db: Session, tenant_id: UUID, kind: str, name: str, exclude_id: UUID | None = None) -> None:
    """Names are unique within a tenant and a type, case-insensitively (3.3.2); another type may reuse it."""
    statement = select(Agent.id).where(
        Agent.tenant_id == tenant_id,
        Agent.kind == kind,
        Agent.deleted_at.is_(None),
        func.lower(Agent.name) == name.lower(),
    )
    if exclude_id is not None:
        statement = statement.where(Agent.id != exclude_id)
    if db.scalar(statement.limit(1)) is not None:
        raise ApiError(409, "agent.name_taken", "该名称已被同类型的其他流程使用")


def _load(db: Session, membership: Membership, flow_id: UUID) -> Agent:
    flow = db.get(Agent, flow_id)
    if flow is None or flow.deleted_at is not None:
        raise ApiError(404, "not_found", "未找到指定流程")
    ensure_resource_in_scope(membership, flow, db)
    return flow


def _audit(db: Session, membership: Membership, action: str, flow: Agent, **extra: str) -> None:
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            action=action,
            details={"flow_id": str(flow.id), "kind": flow.kind, "name": flow.name, **extra},
        )
    )


def _can(db: Session, membership: Membership, permission: str) -> bool:
    if membership.role == "tenant_admin":
        return True
    if membership.custom_role_id is not None:
        role = db.scalar(
            select(TenantRole).where(
                TenantRole.id == membership.custom_role_id,
                TenantRole.tenant_id == membership.tenant_id,
                TenantRole.is_active.is_(True),
            )
        )
        return role is not None and permission in flatten_permissions(role.permissions)
    return permission in permissions_for_role(membership.role)


def _parsed(raw: dict | None) -> FlowConfigV1 | None:
    try:
        return agent_service.parse_config(raw)
    except ConfigCorrupted:
        return None


def _live_version(db: Session, flow: Agent) -> str | None:
    if flow.live_version_id is None:
        return None
    return db.scalar(select(FlowVersion.version).where(FlowVersion.id == flow.live_version_id))


def _summary_fields(db: Session, flow: Agent) -> dict:
    config = _parsed(flow.draft_config)
    return {
        "node_count": len(config.nodes) if config is not None else 0,
        "config_corrupted": config is None,
        "has_draft_changes": flow.published_config is not None and flow.published_config != flow.draft_config,
        "live_version": _live_version(db, flow),
    }


def _summary(db: Session, flow: Agent) -> AgentSummaryResponse:
    return AgentSummaryResponse.model_validate(flow).model_copy(update=_summary_fields(db, flow))


def _detail(db: Session, membership: Membership, flow: Agent) -> AgentResponse:
    config = _parsed(flow.draft_config)
    base = AgentSummaryResponse.model_validate(flow).model_dump()
    return AgentResponse(
        **{**base, **_summary_fields(db, flow)},
        config=config.model_dump(mode="json") if config is not None else None,
        published_config=flow.published_config,
        reference_count=len(agent_service.find_references(db, flow)),
        invalid_nodes=agent_service.invalid_node_ids(db, flow, config) if config is not None else [],
        versions_count=db.scalar(select(func.count()).select_from(FlowVersion).where(FlowVersion.flow_id == flow.id)) or 0,
        can_edit=_can(db, membership, "agents:edit"),
        can_publish=_can(db, membership, "agents:publish"),
    )


# ---------------------------------------------------------------------------
# Endpoints.
# ---------------------------------------------------------------------------


@router.get("/agents/summary", response_model=AgentCountsResponse)
def flows_summary(
    membership: Membership = Depends(require_tenant_permission("agents:view")),
    db: Session = Depends(get_db),
) -> AgentCountsResponse:
    rows = db.execute(
        select(Agent.kind, func.count())
        .where(Agent.tenant_id == membership.tenant_id, Agent.deleted_at.is_(None))
        .group_by(Agent.kind)
    ).all()
    counts = dict(rows)
    return AgentCountsResponse(chatflow=counts.get("chatflow", 0), workflow=counts.get("workflow", 0))


@router.get("/agents", response_model=Page[AgentSummaryResponse])
def list_flows(
    kind: AgentKind | None = None,
    q: str | None = Query(default=None, max_length=100),
    pagination: Pagination = Depends(get_pagination),
    membership: Membership = Depends(require_tenant_permission("agents:view")),
    db: Session = Depends(get_db),
) -> Page[AgentSummaryResponse]:
    statement = select(Agent).where(Agent.tenant_id == membership.tenant_id, Agent.deleted_at.is_(None))
    if kind:
        statement = statement.where(Agent.kind == kind)
    if q and q.strip():
        statement = statement.where(func.lower(Agent.name).like(f"%{q.strip().lower()}%"))
    rows, total = paginate(db, statement.order_by(Agent.updated_at.desc(), Agent.id), pagination)
    return Page[AgentSummaryResponse](
        items=[_summary(db, flow) for flow in rows], total=total, limit=pagination.limit, offset=pagination.offset
    )


@router.post("/agents", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
def create_flow(
    payload: AgentCreateRequest,
    membership: Membership = Depends(require_tenant_permission("agents:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> AgentResponse:
    name = _validate_name(payload.name)
    _ensure_unique(db, membership.tenant_id, payload.kind, name)
    flow = Agent(
        tenant_id=membership.tenant_id,
        kind=payload.kind,
        name=name,
        description=_validate_description(payload.description),
        icon=payload.icon or DEFAULT_ICONS[payload.kind],
        status="draft",
        draft_config=agent_service.preset_config(payload.kind),
        created_by=_actor_name(db, membership),
        updated_by=_actor_name(db, membership),
    )
    db.add(flow)
    db.flush()
    _audit(db, membership, "flow.created", flow)
    db.commit()
    return _detail(db, membership, flow)


@router.get("/agents/{flow_id}", response_model=AgentResponse)
def get_flow(
    flow_id: UUID,
    membership: Membership = Depends(require_tenant_permission("agents:view")),
    db: Session = Depends(get_db),
) -> AgentResponse:
    return _detail(db, membership, _load(db, membership, flow_id))


@router.put("/agents/{flow_id}", response_model=AgentResponse)
def update_flow(
    flow_id: UUID,
    payload: AgentUpdateRequest,
    membership: Membership = Depends(require_tenant_permission("agents:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> AgentResponse:
    """Saves the draft only. A published flow stays published and callers keep executing the live version until
    “发布流程” is used again (AGT-024, RUL-002)."""
    flow = _load(db, membership, flow_id)
    if payload.row_version is None:
        raise ApiError(422, "validation.invalid", "缺少 row_version")
    check_row_version(flow, payload.row_version, db)
    if payload.name is not None:
        name = _validate_name(payload.name)
        _ensure_unique(db, membership.tenant_id, flow.kind, name, exclude_id=flow.id)
        flow.name = name
    if payload.description is not None:
        flow.description = _validate_description(payload.description)
    if payload.icon is not None:
        flow.icon = payload.icon
    if payload.config is not None:
        if _parsed(flow.draft_config) is None:
            raise ApiError(409, "agent.config_corrupted", CORRUPTED)
        try:
            config = agent_service.parse_config(payload.config)
        except ConfigCorrupted as exc:
            raise ApiError(422, "validation.invalid", f"配置格式不正确：{str(exc).splitlines()[-1][:120]}") from exc
        try:
            agent_service.ensure_no_cycle(db, flow, config)
        except ReferenceCycle as exc:
            if exc.self_reference:
                raise ApiError(422, "agent.self_reference", "流程不能调用自身") from exc
            raise ApiError(422, "agent.cycle", "流程之间的调用不能形成环") from exc
        flow.draft_config = config.model_dump(mode="json")
    flow.row_version += 1
    flow.updated_by = _actor_name(db, membership)
    _audit(db, membership, "flow.draft_saved", flow)
    db.commit()
    return _detail(db, membership, flow)


@router.get("/agents/{flow_id}/checks", response_model=AgentChecksResponse)
def flow_checks(
    flow_id: UUID,
    membership: Membership = Depends(require_tenant_permission("agents:view")),
    db: Session = Depends(get_db),
) -> AgentChecksResponse:
    flow = _load(db, membership, flow_id)
    config = _parsed(flow.draft_config)
    if config is None:
        raise ApiError(409, "agent.config_corrupted", CORRUPTED)
    issues = agent_service.check_flow(db, flow, config)
    return AgentChecksResponse(ok=not issues, issues=issues)


@router.get("/agents/{flow_id}/references", response_model=AgentReferencesResponse)
def flow_references(
    flow_id: UUID,
    membership: Membership = Depends(require_tenant_permission("agents:view")),
    db: Session = Depends(get_db),
) -> AgentReferencesResponse:
    flow = _load(db, membership, flow_id)
    items = [AgentReferenceEntry(kind=kind, id=ref_id, name=name) for kind, ref_id, name in agent_service.find_references(db, flow)]
    return AgentReferencesResponse(total=len(items), items=items)


@router.get("/agents/{flow_id}/versions", response_model=list[FlowVersionResponse])
def flow_versions(
    flow_id: UUID,
    membership: Membership = Depends(require_tenant_permission("agents:view")),
    db: Session = Depends(get_db),
) -> list[FlowVersionResponse]:
    flow = _load(db, membership, flow_id)
    rows = db.scalars(select(FlowVersion).where(FlowVersion.flow_id == flow.id).order_by(FlowVersion.version_seq.desc()))
    return [
        FlowVersionResponse.model_validate(row).model_copy(update={"is_live": row.id == flow.live_version_id})
        for row in rows
    ]


@router.post("/agents/{flow_id}/publish", response_model=AgentResponse)
def publish(
    flow_id: UUID,
    payload: PublishRequest | None = None,
    membership: Membership = Depends(require_tenant_permission("agents:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> AgentResponse:
    """演进 PRD 6.8: freezes the draft as a new immutable version once the configuration check passes."""
    payload = payload or PublishRequest()
    flow = _load(db, membership, flow_id)
    if payload.row_version is not None:
        check_row_version(flow, payload.row_version, db)
    if flow.status == "disabled":
        raise ApiError(409, "agent.disabled", "已停用的流程需先启用")
    config = _parsed(flow.draft_config)
    if config is None:
        raise ApiError(409, "agent.config_corrupted", CORRUPTED)
    issues = agent_service.check_flow(db, flow, config)
    if issues:
        raise ApiError(422, "agent.checks_failed", "；".join(issue.message for issue in issues))
    actor = _actor_name(db, membership)
    version = agent_service.publish_flow(db, flow, config, payload.change_note, actor)
    flow.row_version += 1
    flow.updated_by = actor
    _audit(db, membership, "flow.published", flow, version=version.version)
    db.commit()
    return _detail(db, membership, flow)


@router.post("/agents/{flow_id}/duplicate", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
def duplicate_flow(
    flow_id: UUID,
    membership: Membership = Depends(require_tenant_permission("agents:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> AgentResponse:
    """“复制为草稿”: a new draft from the source's current draft configuration (never published)."""
    source = _load(db, membership, flow_id)
    if _parsed(source.draft_config) is None:
        raise ApiError(409, "agent.config_corrupted", CORRUPTED)
    base, suffix = source.name[: NAME_MAX - 8], " 副本"
    name, index = f"{base}{suffix}", 1
    while db.scalar(
        select(Agent.id).where(
            Agent.tenant_id == membership.tenant_id, Agent.kind == source.kind, Agent.deleted_at.is_(None),
            func.lower(Agent.name) == name.lower(),
        )
    ):
        index += 1
        name = f"{base}{suffix} {index}"
    actor = _actor_name(db, membership)
    copy = Agent(
        tenant_id=membership.tenant_id, kind=source.kind, name=name, description=source.description, icon=source.icon,
        status="draft", draft_config=source.draft_config, created_by=actor, updated_by=actor,
    )
    db.add(copy)
    db.flush()
    _audit(db, membership, "flow.duplicated", copy, source_id=str(source.id))
    db.commit()
    return _detail(db, membership, copy)


@router.post("/agents/{flow_id}/disable", response_model=AgentResponse)
def disable_flow(
    flow_id: UUID,
    membership: Membership = Depends(require_tenant_permission("agents:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> AgentResponse:
    flow = _load(db, membership, flow_id)
    if flow.status != "published":
        raise ApiError(409, "agent.not_published", "只有已发布的流程可以停用")
    references = len(agent_service.find_references(db, flow))
    flow.status = "disabled"
    flow.row_version += 1
    flow.updated_by = _actor_name(db, membership)
    _audit(db, membership, "flow.disabled", flow, references=str(references))
    db.commit()
    return _detail(db, membership, flow)


@router.post("/agents/{flow_id}/enable", response_model=AgentResponse)
def enable_flow(
    flow_id: UUID,
    membership: Membership = Depends(require_tenant_permission("agents:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> AgentResponse:
    flow = _load(db, membership, flow_id)
    if flow.status != "disabled":
        raise ApiError(409, "agent.not_disabled", "只有已停用的流程可以启用")
    flow.status = "published" if flow.live_version_id is not None else "draft"
    flow.row_version += 1
    flow.updated_by = _actor_name(db, membership)
    _audit(db, membership, "flow.enabled", flow)
    db.commit()
    return _detail(db, membership, flow)


@router.delete("/agents/{flow_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_flow(
    flow_id: UUID,
    membership: Membership = Depends(require_tenant_permission("agents:delete")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    """Soft delete. Referencing flows / connections / apps are not blocked: they show the reference as invalid (RUL-011)."""
    flow = _load(db, membership, flow_id)
    references = len(agent_service.find_references(db, flow))
    flow.deleted_at = utc_now()
    flow.updated_by = _actor_name(db, membership)
    _audit(db, membership, "flow.deleted", flow, references=str(references))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
