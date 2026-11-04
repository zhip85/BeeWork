"""End-to-end check: BeeWork API -> LiteLLM gateway -> tenant-scoped virtual key.

Runs inside the api container against the running stack:
    docker compose exec api python /app/scripts/check_gateway_e2e.py

It registers a throwaway catalog model with a dummy provider key and only runs
access checks (key validity + model allowlist via /v1/models), so no provider is
contacted and nothing is billed.
The model is disabled (and removed from the gateway) at the end.
Where the sandbox cannot resolve api.openai.com, set E2E_ENDPOINT_URL to a public https address
(e.g. https://1.1.1.1/v1): the check never calls the provider.
"""

import os
import sys
import time
import uuid

import httpx

API = os.environ.get("BEEWORK_API", "http://localhost:8000")
PLATFORM = ("platform@beework.dev", os.environ.get("BOOTSTRAP_PLATFORM_ADMIN_PASSWORD", "BeeWork-Platform-2026!"))
TENANT_ADMIN = ("admin@beework.dev", os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "Beework-Dev-2026!"))
REVIEWER = ("reviewer@beework.dev", os.environ.get("BOOTSTRAP_PLATFORM_REVIEWER_PASSWORD", "BeeWork-Reviewer-2026!"))
ORIGIN = os.environ.get("APP_ORIGIN", "http://localhost:5173")
LITELLM = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000")
SETTLE_SECONDS = 90


def session(email: str, password: str) -> httpx.Client:
    client = httpx.Client(base_url=API, timeout=60, headers={"Origin": ORIGIN})
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    response.raise_for_status()
    client.headers["X-CSRF-Token"] = client.cookies["beework_csrf"]
    return client


