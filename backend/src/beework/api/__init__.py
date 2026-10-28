from fastapi import APIRouter

from beework.api import (
    assistants,
    audit,
    auth,
    integrations,
    jobs,
    members,
    organizations,
    platform_models,
    platform_tenants,
    roles,
    subscription,
    tenant_models,
)

router = APIRouter(prefix="/api/v1")
for module in (
    assistants,
    auth,
    audit,
    platform_tenants,
    platform_models,
    tenant_models,
    members,
    roles,
    organizations,
    integrations,
    jobs,
    subscription,
):
    router.include_router(module.router)
