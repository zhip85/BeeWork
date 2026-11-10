from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework import agent_service, rbac
from beework.agent_service import ConfigCorrupted, ReferenceCycle
from beework.api.assistants import _actor_name, _enabled_chat_models
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
from beework.models import AuditEvent, Membership, PlatformModel, TenantRole, utc_now
from beework.models.agents import Agent
from beework.rbac import require_tenant_permission
from beework.schemas.agents import (
    DEFAULT_ICONS,
    AgentChecksResponse,
    AgentConfigV1,
    AgentCountsResponse,
    AgentCreateRequest,
    AgentKind,
    AgentReferenceEntry,
    AgentReferencesResponse,
    AgentResponse,
    AgentSummaryResponse,
    AgentTransitionRequest,
    AgentUpdateRequest,
    FlowConfigV1,
)

router = APIRouter()

NAME_MAX = 50
DESCRIPTION_MAX = 200


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
        raise ApiError(409, "agent.name_taken", "该名称已被同类型的其他对象使用")


def _load(db: Session, membership: Membership, agent_id: UUID) -> Agent:
    agent = db.get(Agent, agent_id)
    if agent is None or agent.deleted_at is not None:
        raise ApiError(404, "not_found", "未找到指定智能体")
    ensure_resource_in_scope(membership, agent, db)
    return agent


def _audit(db: Session, membership: Membership, action: str, agent: Agent, **extra: str) -> None:
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            action=action,
            details={"agent_id": str(agent.id), "kind": agent.kind, "name": agent.name, **extra},
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
        return role is not None and permission in rbac.flatten_permissions(role.permissions)
    return permission in rbac.permissions_for_role(membership.role)


def _model_names(db: Session, ids: set[UUID]) -> dict[UUID, str]:
    if not ids:
        return {}
    return {
        model.id: model.display_name
        for model in db.scalars(select(PlatformModel).where(PlatformModel.id.in_(ids)))
    }


def _parsed(agent: Agent, raw: dict | None) -> AgentConfigV1 | FlowConfigV1 | None:
    try:
        return agent_service.parse_config(agent.kind, raw)
    except ConfigCorrupted:
        return None


def _summary_fields(agent: Agent, model_names: dict[UUID, str]) -> dict:
    config = _parsed(agent, agent.draft_config)
    model_id = getattr(config, "model_id", None)
    return {
        "model_id": model_id,
        "model_name": model_names.get(model_id) if model_id else None,
        "node_count": len(config.nodes) if isinstance(config, FlowConfigV1) else 0,
        "config_corrupted": config is None,
        "has_draft_changes": agent.status != "draft"
        and agent.available_config is not None
        and agent.available_config != agent.draft_config,
    }


def _summary(agent: Agent, model_names: dict[UUID, str]) -> AgentSummaryResponse:
    base = AgentSummaryResponse.model_validate(agent)
    return base.model_copy(update=_summary_fields(agent, model_names))


def _detail(db: Session, membership: Membership, agent: Agent) -> AgentResponse:
    fields = _summary_fields(agent, _model_names(db, {UUID(str(v)) for v in [agent.draft_config.get("model_id")] if v}))
    config = _parsed(agent, agent.draft_config)
    base = AgentSummaryResponse.model_validate(agent).model_dump()
    return AgentResponse(
        **{**base, **fields},
        config=config.model_dump(mode="json") if config is not None else None,
        available_config=agent.available_config,
        reference_count=len(agent_service.find_references(db, agent)),
        can_edit=_can(db, membership, "agents:edit"),
        can_publish=_can(db, membership, "agents:publish"),
    )


def _validate_model(db: Session, tenant_id: UUID, config: AgentConfigV1, current: dict | None) -> None:
    """The model must be one the tenant enabled; keeping an already chosen (now disabled) model is allowed."""
    if config.model_id is None:
        return
    if str((current or {}).get("model_id") or "") == str(config.model_id):
        return
    models, _ = _enabled_chat_models(db, tenant_id)
    if config.model_id not in {model.id for model, _ in models}:
        raise ApiError(422, "agent.model_unavailable", "所选模型未在当前环境启用")


# ---------------------------------------------------------------------------
# Endpoints.
# ---------------------------------------------------------------------------


