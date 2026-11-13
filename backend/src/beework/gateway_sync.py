"""Keep the AI gateway (LiteLLM) in step with BeeWork's model configuration.

BeeWork's database stays the source of truth; the gateway is a projection of it.
Sync runs after the business transaction commits, records its outcome on the
row, and can be retried, so a gateway outage never blocks configuration.
"""

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework import model_catalog
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
from beework.tenant_lifecycle import NON_SERVING_STATUSES
from beework.usage import usage_paused
from beework.worker.queue import enqueue, job_handler

logger = logging.getLogger(__name__)

# Enough retries (with the queue's backoff capped at an hour) to outlast a long gateway outage.
ACCESS_JOB_ATTEMPTS = 30


# ---------------------------------------------------------------------------
# Requests: API handlers mark the row ``pending`` and queue the work (T-005); the Worker runs the sync.
# ---------------------------------------------------------------------------


def request_model_sync(db: Session, model: PlatformModel) -> None:
    """Queue a gateway sync for a catalog entry and return immediately with ``pending``."""
    if not gateway_configured():
        model.gateway_sync_status = "not_configured"
        model.gateway_error = None
        db.commit()
        return
    model.gateway_sync_status = "pending"
    model.gateway_error = None
    enqueue(db, "gateway.sync_model", {"model_id": str(model.id)}, idempotency_key=f"gateway.sync_model:{model.id}:{_stamp()}")
    db.commit()


def request_environment_sync(db: Session, config: TenantModelEnvironment) -> None:
    if not gateway_configured():
        config.gateway_sync_status = "not_configured"
        config.gateway_error = None
        db.commit()
        return
    config.gateway_sync_status = "pending"
    config.gateway_error = None
    enqueue(
        db, "gateway.sync_environment", {"environment_id": str(config.id)}, tenant_id=config.tenant_id,
        idempotency_key=f"gateway.sync_environment:{config.id}:{_stamp()}",
    )
    db.commit()


def _stamp() -> str:
    # One queued sync per row and moment; a request made while one is already queued simply queues another,
    # which is harmless because every sync converges on the database state when it runs.
    return datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")


def sync_platform_model(db: Session, model: PlatformModel) -> None:
    """Register an enabled catalog entry in the gateway, or remove a disabled one."""
    if not gateway_configured():
        model.gateway_sync_status = "not_configured"
        model.gateway_error = None
        db.commit()
        return
    alias = model_alias(model.id)
    try:
        with LiteLLMClient() as client:
            if model.status == "disabled":
                client.delete_model(model.id)
                client.clear_fallback(alias)
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
                # T-014: the catalog's degradation target is a router-level fallback in LiteLLM,
                # managed over /fallback so the gateway (not BeeWork) retries a failing call.
                fallback = model_catalog.gateway_fallback_alias(db, model)
                if fallback:
                    client.set_fallback(alias, [fallback])
                else:
                    client.clear_fallback(alias)
    except (GatewayError, ValueError) as exc:
        model.gateway_sync_status = "failed"
        model.gateway_error = str(exc)[:500]
    else:
        model.gateway_sync_status = "synced"
        model.gateway_error = None
        model.gateway_synced_at = datetime.now(UTC)
    db.commit()


def _tenant_may_use_alias(db: Session, tenant_id: UUID, alias: str) -> bool:
    target_id = model_catalog.model_id_from_alias(alias)
    target = db.get(PlatformModel, target_id) if target_id else None
    return target is not None and model_catalog.tenant_may_use(db, tenant_id, target)


