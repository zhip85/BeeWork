from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.api.assistants import (
    MODEL_ENVIRONMENT,
    _enabled_chat_models,
    _load,
    _visible_statement,
)
from beework.api.conventions import ApiError
from beework.auth import require_csrf
from beework.db import get_db
from beework.entitlements import check_quota, get_usage_counter
from beework.models import AuditEvent, Membership, Tenant, TenantModelEnvironment
from beework.models.assistants import Assistant
from beework.models.runtime import Run
from beework.rbac import require_tenant_permission
from beework.runtime.dsl import dsl_from_draft
from beework.runtime.errors import RuntimeFailure
from beework.runtime.executor import SERVING_TENANT_STATUSES
from beework.schemas.runs import CreateRunRequest, CreateRunResponse, RunResponse
from beework.tenant_access import tenant_access_mode
from beework.worker.events import publish_run_event
from beework.worker.queue import enqueue

router = APIRouter()

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


def _admit(db: Session, membership: Membership, assistant: Assistant) -> None:
    """Admission checks of PRD 5.5 step 2; raises 409 with a machine readable code."""
    tenant = db.get(Tenant, membership.tenant_id)
    if tenant is None or tenant.status not in SERVING_TENANT_STATUSES or tenant_access_mode(
        tenant.status, membership.role
    ) != "full":
        raise ApiError(409, "runtime.tenant_unavailable", "企业工作区当前不可运行助理")
    if assistant.status == "disabled":
        raise ApiError(409, "runtime.assistant_disabled", "助理已停用")
    environment = db.scalar(
        select(TenantModelEnvironment).where(
            TenantModelEnvironment.tenant_id == membership.tenant_id,
            TenantModelEnvironment.environment == MODEL_ENVIRONMENT,
        )
    )
    if environment is None or environment.gateway_sync_status != "synced":
        raise ApiError(409, "runtime.gateway_not_ready", "该环境尚未同步到 AI 网关，请先在设置中保存模型配置")
    # Model points are metered by T-011; until its counter exists this check is skipped.
    if get_usage_counter("monthly_points") is not None:
        if not check_quota(db, membership.tenant_id, "monthly_points", 0).allowed:
            raise ApiError(409, "plan.quota_exceeded", "本月模型点数已用完，请联系平台升级")


def _load_run(db: Session, membership: Membership, run_id: UUID, *, lock: bool = False) -> Run:
    """A run of this tenant whose assistant the caller may see; anything else is a 404."""
    statement = (
        select(Run)
        .join(Assistant, (Assistant.id == Run.assistant_id) & (Assistant.tenant_id == Run.tenant_id))
        .where(Run.id == run_id, Run.tenant_id == membership.tenant_id)
    )
    statement = _visible_statement(statement, membership, db)
    if lock:
        statement = statement.with_for_update(of=Run)
    run = db.scalar(statement)
    if run is None:
        raise ApiError(404, "not_found", "未找到指定运行")
    return run


@router.post(
    "/assistants/{assistant_id}/runs",
    response_model=CreateRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_debug_run(
    assistant_id: UUID,
    payload: CreateRunRequest,
    membership: Membership = Depends(require_tenant_permission("assistants:test")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> CreateRunResponse:
    """Start a debug run of the assistant's current draft; follow it via ``GET /runs/{id}/events``."""
    assistant = _load(db, membership, assistant_id)
    _admit(db, membership, assistant)
    try:
        dsl = dsl_from_draft(assistant)
    except RuntimeFailure as exc:
        raise ApiError(409, exc.code, exc.message) from exc
    models, _ = _enabled_chat_models(db, membership.tenant_id)
    if str(assistant.draft_config.get("model_id")) not in {str(model.id) for model, _ in models}:
        raise ApiError(409, "runtime.model_unavailable", "助理所选模型未在当前环境启用")
    if len(payload.message.strip()) > dsl.limits.max_input_chars:
        raise ApiError(422, "runtime.input_rejected", f"消息长度不能超过 {dsl.limits.max_input_chars} 个字符")

    run = Run(
        tenant_id=membership.tenant_id,
        assistant_id=assistant.id,
        draft_row_version=assistant.row_version,
        environment=MODEL_ENVIRONMENT,
        source="debug",
        status="queued",
        config=dsl.model_dump(mode="json"),
        input={"message": payload.message},
        created_by=membership.user_id,
    )
    db.add(run)
    db.flush()
    enqueue(
        db,
        "runtime.execute",
        {"run_id": str(run.id)},
        tenant_id=membership.tenant_id,
        idempotency_key=f"runtime.execute:{run.id}",
    )
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            action="assistant.run_started",
            details={"assistant_id": str(assistant.id), "run_id": str(run.id), "source": "debug"},
        )
    )
    # Commits the run, its job and the audit row together with the first ``queued`` event.
    publish_run_event(db, run.id, "queued", {"assistant_id": str(assistant.id)}, tenant_id=membership.tenant_id)
    return CreateRunResponse(run_id=run.id)


@router.get("/runs/{run_id}", response_model=RunResponse)
def get_run(
    run_id: UUID,
    membership: Membership = Depends(require_tenant_permission("assistants:view")),
    db: Session = Depends(get_db),
) -> RunResponse:
    return RunResponse.model_validate(_load_run(db, membership, run_id), from_attributes=True)


@router.post("/runs/{run_id}/cancel", response_model=RunResponse)
def cancel_run(
    run_id: UUID,
    membership: Membership = Depends(require_tenant_permission("assistants:test")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> RunResponse:
    run = _load_run(db, membership, run_id, lock=True)
    if run.status in TERMINAL_STATUSES:
        raise ApiError(409, "run.not_cancellable", "运行已结束")
    run.cancel_requested = True
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            action="assistant.run_cancelled",
            details={"assistant_id": str(run.assistant_id), "run_id": str(run.id)},
        )
    )
    db.commit()
    # A queued run has no worker attached yet, so finish it here; a running one stops at its next chunk.
    if run.status == "queued":
        from beework.runtime.executor import execute_run

        execute_run(db, run.id)
        db.refresh(run)
    return RunResponse.model_validate(run, from_attributes=True)