def check(label: str, condition: bool, detail: object = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {label}{f' — {detail}' if detail else ''}")
    if not condition:
        sys.exit(1)


def wait_for(label: str, read, done) -> object:
    """Gateway syncs and key blocks run in the Worker (T-005), so every effect is awaited, never assumed."""
    deadline = time.time() + SETTLE_SECONDS
    value = read()
    while not done(value) and time.time() < deadline:
        time.sleep(1.5)
        value = read()
    check(label, done(value), value if not isinstance(value, dict) else {k: value.get(k) for k in ("gateway_sync_status", "gateway_error")})
    return value


def tenant_key_status(tenant_id: str) -> int:
    """HTTP status LiteLLM gives the tenant's own virtual key (read from the database, as the runtime does)."""
    from sqlalchemy import select

    from beework.db import SessionLocal
    from beework.gateway_sync import tenant_gateway_key
    from beework.models import TenantModelEnvironment

    with SessionLocal() as db:
        config = db.scalar(select(TenantModelEnvironment).where(
            TenantModelEnvironment.tenant_id == uuid.UUID(tenant_id), TenantModelEnvironment.environment == "production"))
        key = tenant_gateway_key(config)
    return httpx.get(f"{LITELLM}/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=30).status_code


def main() -> None:
    platform = session(*PLATFORM)
    integrations = {item["key"]: item for item in platform.get("/api/v1/platform/integrations").json()}
    check("LiteLLM reachable", integrations["litellm"]["reachable"] is True, integrations["litellm"])

    tenant_admin = session(*TENANT_ADMIN)
    me = tenant_admin.get("/api/v1/auth/me").json()
    tenant = me["memberships"][0]
    headers = {"X-Tenant-ID": tenant["id"]}
    environment = tenant_admin.get("/api/v1/tenant/model-environments/production", headers=headers).json()

    platform_tenant = next(
        item for item in platform.get("/api/v1/platform/tenants").json() if item["id"] == tenant["id"]
    )
    name = f"E2E Mock Chat {uuid.uuid4().hex[:6]}"
    created = platform.post(
        "/api/v1/platform/models",
        json={
            "display_name": name,
            "provider": "openai",
            "model_type": "chat",
            "availability": "all_plans",
            "input_price_per_million": "0",
            "output_price_per_million": "0",
            "input_points_per_thousand": "0",
            "output_points_per_thousand": "0",
            "status": "healthy",
            "provider_model_id": "gpt-4o-mini",
            "endpoint_url": os.environ.get("E2E_ENDPOINT_URL", "https://api.openai.com/v1"),
            "api_key": "sk-dummy-not-a-real-key",
            "region": platform_tenant["region"],
        },
    )
    check("create catalog model", created.status_code == 201, created.text[:300])
    model = created.json()
    check("catalog sync is queued, not run inside the request", model["gateway_sync_status"] == "pending", model["gateway_sync_status"])
    suspended = False
    try:
        model = wait_for(
            "catalog model synced to LiteLLM by the Worker",
            lambda: next(m for m in platform.get("/api/v1/platform/models").json()["models"] if m["id"] == model["id"]),
            lambda m: m["gateway_sync_status"] != "pending",
        )
        check("catalog model synced to LiteLLM", model["gateway_sync_status"] == "synced", model["gateway_error"])
        platform_test = platform.post(f"/api/v1/platform/models/{model['id']}/test", json={"access_only": True}).json()
        check("platform key can access model", platform_test["ok"], platform_test)

        enabled = environment["enabled_model_ids"] + [model["id"]]
        saved = tenant_admin.put(
            "/api/v1/tenant/model-environments/production",
            headers=headers,
            json={
                "expected_version": environment["version"],
                "enabled_model_ids": enabled,
                "default_model_ids": environment["default_model_ids"],
            },
        )
        check("tenant enables model", saved.status_code == 200, saved.text[:300])
        check("environment save returns pending", saved.json()["gateway_sync_status"] == "pending", saved.json()["gateway_sync_status"])
        synced = wait_for(
            "tenant environment synced (team + key)",
            lambda: tenant_admin.get("/api/v1/tenant/model-environments/production", headers=headers).json(),
            lambda e: e["gateway_sync_status"] != "pending",
        )
        check("tenant environment synced", synced["gateway_sync_status"] == "synced", synced["gateway_error"])

        tenant_test = tenant_admin.post(
            "/api/v1/tenant/model-environments/production/test",
            headers=headers,
            json={"model_id": model["id"], "access_only": True},
        ).json()
        check("tenant virtual key can access enabled model", tenant_test["ok"], tenant_test)

        # Disable the model: the gateway deployment is removed, so the tenant key can no longer use it.
        payload = {
            key: model[key]
            for key in (
                "display_name", "provider", "model_type", "availability",
                "input_price_per_million", "output_price_per_million",
                "input_points_per_thousand", "output_points_per_thousand",
                "provider_model_id", "endpoint_url", "api_version", "region",
                "context_window", "max_output_tokens", "default_temperature", "rpm_limit",
                "fallback_model_id", "allowlist_tenant_ids",
            )
        }
        disabled = platform.put(f"/api/v1/platform/models/{model['id']}", json={**payload, "status": "disabled"})
        check("disable model accepted", disabled.status_code == 200, disabled.text[:300])
        wait_for(
            "disable model removed from gateway",
            lambda: next(m for m in platform.get("/api/v1/platform/models").json()["models"] if m["id"] == model["id"]),
            lambda m: m["gateway_sync_status"] == "synced",
        )
        wait_for(
            "tenant environment re-scoped",
            lambda: tenant_admin.get("/api/v1/tenant/model-environments/production", headers=headers).json(),
            lambda e: e["gateway_sync_status"] == "synced",
        )
        after = tenant_admin.post(
            "/api/v1/tenant/model-environments/production/test",
            headers=headers,
            json={"model_id": model["id"], "access_only": True},
        ).json()
        check("tenant key loses access after model disabled", after["ok"] is False, after.get("error"))

        # A suspended workspace must not consume models: its virtual key is blocked in the gateway (T-005).
        check("tenant key works while the workspace is active", tenant_key_status(tenant["id"]) == 200)
        reviewer = session(*REVIEWER)
        requested = platform.post(f"/api/v1/platform/tenants/{tenant['id']}/lifecycle", json={"action": "suspend", "reason": "e2e check"})
        check("suspension needs a second reviewer", requested.status_code == 202, requested.text[:300])
        approved = reviewer.post(f"/api/v1/platform/operations/{requested.json()['operation']['id']}/approve", json={})
        check("second platform admin approves suspension", approved.status_code == 200, approved.text[:300])
        suspended = True
        wait_for("suspended tenant's virtual key is rejected by the gateway", lambda: tenant_key_status(tenant["id"]), lambda status: status in (401, 403))
        restored = platform.post(f"/api/v1/platform/tenants/{tenant['id']}/lifecycle", json={"action": "restore"})
        check("platform restores the workspace", restored.status_code == 200, restored.text[:300])
        suspended = False
        wait_for("restored tenant's virtual key works again", lambda: tenant_key_status(tenant["id"]), lambda status: status == 200)
    finally:
        if suspended:  # never leave the dev workspace suspended if a check failed half-way
            platform.post(f"/api/v1/platform/tenants/{tenant['id']}/lifecycle", json={"action": "restore"})
        # Restore the tenant environment to what it was before the check.
        current = tenant_admin.get("/api/v1/tenant/model-environments/production", headers=headers).json()
        tenant_admin.put(
            "/api/v1/tenant/model-environments/production",
            headers=headers,
            json={
                "expected_version": current["version"],
                "enabled_model_ids": [i for i in current["enabled_model_ids"] if i != model["id"]],
                "default_model_ids": {
                    key: (None if value == model["id"] else value)
                    for key, value in current["default_model_ids"].items()
                },
            },
        )
    print("Gateway end-to-end check passed.")


if __name__ == "__main__":
    main()
