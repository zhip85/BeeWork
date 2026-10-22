from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from beework.api.conventions import require_platform_admin
from beework.auth import (
    require_csrf,
)
from beework.config import settings
from beework.db import get_db
from beework.gateway_sync import sync_environments_using_model, sync_platform_model
from beework.integrations.litellm import model_alias
from beework.model_secrets import encrypt_model_api_key
from beework.model_testing import run_model_test
from beework.models import (
    AuditEvent,
    PlatformModel,
    PlatformModelTenant,
    Tenant,
    User,
)
from beework.schemas import (
    ModelTestRequest,
    ModelTestResponse,
    PlatformModelListResponse,
    PlatformModelResponse,
    PlatformModelWriteRequest,
)

router = APIRouter()


def _platform_model_response(model: PlatformModel) -> PlatformModelResponse:
    return PlatformModelResponse(
        id=model.id,
        display_name=model.display_name,
        provider=model.provider,
        model_type=model.model_type,
        availability=model.availability,
        input_price_per_million=model.input_price_per_million,
        output_price_per_million=model.output_price_per_million,
        input_points_per_thousand=model.input_points_per_thousand,
        output_points_per_thousand=model.output_points_per_thousand,
        status=model.status,
        provider_model_id=model.provider_model_id,
        endpoint_url=model.endpoint_url,
        credential_configured=bool(model.encrypted_api_key),
        api_key_masked=f"••••••{model.api_key_last_four}",
        api_version=model.api_version,
        region=model.region,
        context_window=model.context_window,
        max_output_tokens=model.max_output_tokens,
        default_temperature=model.default_temperature,
        rpm_limit=model.rpm_limit,
        fallback_model_id=model.fallback_model_id,
        allowlist_tenant_ids=[entry.tenant_id for entry in model.allowlist],
        created_at=model.created_at,
        gateway_sync_status=model.gateway_sync_status,
        gateway_error=model.gateway_error,
        gateway_synced_at=model.gateway_synced_at,
        updated_at=model.updated_at,
    )


def _validate_platform_model_references(
    db: Session,
    payload: PlatformModelWriteRequest,
    *,
    model_id: UUID | None = None,
) -> list[Tenant]:
    tenants = list(
        db.scalars(
            select(Tenant).where(Tenant.id.in_(payload.allowlist_tenant_ids))
        )
    ) if payload.allowlist_tenant_ids else []
    if len(tenants) != len(set(payload.allowlist_tenant_ids)):
        raise HTTPException(status_code=422, detail="白名单包含不存在的租户")

    if payload.fallback_model_id is None:
        return tenants
    if payload.fallback_model_id == model_id:
        raise HTTPException(status_code=422, detail="模型不能降级到自身")
    fallback = db.get(PlatformModel, payload.fallback_model_id)
    if fallback is None:
        raise HTTPException(status_code=422, detail="降级模型不存在")
    if fallback.model_type != payload.model_type or fallback.region != payload.region:
        raise HTTPException(status_code=422, detail="降级模型必须与当前模型类型和区域一致")

    visited: set[UUID] = set()
    current: PlatformModel | None = fallback
    while current is not None:
        if current.id == model_id:
            raise HTTPException(status_code=422, detail="降级模型不能形成循环引用")
        if current.id in visited:
            raise HTTPException(status_code=409, detail="现有模型降级关系已存在循环")
        visited.add(current.id)
        current = (
            db.get(PlatformModel, current.fallback_model_id)
            if current.fallback_model_id
            else None
        )
    return tenants


def _write_platform_model(
    db: Session,
    model: PlatformModel,
    payload: PlatformModelWriteRequest,
    *,
    create: bool,
) -> None:
    tenant_rows = _validate_platform_model_references(
        db,
        payload,
        model_id=None if create else model.id,
    )
    if create and not payload.api_key:
        raise HTTPException(status_code=422, detail="新增模型必须填写 API Key")
    if payload.api_key:
        model.encrypted_api_key = encrypt_model_api_key(payload.api_key)
        model.api_key_last_four = payload.api_key[-4:]

    for field in (
        "display_name",
        "provider",
        "model_type",
        "availability",
        "input_price_per_million",
        "output_price_per_million",
        "input_points_per_thousand",
        "output_points_per_thousand",
        "status",
        "provider_model_id",
        "endpoint_url",
        "api_version",
        "region",
        "context_window",
        "max_output_tokens",
        "default_temperature",
        "rpm_limit",
        "fallback_model_id",
    ):
        setattr(model, field, getattr(payload, field))
    model.allowlist.clear()
    model.allowlist.extend(
        PlatformModelTenant(tenant_id=tenant.id) for tenant in tenant_rows
    )