def sync_tenant_environment(db: Session, tenant: Tenant, config: TenantModelEnvironment) -> None:
    """Restrict the environment's gateway team to the models it has enabled — plus, T-014, the
    alias each of them resolves to at run time.

    A disabled entry is replaced outright by its degradation target (its deployment is gone), so
    that target must be allowed by the team or the tenant key gets rejected on the direct request
    (review fix 1); the same goes for a serving entry's request-level fallback."""
    if not gateway_configured():
        config.gateway_sync_status = "not_configured"
        config.gateway_error = None
        db.commit()
        return
    aliases: list[str] = []
    seen: set[str] = set()
    for model_id in db.scalars(
        select(TenantModelSelection.model_id).where(TenantModelSelection.environment_id == config.id)
    ):
        model = db.get(PlatformModel, model_id)
        if model is None:
            continue
        resolved = model_catalog.resolve_runtime_model(db, model_alias(model_id))
        if model.status == "disabled" and resolved.alias == model_alias(model_id):
            continue  # disabled without a serving target: nothing to allow
        own = model_alias(model_id)
        for candidate in (resolved.alias, *resolved.fallbacks):
            if candidate != own and not _tenant_may_use_alias(db, tenant.id, candidate):
                continue  # a degradation target the tenant is not entitled to is never put in its scope
            if candidate not in seen:
                seen.add(candidate)
                aliases.append(candidate)
    alias = team_alias(tenant.id, config.environment)
    try:
        with LiteLLMClient() as client:
            config.gateway_team_id = client.ensure_team(
                team_id=config.gateway_team_id, alias=alias, models=aliases
            )
            if not config.gateway_key_encrypted:
                new_key = client.generate_key(team_id=config.gateway_team_id, alias=alias)
                config.gateway_key_encrypted = encrypt_model_api_key(new_key)
                config.gateway_key_blocked = False
                if should_block(tenant, config.environment, usage_is_paused=usage_paused(db, tenant.id)):
                    # a key minted for a suspended or budget-paused workspace starts blocked
                    client.block_key(new_key)
                    config.gateway_key_blocked = True
    except GatewayError as exc:
        config.gateway_sync_status = "failed"
        config.gateway_error = str(exc)[:500]
    else:
        config.gateway_sync_status = "synced"
        config.gateway_error = None
        config.gateway_synced_at = datetime.now(UTC)
    db.commit()


def should_block(tenant: Tenant, environment: str, *, usage_is_paused: bool) -> bool:
    """A gateway key is blocked while the workspace is not serving, or while the budget pause (RUL-022) covers a
    non-production environment."""
    return tenant.status in NON_SERVING_STATUSES or (usage_is_paused and environment != "production")


def environment_ready(config: TenantModelEnvironment | None) -> bool:
    """Can runs and checks use this environment's gateway key?

    A key that already exists keeps working while a re-sync is queued (T-005 made syncing asynchronous, so a
    model-list save must not take a working environment offline until the Worker catches up); only an environment
    that never synced (no key yet) or whose last sync failed is unusable.
    """
    return bool(config and config.gateway_key_encrypted and config.gateway_sync_status in ("synced", "pending"))


def tenant_gateway_key(config: TenantModelEnvironment) -> str:
    if not environment_ready(config):
        raise GatewayError("该环境尚未同步到 AI 网关，请先保存配置或重试同步")
    return decrypt_model_api_key(config.gateway_key_encrypted)


def sync_environments_using_model(db: Session, model_id) -> None:
    """Queue a re-scope of every tenant environment that enabled this model (e.g. after it is disabled)."""
    configs = list(
        db.scalars(
            select(TenantModelEnvironment)
            .join(TenantModelSelection, TenantModelSelection.environment_id == TenantModelEnvironment.id)
            .where(TenantModelSelection.model_id == model_id)
        )
    )
    for config in configs:
        request_environment_sync(db, config)


def sync_models_using_fallback(db: Session, model_id: UUID) -> None:
    """A degradation target changed state: re-sync every model whose chain includes it (T-014).

    Transitive, not just direct referencers: with A→B→C, disabling C changes the first serving
    hop of B *and* can empty A's chain once B is gone too, so A's router fallback must be
    recomputed as well or it keeps pointing at a model the gateway no longer serves (review fix 4)."""
    for model in model_catalog.models_whose_chain_includes(db, model_id):
        request_model_sync(db, model)


def sync_environments_of_chain(db: Session, model_id: UUID) -> None:
    """Re-scope the tenant environments of this model and of every model degrading through it:
    their team allowlist is derived from the resolved aliases (T-014 review fix 1/4)."""
    for model in (db.get(PlatformModel, model_id), *model_catalog.models_whose_chain_includes(db, model_id)):
        if model is not None:
            sync_environments_using_model(db, model.id)


def request_model_deletion(db: Session, model_id: UUID, alias: str) -> None:
    """PLT-093: cleanup of the gateway deployment + router fallback after a catalog row is deleted.
    Queued with the business transaction so a gateway outage is retried by the Worker (T-005)."""
    if not gateway_configured():
        return
    enqueue(
        db, "gateway.delete_model", {"model_id": str(model_id), "alias": alias},
        idempotency_key=f"gateway.delete_model:{model_id}",
    )