@router.get("/agents/summary", response_model=AgentCountsResponse)
def agents_summary(
    membership: Membership = Depends(require_tenant_permission("agents:view")),
    db: Session = Depends(get_db),
) -> AgentCountsResponse:
    rows = db.execute(
        select(Agent.kind, func.count())
        .where(Agent.tenant_id == membership.tenant_id, Agent.deleted_at.is_(None))
        .group_by(Agent.kind)
    ).all()
    counts = dict(rows)
    return AgentCountsResponse(
        agent=counts.get("agent", 0), chatflow=counts.get("chatflow", 0), workflow=counts.get("workflow", 0)
    )


@router.get("/agents", response_model=Page[AgentSummaryResponse])
def list_agents(
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
    names = _model_names(
        db, {UUID(str(a.draft_config.get("model_id"))) for a in rows if (a.draft_config or {}).get("model_id")}
    )
    return Page[AgentSummaryResponse](
        items=[_summary(agent, names) for agent in rows],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.post("/agents", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
def create_agent(
    payload: AgentCreateRequest,
    membership: Membership = Depends(require_tenant_permission("agents:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> AgentResponse:
    if payload.is_team and payload.kind != "agent":
        raise ApiError(422, "agent.team_kind", "只有智能体可以是多智能体协作组")
    name = _validate_name(payload.name)
    _ensure_unique(db, membership.tenant_id, payload.kind, name)
    model_id = None
    if payload.kind == "agent":
        models, _ = _enabled_chat_models(db, membership.tenant_id)
        model_id = next((model.id for model, is_default in models if is_default), None)
    agent = Agent(
        tenant_id=membership.tenant_id,
        kind=payload.kind,
        is_team=payload.is_team,
        name=name,
        description=_validate_description(payload.description),
        icon=payload.icon or ("team" if payload.is_team else DEFAULT_ICONS[payload.kind]),
        status="draft",
        draft_config=agent_service.preset_config(payload.kind, payload.is_team, model_id),
        created_by=_actor_name(db, membership),
        updated_by=_actor_name(db, membership),
    )
    db.add(agent)
    db.flush()
    _audit(db, membership, "agent.created", agent)
    db.commit()
    return _detail(db, membership, agent)


@router.get("/agents/{agent_id}", response_model=AgentResponse)
def get_agent(
    agent_id: UUID,
    membership: Membership = Depends(require_tenant_permission("agents:view")),
    db: Session = Depends(get_db),
) -> AgentResponse:
    return _detail(db, membership, _load(db, membership, agent_id))


@router.put("/agents/{agent_id}", response_model=AgentResponse)
def update_agent(
    agent_id: UUID,
    payload: AgentUpdateRequest,
    membership: Membership = Depends(require_tenant_permission("agents:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> AgentResponse:
    """Saves the draft only. An available agent stays available and keeps executing its available config until
    “设为可用” is used again (AGT-024, RUL-002)."""
    agent = _load(db, membership, agent_id)
    if payload.row_version is None:
        raise ApiError(422, "validation.invalid", "缺少 row_version")
    check_row_version(agent, payload.row_version, db)
    if payload.name is not None:
        name = _validate_name(payload.name)
        _ensure_unique(db, membership.tenant_id, agent.kind, name, exclude_id=agent.id)
        agent.name = name
    if payload.description is not None:
        agent.description = _validate_description(payload.description)
    if payload.icon is not None:
        agent.icon = payload.icon
    if payload.config is not None:
        if _parsed(agent, agent.draft_config) is None:
            raise ApiError(409, "agent.config_corrupted", "该配置损坏，已保留原始数据，请恢复后再编辑。")
        try:
            config = agent_service.parse_config(agent.kind, payload.config)
        except ConfigCorrupted as exc:
            raise ApiError(422, "validation.invalid", f"配置格式不正确：{str(exc).splitlines()[-1][:120]}") from exc
        if isinstance(config, AgentConfigV1):
            _validate_model(db, membership.tenant_id, config, agent.draft_config)
        try:
            agent_service.ensure_no_cycle(db, agent, config)
        except ReferenceCycle as exc:
            if exc.self_reference:
                raise ApiError(422, "agent.self_reference", "智能体不能连接或引用自身") from exc
            raise ApiError(422, "agent.cycle", "智能体之间的连接不能形成环") from exc
        agent.draft_config = config.model_dump(mode="json")
    agent.row_version += 1
    agent.updated_by = _actor_name(db, membership)
    _audit(db, membership, "agent.draft_saved", agent)
    db.commit()
    return _detail(db, membership, agent)


@router.get("/agents/{agent_id}/checks", response_model=AgentChecksResponse)
def agent_checks(
    agent_id: UUID,
    membership: Membership = Depends(require_tenant_permission("agents:view")),
    db: Session = Depends(get_db),
) -> AgentChecksResponse:
    agent = _load(db, membership, agent_id)
    config = _parsed(agent, agent.draft_config)
    if config is None:
        raise ApiError(409, "agent.config_corrupted", "该配置损坏，已保留原始数据，请恢复后再编辑。")
    issues = agent_service.check_agent(db, agent, config)
    return AgentChecksResponse(ok=not issues, issues=issues)


@router.get("/agents/{agent_id}/references", response_model=AgentReferencesResponse)
def agent_references(
    agent_id: UUID,
    membership: Membership = Depends(require_tenant_permission("agents:view")),
    db: Session = Depends(get_db),
) -> AgentReferencesResponse:
    agent = _load(db, membership, agent_id)
    items = [AgentReferenceEntry(kind=kind, id=ref_id, name=name) for kind, ref_id, name in agent_service.find_references(db, agent)]
    return AgentReferencesResponse(total=len(items), items=items)


@router.post("/agents/{agent_id}/make-available", response_model=AgentResponse)
def make_available(
    agent_id: UUID,
    payload: AgentTransitionRequest | None = None,
    membership: Membership = Depends(require_tenant_permission("agents:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> AgentResponse:
    agent = _load(db, membership, agent_id)
    if payload is not None and payload.row_version is not None:
        check_row_version(agent, payload.row_version, db)
    if agent.status == "disabled":
        raise ApiError(409, "agent.disabled", "已停用的对象需先启用")
    config = _parsed(agent, agent.draft_config)
    if config is None:
        raise ApiError(409, "agent.config_corrupted", "该配置损坏，已保留原始数据，请恢复后再编辑。")
    issues = agent_service.check_agent(db, agent, config)
    if issues:
        raise ApiError(422, "agent.checks_failed", "；".join(issue.message for issue in issues))
    agent.available_config = config.model_dump(mode="json")
    agent.status = "available"
    agent.available_at = utc_now()
    agent.row_version += 1
    agent.updated_by = _actor_name(db, membership)
    _audit(db, membership, "agent.made_available", agent)
    db.commit()
    return _detail(db, membership, agent)


@router.post("/agents/{agent_id}/disable", response_model=AgentResponse)
def disable_agent(
    agent_id: UUID,
    membership: Membership = Depends(require_tenant_permission("agents:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> AgentResponse:
    agent = _load(db, membership, agent_id)
    if agent.status != "available":
        raise ApiError(409, "agent.not_available", "只有可用的对象可以停用")
    references = len(agent_service.find_references(db, agent))
    agent.status = "disabled"
    agent.row_version += 1
    agent.updated_by = _actor_name(db, membership)
    _audit(db, membership, "agent.disabled", agent, references=str(references))
    db.commit()
    return _detail(db, membership, agent)


@router.post("/agents/{agent_id}/enable", response_model=AgentResponse)
def enable_agent(
    agent_id: UUID,
    membership: Membership = Depends(require_tenant_permission("agents:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> AgentResponse:
    agent = _load(db, membership, agent_id)
    if agent.status != "disabled":
        raise ApiError(409, "agent.not_disabled", "只有已停用的对象可以启用")
    agent.status = "available" if agent.available_config is not None else "draft"
    agent.row_version += 1
    agent.updated_by = _actor_name(db, membership)
    _audit(db, membership, "agent.enabled", agent)
    db.commit()
    return _detail(db, membership, agent)


@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_agent(
    agent_id: UUID,
    membership: Membership = Depends(require_tenant_permission("agents:delete")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    """Soft delete. Referencing assistants / agents are not blocked: they show the reference as invalid (RUL-011)."""
    agent = _load(db, membership, agent_id)
    references = len(agent_service.find_references(db, agent))
    agent.deleted_at = utc_now()
    agent.updated_by = _actor_name(db, membership)
    _audit(db, membership, "agent.deleted", agent, references=str(references))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
