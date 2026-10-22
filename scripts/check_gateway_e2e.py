"""End-to-end check: BeeWork API -> LiteLLM gateway -> tenant-scoped virtual key.

Runs inside the api container against the running stack:
    docker compose exec api python /app/scripts/check_gateway_e2e.py

It registers a throwaway catalog model with a dummy provider key and only runs
access checks (key validity + model allowlist via /v1/models), so no provider is
contacted and nothing is billed.
The model is disabled (and removed from the gateway) at the end.
"""

import os
import sys
import uuid

import httpx

API = os.environ.get("BEEWORK_API", "http://localhost:8000")
PLATFORM = ("platform@beework.dev", os.environ.get("BOOTSTRAP_PLATFORM_ADMIN_PASSWORD", "BeeWork-Platform-2026!"))
TENANT_ADMIN = ("admin@beework.dev", os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "Beework-Dev-2026!"))
ORIGIN = os.environ.get("APP_ORIGIN", "http://localhost:5173")


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
            "endpoint_url": "https://api.openai.com/v1",
            "api_key": "sk-dummy-not-a-real-key",
            "region": platform_tenant["region"],
        },
    )
    check("create catalog model", created.status_code == 201, created.text[:300])
    model = created.json()
    try:
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
        check("tenant environment synced (team + key)", saved.json()["gateway_sync_status"] == "synced", saved.json()["gateway_error"])

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
        check("disable model removes it from gateway", disabled.json()["gateway_sync_status"] == "synced", disabled.text[:300])
        after = tenant_admin.post(
            "/api/v1/tenant/model-environments/production/test",
            headers=headers,
            json={"model_id": model["id"], "access_only": True},
        ).json()
        check("tenant key loses access after model disabled", after["ok"] is False, after.get("error"))
    finally:
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
