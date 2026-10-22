
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.auth import (
    require_csrf,
)
from beework.db import get_db
from beework.gateway_sync import sync_tenant_environment, tenant_gateway_key
from beework.integrations.litellm import GatewayError, model_alias
from beework.model_testing import run_model_test
from beework.models import (
    AuditEvent,
    Membership,
    PlatformModel,
    PlatformModelTenant,
    Tenant,
    TenantModelEnvironment,
    TenantModelSelection,
)
from beework.rbac import (
    require_tenant_permission,
)
from beework.schemas import (
    ModelTestResponse,
    TenantModelEnvironmentResponse,
    TenantModelEnvironmentWriteRequest,
    TenantModelOption,
    TenantModelTestRequest,
)
from beework.tenant_access import ENVIRONMENTS, environments_for_plan

router = APIRouter()


def _ensure_environment_available(tenant: Tenant, environment: str) -> None:
    if environment not in ENVIRONMENTS:
        raise HTTPException(status_code=422, detail="环境必须是开发、预发或生产")
    if environment not in environments_for_plan(tenant.plan):
        raise HTTPException(status_code=403, detail="当前套餐仅提供生产环境，开发与预发环境需企业版")


def _eligible_tenant_models(db: Session, tenant: Tenant) -> list[PlatformModel]:
    models = list(db.scalars(
        select(PlatformModel)
        .where(
            PlatformModel.status == "healthy",
            PlatformModel.region == tenant.region,
        )
        .order_by(PlatformModel.provider, PlatformModel.display_name)
    ))
    eligible = []
    for model in models:
        if model.availability == "all_plans":
            eligible.append(model)
        elif model.availability == "professional_plus" and tenant.plan in {
            "professional", "enterprise"
        }:
            eligible.append(model)
        elif model.availability == "enterprise" and tenant.plan == "enterprise":
            eligible.append(model)
        elif model.availability == "allowlist" and db.scalar(
            select(PlatformModelTenant.model_id).where(
                PlatformModelTenant.model_id == model.id,
                PlatformModelTenant.tenant_id == tenant.id,
            )
        ):
            eligible.append(model)
    return eligible


def _tenant_model_environment_response(
    db: Session, tenant: Tenant, environment: str
) -> TenantModelEnvironmentResponse:
    config = db.scalar(
        select(TenantModelEnvironment).where(
            TenantModelEnvironment.tenant_id == tenant.id,
            TenantModelEnvironment.environment == environment,
        )
    )
    selections = list(
        db.scalars(
            select(TenantModelSelection).where(
                TenantModelSelection.environment_id == config.id
            )
        )
    ) if config else []
    eligible_models = _eligible_tenant_models(db, tenant)
    eligible_by_id = {model.id: model for model in eligible_models}
    enabled_ids = [selection.model_id for selection in selections]
    default_ids = {
        model_type: next(
            (
                selection.model_id
                for selection in selections
                if selection.model_type == model_type and selection.is_default
            ),
            None,
        )
        for model_type in ("chat", "embedding", "rerank")
    }
    return TenantModelEnvironmentResponse(
        tenant_id=tenant.id,
        environment=environment,
        available_environments=list(environments_for_plan(tenant.plan)),
        plan=tenant.plan,
        region=tenant.region,
        version=config.version if config else 0,
        updated_at=config.updated_at if config else None,
        models=[
            TenantModelOption(
                id=model.id,
                display_name=model.display_name,
                provider=model.provider,
                model_type=model.model_type,
                region=model.region,
                input_price_per_million=model.input_price_per_million,
            )
            for model in eligible_models
        ],
        enabled_model_ids=enabled_ids,
        default_model_ids=default_ids,
        unavailable_enabled_model_ids=[
            model_id for model_id in enabled_ids if model_id not in eligible_by_id
        ],
        gateway_sync_status=config.gateway_sync_status if config else "not_configured",
        gateway_error=config.gateway_error if config else None,
    )


@router.get(
    "/tenant/model-environments/{environment}",
    response_model=TenantModelEnvironmentResponse,
)
def get_tenant_model_environment(
    environment: str,
    membership: Membership = Depends(require_tenant_permission("models:view")),
    db: Session = Depends(get_db),
) -> TenantModelEnvironmentResponse:
    tenant = db.get(Tenant, membership.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="未找到企业工作区")
    _ensure_environment_available(tenant, environment)
    return _tenant_model_environment_response(db, tenant, environment)


