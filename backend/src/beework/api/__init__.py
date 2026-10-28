from fastapi import APIRouter

from beework.api import (
    assistant_versions,
    assistants,
    audit,
    auth,
    compliance,
    conversations,
    integrations,
    jobs,
    members,
    organizations,
    platform_models,
    platform_tenants,
    roles,
    runs,
    subscription,
    tenant_models,
)

router = APIRouter(prefix="/api/v1")
for module in (
    assistants,
    assistant_versions,
    compliance,
    conversations,
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
    runs,
    subscription,
):
    router.include_router(module.router)