# ---------------------------------------------------------------------------
# Worker jobs.
# ---------------------------------------------------------------------------


@job_handler("gateway.sync_model")
def sync_model_job(db: Session, payload: dict[str, Any]) -> None:
    model = db.get(PlatformModel, UUID(payload["model_id"]))
    if model is not None:
        sync_platform_model(db, model)


@job_handler("gateway.delete_model")
def delete_model_job(db: Session, payload: dict[str, Any]) -> None:
    """Remove the deployment and its router fallback; idempotent, so a retry after a partial failure is safe."""
    if not gateway_configured():
        return
    with LiteLLMClient() as client:
        client.delete_model(UUID(payload["model_id"]))
        client.clear_fallback(payload["alias"])


@job_handler("gateway.sync_environment")
def sync_environment_job(db: Session, payload: dict[str, Any]) -> None:
    config = db.get(TenantModelEnvironment, UUID(payload["environment_id"]))
    if config is None:
        return
    tenant = db.get(Tenant, config.tenant_id)
    if tenant is not None:
        sync_tenant_environment(db, tenant, config)


def apply_tenant_access(db: Session, tenant: Tenant) -> None:
    """Block the tenant's gateway keys while its status forbids model use (or, for non-production environments, while
    the budget pause applies), unblock them when that no longer holds.

    Raises ``GatewayError`` when the gateway cannot be reached so the job is retried: a suspended workspace
    must not keep a working key.
    """
    paused = usage_paused(db, tenant.id)
    configs = list(
        db.scalars(
            select(TenantModelEnvironment).where(
                TenantModelEnvironment.tenant_id == tenant.id,
                TenantModelEnvironment.gateway_key_encrypted.is_not(None),
            )
        )
    )
    pending = [
        config for config in configs
        if config.gateway_key_blocked != should_block(tenant, config.environment, usage_is_paused=paused)
    ]
    if not pending:
        return
    if not gateway_configured():
        raise GatewayError("AI 网关（LiteLLM）未配置，无法更新租户的模型访问权限")
    with LiteLLMClient() as client:
        for config in pending:
            key = decrypt_model_api_key(config.gateway_key_encrypted)
            blocked = should_block(tenant, config.environment, usage_is_paused=paused)
            client.block_key(key) if blocked else client.unblock_key(key)
            config.gateway_key_blocked = blocked
            db.commit()  # one key at a time: a failure later keeps the keys already handled


def reconcile_tenant_access(db: Session) -> int:
    """Safety net: queue an access sync for every tenant whose gateway keys disagree with its status.

    Blocking is normally queued with the status change, but a long gateway outage can exhaust a job's retries;
    this hourly pass makes sure a suspended workspace never keeps a working key indefinitely.
    """
    hour = datetime.now(UTC).strftime("%Y%m%d%H")
    queued = 0
    rows = db.execute(
        select(Tenant, TenantModelEnvironment)
        .join(TenantModelEnvironment, TenantModelEnvironment.tenant_id == Tenant.id)
        .where(TenantModelEnvironment.gateway_key_encrypted.is_not(None))
    )
    seen: set[UUID] = set()
    for tenant, config in rows:
        if tenant.id not in seen and config.gateway_key_blocked != should_block(
            tenant, config.environment, usage_is_paused=usage_paused(db, tenant.id)
        ):
            seen.add(tenant.id)
            enqueue(
                db, "gateway.sync_tenant_access", {"tenant_id": str(tenant.id)}, tenant_id=tenant.id,
                idempotency_key=f"gateway.access:{tenant.id}:reconcile:{hour}", max_attempts=ACCESS_JOB_ATTEMPTS,
            )
            queued += 1
    return queued


@job_handler("gateway.reconcile_access")
def reconcile_access_job(db: Session, _payload: dict[str, Any]) -> None:
    reconcile_tenant_access(db)


@job_handler("gateway.sync_tenant_access")
def sync_tenant_access_job(db: Session, payload: dict[str, Any]) -> None:
    tenant = db.get(Tenant, UUID(payload["tenant_id"]))
    if tenant is not None:
        apply_tenant_access(db, tenant)
