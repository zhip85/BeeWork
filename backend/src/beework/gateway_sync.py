"""Keep the AI gateway (LiteLLM) in step with BeeWork's model configuration.

BeeWork's database stays the source of truth; the gateway is a projection of it.
Sync runs after the business transaction commits, records its outcome on the
row, and can be retried, so a gateway outage never blocks configuration.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.integrations.litellm import (
    GatewayError,
    LiteLLMClient,
    gateway_configured,
    model_alias,
    team_alias,
)
from beework.model_secrets import decrypt_model_api_key, encrypt_model_api_key
from beework.models import (
    PlatformModel,
    Tenant,
    TenantModelEnvironment,
    TenantModelSelection,
)


def sync_platform_model(db: Session, model: PlatformModel) -> None:
    """Register an enabled catalog entry in the gateway, or remove a disabled one."""
    if not gateway_configured():
        model.gateway_sync_status = "not_configured"
        model.gateway_error = None
        db.commit()
        return
    try:
        with LiteLLMClient() as client:
            if model.status == "disabled":
                client.delete_model(model.id)
            else:
                client.upsert_model(
                    model_id=model.id,
                    provider=model.provider,
                    provider_model_id=model.provider_model_id,
                    endpoint_url=model.endpoint_url,
                    api_key=decrypt_model_api_key(model.encrypted_api_key),
                    api_version=model.api_version,
                    rpm_limit=model.rpm_limit,
                    model_type=model.model_type,
                )
    except (GatewayError, ValueError) as exc:
        model.gateway_sync_status = "failed"
        model.gateway_error = str(exc)[:500]
    else:
        model.gateway_sync_status = "synced"
        model.gateway_error = None
        model.gateway_synced_at = datetime.now(UTC)
    db.commit()


def sync_tenant_environment(db: Session, tenant: Tenant, config: TenantModelEnvironment) -> None:
    """Restrict the environment's gateway team to the models it has enabled."""
    if not gateway_configured():
        config.gateway_sync_status = "not_configured"
        config.gateway_error = None
        db.commit()
        return
    aliases = [
        model_alias(model_id)
        for model_id in db.scalars(
            select(TenantModelSelection.model_id)
            .join(PlatformModel, PlatformModel.id == TenantModelSelection.model_id)
            .where(
                TenantModelSelection.environment_id == config.id,
                PlatformModel.status != "disabled",
            )
        )
    ]
    alias = team_alias(tenant.id, config.environment)
    try:
        with LiteLLMClient() as client:
            config.gateway_team_id = client.ensure_team(
                team_id=config.gateway_team_id, alias=alias, models=aliases
            )
            if not config.gateway_key_encrypted:
                config.gateway_key_encrypted = encrypt_model_api_key(
                    client.generate_key(team_id=config.gateway_team_id, alias=alias)
                )
    except GatewayError as exc:
        config.gateway_sync_status = "failed"
        config.gateway_error = str(exc)[:500]
    else:
        config.gateway_sync_status = "synced"
        config.gateway_error = None
        config.gateway_synced_at = datetime.now(UTC)
    db.commit()


def tenant_gateway_key(config: TenantModelEnvironment) -> str:
    if config.gateway_sync_status != "synced" or not config.gateway_key_encrypted:
        raise GatewayError("该环境尚未同步到 AI 网关，请先保存配置或重试同步")
    return decrypt_model_api_key(config.gateway_key_encrypted)


def sync_environments_using_model(db: Session, model_id) -> None:
    """Re-scope every tenant environment that enabled this model (e.g. after it is disabled)."""
    configs = list(
        db.scalars(
            select(TenantModelEnvironment)
            .join(TenantModelSelection, TenantModelSelection.environment_id == TenantModelEnvironment.id)
            .where(TenantModelSelection.model_id == model_id)
        )
    )
    for config in configs:
        tenant = db.get(Tenant, config.tenant_id)
        if tenant is not None:
            sync_tenant_environment(db, tenant, config)