@router.get("/platform/models", response_model=PlatformModelListResponse)
def list_platform_models(
    search: str | None = Query(default=None, max_length=120),
    status_filter: str | None = Query(
        default=None, alias="status", pattern="^(healthy|canary|disabled)$"
    ),
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PlatformModelListResponse:
    statement = select(PlatformModel)
    if search and search.strip():
        statement = statement.where(
            or_(
                PlatformModel.display_name.ilike(f"%{search.strip()}%"),
                PlatformModel.provider_model_id.ilike(f"%{search.strip()}%"),
            )
        )
    if status_filter:
        statement = statement.where(PlatformModel.status == status_filter)
    models = db.scalars(
        statement.order_by(PlatformModel.updated_at.desc(), PlatformModel.display_name)
    )
    return PlatformModelListResponse(
        models=[_platform_model_response(model) for model in models]
    )


@router.post(
    "/platform/models",
    response_model=PlatformModelResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_platform_model(
    payload: PlatformModelWriteRequest,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> PlatformModelResponse:
    if db.scalar(
        select(PlatformModel.id).where(
            PlatformModel.display_name == payload.display_name
        )
    ):
        raise HTTPException(status_code=409, detail="模型显示名称已存在")
    model = PlatformModel()
    _write_platform_model(db, model, payload, create=True)
    db.add(model)
    db.add(
        AuditEvent(
            tenant_id=None,
            actor_user_id=user.id,
            action="model_catalog.created",
            details={
                "display_name": payload.display_name,
                "provider": payload.provider,
                "model_type": payload.model_type,
                "region": payload.region,
                "status": payload.status,
            },
        )
    )
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="模型显示名称已存在") from error
    db.refresh(model)
    sync_platform_model(db, model)
    return _platform_model_response(model)


@router.put("/platform/models/{model_id}", response_model=PlatformModelResponse)
def update_platform_model(
    model_id: UUID,
    payload: PlatformModelWriteRequest,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> PlatformModelResponse:
    model = db.get(PlatformModel, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="未找到模型")
    duplicate = db.scalar(
        select(PlatformModel.id).where(
            PlatformModel.display_name == payload.display_name,
            PlatformModel.id != model.id,
        )
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="模型显示名称已存在")
    previous_status = model.status
    _write_platform_model(db, model, payload, create=False)
    db.add(
        AuditEvent(
            tenant_id=None,
            actor_user_id=user.id,
            action=(
                "model_catalog.disabled"
                if payload.status == "disabled" and previous_status != "disabled"
                else "model_catalog.updated"
            ),
            details={
                "model_id": str(model.id),
                "display_name": payload.display_name,
                "status": payload.status,
            },
        )
    )
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="模型目录更新冲突") from error
    db.refresh(model)
    sync_platform_model(db, model)
    sync_environments_using_model(db, model.id)
    db.refresh(model)
    return _platform_model_response(model)


def _platform_model_or_404(db: Session, model_id: UUID) -> PlatformModel:
    model = db.get(PlatformModel, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="未找到模型")
    return model


@router.post("/platform/models/{model_id}/sync", response_model=PlatformModelResponse)
def resync_platform_model(
    model_id: UUID,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> PlatformModelResponse:
    model = _platform_model_or_404(db, model_id)
    sync_platform_model(db, model)
    return _platform_model_response(model)


@router.post("/platform/models/{model_id}/test", response_model=ModelTestResponse)
def test_platform_model(
    model_id: UUID,
    payload: ModelTestRequest,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ModelTestResponse:
    """Connectivity test through the gateway with the platform master key (PLT-09x)."""
    model = _platform_model_or_404(db, model_id)
    if model.gateway_sync_status != "synced":
        raise HTTPException(status_code=409, detail="模型尚未同步到 AI 网关，请先同步")
    result = run_model_test(
        api_key=settings.litellm_master_key, model=model_alias(model.id), payload=payload
    )
    db.add(
        AuditEvent(
            tenant_id=None,
            actor_user_id=user.id,
            action="model_catalog.tested",
            result="success" if result.ok else "failure",
            details={
                "model_id": str(model.id),
                "display_name": model.display_name,
                "access_only": str(payload.access_only),
                "latency_ms": str(result.latency_ms),
            },
        )
    )
    db.commit()
    return result