@router.put(
    "/tenant/model-environments/{environment}",
    response_model=TenantModelEnvironmentResponse,
)
def update_tenant_model_environment(
    environment: str,
    payload: TenantModelEnvironmentWriteRequest,
    membership: Membership = Depends(require_tenant_permission("models:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> TenantModelEnvironmentResponse:
    tenant = db.scalar(
        select(Tenant).where(Tenant.id == membership.tenant_id).with_for_update(key_share=True)
    )
    if tenant is None:
        raise HTTPException(status_code=404, detail="未找到企业工作区")
    _ensure_environment_available(tenant, environment)
    config = db.scalar(
        select(TenantModelEnvironment)
        .where(
            TenantModelEnvironment.tenant_id == tenant.id,
            TenantModelEnvironment.environment == environment,
        )
        .with_for_update()
    )
    current_version = config.version if config else 0
    if payload.expected_version != current_version:
        raise HTTPException(status_code=409, detail="模型环境配置已被其他人修改，请刷新后重试")
    eligible = {
        model.id: model for model in _eligible_tenant_models(db, tenant)
    }
    requested_ids = set(payload.enabled_model_ids)
    if requested_ids - eligible.keys():
        raise HTTPException(
            status_code=422,
            detail="所选模型已停用，或不符合当前套餐、区域和租户开放范围",
        )
    for model_type, model_id in payload.default_model_ids.items():
        if model_id is not None and eligible[model_id].model_type != model_type:
            raise HTTPException(status_code=422, detail="默认模型类型与配置项不匹配")
    if config is None:
        config = TenantModelEnvironment(
            tenant_id=tenant.id,
            environment=environment,
            version=0,
        )
        db.add(config)
        db.flush()
    db.query(TenantModelSelection).filter(
        TenantModelSelection.environment_id == config.id
    ).delete(synchronize_session=False)
    for model_id in payload.enabled_model_ids:
        model = eligible[model_id]
        db.add(
            TenantModelSelection(
                environment_id=config.id,
                model_id=model_id,
                model_type=model.model_type,
                is_default=payload.default_model_ids.get(model.model_type) == model_id,
            )
        )
    config.version += 1
    db.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=membership.user_id,
            action="model_environment.updated",
            details={
                "environment": environment,
                "version": str(config.version),
                "enabled_model_count": str(len(payload.enabled_model_ids)),
            },
        )
    )
    db.commit()
    db.refresh(config)
    sync_tenant_environment(db, tenant, config)
    return _tenant_model_environment_response(db, tenant, environment)


def _environment_config(
    db: Session, membership: Membership, environment: str
) -> tuple[Tenant, TenantModelEnvironment]:
    tenant = db.get(Tenant, membership.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="未找到企业工作区")
    _ensure_environment_available(tenant, environment)
    config = db.scalar(
        select(TenantModelEnvironment).where(
            TenantModelEnvironment.tenant_id == tenant.id,
            TenantModelEnvironment.environment == environment,
        )
    )
    if config is None:
        raise HTTPException(status_code=409, detail="请先保存该环境的模型配置")
    return tenant, config


@router.post(
    "/tenant/model-environments/{environment}/sync",
    response_model=TenantModelEnvironmentResponse,
)
def resync_tenant_model_environment(
    environment: str,
    membership: Membership = Depends(require_tenant_permission("models:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> TenantModelEnvironmentResponse:
    tenant, config = _environment_config(db, membership, environment)
    sync_tenant_environment(db, tenant, config)
    return _tenant_model_environment_response(db, tenant, environment)


@router.post(
    "/tenant/model-environments/{environment}/test",
    response_model=ModelTestResponse,
)
def test_tenant_model(
    environment: str,
    payload: TenantModelTestRequest,
    membership: Membership = Depends(require_tenant_permission("models:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ModelTestResponse:
    """Call an enabled model with this environment's own gateway key."""
    tenant, config = _environment_config(db, membership, environment)
    enabled = db.scalar(
        select(TenantModelSelection.id).where(
            TenantModelSelection.environment_id == config.id,
            TenantModelSelection.model_id == payload.model_id,
        )
    )
    if enabled is None:
        raise HTTPException(status_code=422, detail="该模型未在当前环境启用")
    try:
        api_key = tenant_gateway_key(config)
    except (GatewayError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    result = run_model_test(api_key=api_key, model=model_alias(payload.model_id), payload=payload)
    db.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=membership.user_id,
            action="model_environment.tested",
            result="success" if result.ok else "failure",
            details={
                "environment": environment,
                "model_id": str(payload.model_id),
                "access_only": str(payload.access_only),
            },
        )
    )
    db.commit()
    return result
