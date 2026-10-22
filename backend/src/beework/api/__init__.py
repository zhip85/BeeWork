from fastapi import APIRouter

from beework.api import (
    audit,
    auth,
    integrations,
    members,
    organizations,
    platform_models,
    platform_tenants,
    roles,
    tenant_models,
)

router = APIRouter(prefix="/api/v1")
for module in (
    auth,
    audit,
    platform_tenants,
    platform_models,
    tenant_models,
    members,
    roles,
    organizations,
    integrations,
):
    router.include_router(module.router)
