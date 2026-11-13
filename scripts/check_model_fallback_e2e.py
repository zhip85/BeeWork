"""T-014 end-to-end check: a failing primary model is served by its catalog degradation model.

Runs inside the api container against the running stack:
    docker compose exec api python /app/scripts/check_model_fallback_e2e.py

The check creates two throwaway catalog entries, points the primary deployment at an
unreachable address and the degradation deployment at a LiteLLM ``mock_response``,
so no provider key is needed. It verifies the router-level fallback synced by the
Worker, the runtime's request-level fallback, the runtime resolution after the
primary is disabled, and — with a tenant virtual key scoped to the resolved
aliases, like BeeWork syncs them — that the degradation alias is directly callable
while the team allowlist still holds. Every temporary object is removed in ``finally``.
"""

import os
import sys
import time
import uuid

import httpx

API = os.environ.get("BEEWORK_API", "http://localhost:8000")
PLATFORM = ("platform@beework.dev", os.environ.get("BOOTSTRAP_PLATFORM_ADMIN_PASSWORD", "BeeWork-Platform-2026!"))
ORIGIN = os.environ.get("APP_ORIGIN", "http://localhost:5173")
LITELLM = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000")
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
# E2E_GATEWAY_ONLY=1 skips the BeeWork API / Worker path (useful when the api service is not up),
# but still exercises the real LiteLLM instance and the runtime's request-level fallback.
GATEWAY_ONLY = os.environ.get("E2E_GATEWAY_ONLY") == "1"
SETTLE_SECONDS = 90
MOCK_ANSWER = "T014_E2E_FALLBACK_OK"
WRITE_FIELDS = (
    "display_name", "provider", "model_type", "availability",
    "input_price_per_million", "output_price_per_million",
    "input_points_per_thousand", "output_points_per_thousand",
    "provider_model_id", "endpoint_url", "api_version", "region",
    "context_window", "max_output_tokens", "default_temperature", "rpm_limit",
    "fallback_model_id", "allowlist_tenant_ids", "status",
)


def session() -> httpx.Client:
    client = httpx.Client(base_url=API, timeout=60, headers={"Origin": ORIGIN})
    response = client.post("/api/v1/auth/login", json={"email": PLATFORM[0], "password": PLATFORM[1]})
    response.raise_for_status()
    client.headers["X-CSRF-Token"] = client.cookies["beework_csrf"]
    return client


def check(label: str, condition: bool, detail: object = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {label}{f' — {detail}' if detail else ''}", flush=True)
    if not condition:
        sys.exit(1)


def wait_for(label: str, read, done) -> object:
    deadline = time.time() + SETTLE_SECONDS
    value = read()
    while not done(value) and time.time() < deadline:
        time.sleep(1.5)
        value = read()
    check(label, done(value), value)
    return value


def catalog_payload(model: dict, **overrides: object) -> dict:
    return {**{key: model.get(key) for key in WRITE_FIELDS}, **overrides}


def create_model(platform: httpx.Client, name: str, region: str) -> dict:
    response = platform.post("/api/v1/platform/models", json={
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
        "region": region,
    })
    check(f"create {name}", response.status_code == 201, response.text[:300])
    return response.json()


def replace_deployment(gateway: httpx.Client, model_id: str, alias: str, params: dict) -> None:
    gateway.post("/model/delete", json={"id": model_id})
    created = gateway.post("/model/new", json={
        "model_name": alias,
        "litellm_params": {"model": "openai/gpt-4o-mini", "api_key": "sk-dummy", **params},
        "model_info": {"id": model_id, "mode": "chat"},
    })
    check(f"deployment {alias} replaced", created.status_code == 200, created.text[:300])


def chat(gateway: httpx.Client, model: str) -> str:
    response = gateway.post("/chat/completions", json={
        "model": model, "messages": [{"role": "user", "content": "hello"}], "max_tokens": 32,
    })
    check(f"chat via {model}", response.status_code == 200, response.text[:300])
    return str((response.json().get("choices") or [{}])[0].get("message", {}).get("content") or "")


def main_gateway_only() -> None:
    """Real LiteLLM, no BeeWork API: router fallback, the runtime's request-level fallback, and the
    tenant virtual key path (review fix: the master key alone bypasses team allowlists, so the old
    script could not catch a degradation target the tenant key is not allowed to call)."""
    check("LiteLLM master key configured", bool(MASTER_KEY))
    gateway = httpx.Client(base_url=LITELLM, headers={"Authorization": f"Bearer {MASTER_KEY}"}, timeout=90)
    primary_id, fallback_id, outsider_id = (str(uuid.uuid4()) for _ in range(3))
    primary_alias, fallback_alias, outsider_alias = f"bw-{primary_id}", f"bw-{fallback_id}", f"bw-{outsider_id}"
    team_id = None
    try:
        replace_deployment(gateway, primary_id, primary_alias, {"api_base": "http://127.0.0.1:9/v1"})
        replace_deployment(gateway, fallback_id, fallback_alias, {"mock_response": MOCK_ANSWER})
        replace_deployment(gateway, outsider_id, outsider_alias, {"mock_response": "SHOULD_NOT_REACH"})
        gateway.post("/fallback", json={
            "model": primary_alias, "fallback_models": [fallback_alias], "fallback_type": "general"
        })
        check("router-level fallback answers a failing primary", MOCK_ANSWER in chat(gateway, primary_alias))

        # The allowlist BeeWork now syncs for a tenant environment (T-014 review fix 1): the enabled
        # model plus the alias the catalog resolves it to — here the degradation target. The primary
        # stays in the list too, matching a serving model with a request-level fallback.
        team = gateway.post("/team/new", json={
            "team_alias": f"t014-tenant-{primary_id[:8]}", "models": [primary_alias, fallback_alias],
        })
        check("tenant team scoped to the resolved aliases", team.status_code == 200, team.text[:300])
        team_id = team.json()["team_id"]
        key = gateway.post("/key/generate", json={"team_id": team_id, "key_alias": "t014-tenant-key"})
        check("tenant virtual key minted", key.status_code == 200, key.text[:300])
        tenant = httpx.Client(base_url=LITELLM, headers={"Authorization": f"Bearer {key.json()['key']}"}, timeout=90)
        check("tenant key reaches the degradation alias directly", MOCK_ANSWER in chat(tenant, fallback_alias))
        outsider = tenant.post("/chat/completions", json={
            "model": outsider_alias, "messages": [{"role": "user", "content": "hello"}], "max_tokens": 32,
        })
        check("tenant key is still scoped by the allowlist", outsider.status_code in (400, 401, 403),
              outsider.text[:200])
        tenant.close()

        gateway.delete(f"/fallback/{primary_alias}?fallback_type=general")
        from beework.runtime.dsl import AssistantDSLv1
        from beework.runtime.llm import build_chat_model

        dsl = AssistantDSLv1(prompt="answer", model_alias=primary_alias)
        llm = build_chat_model(dsl, api_key=key.json()["key"], metadata={}, fallbacks=[fallback_alias])
        answer = llm.invoke([("human", "hello")])
        check("runtime request-level fallback answers a failing primary", MOCK_ANSWER in str(answer.content),
              str(answer.content)[:200])
        print("Model catalog gateway degradation check passed (E2E_GATEWAY_ONLY=1).", flush=True)
    finally:
        gateway.delete(f"/fallback/{primary_alias}?fallback_type=general")
        if team_id:
            gateway.post("/team/delete", json={"team_ids": [team_id]})
        for deployment_id in (primary_id, fallback_id, outsider_id):
            gateway.post("/model/delete", json={"id": deployment_id})
        gateway.close()


def main() -> None:
    if GATEWAY_ONLY:
        return main_gateway_only()
    check("LiteLLM master key configured", bool(MASTER_KEY))
    platform = session()
    gateway = httpx.Client(base_url=LITELLM, headers={"Authorization": f"Bearer {MASTER_KEY}"}, timeout=90)
    region = platform.get("/api/v1/platform/tenants").json()[0]["region"]
    suffix = uuid.uuid4().hex[:6]
    primary = create_model(platform, f"T014 E2E Primary {suffix}", region)
    fallback = create_model(platform, f"T014 E2E Fallback {suffix}", region)
    primary_alias, fallback_alias = f"bw-{primary['id']}", f"bw-{fallback['id']}"

    def read_model(model_id: str) -> dict:
        return next(m for m in platform.get("/api/v1/platform/models").json()["models"] if m["id"] == model_id)

    try:
        for model in (primary, fallback):
            wait_for(f"{model['display_name']} synced", lambda m=model: read_model(m["id"]),
                     lambda value: value["gateway_sync_status"] != "pending")
        saved = platform.put(f"/api/v1/platform/models/{primary['id']}", json=catalog_payload(
            primary, fallback_model_id=fallback["id"]
        ))
        check("degradation model saved", saved.status_code == 200, saved.text[:300])
        primary = saved.json()
        wait_for("primary re-synced with fallback", lambda: read_model(primary["id"]),
                 lambda value: value["gateway_sync_status"] == "synced")
        setting = gateway.get(f"/fallback/{primary_alias}?fallback_type=general")
        check("Worker projected the catalog fallback into LiteLLM",
              setting.status_code == 200 and fallback_alias in setting.json().get("fallback_models", []),
              setting.text[:300])

        replace_deployment(gateway, primary["id"], primary_alias, {"api_base": "http://127.0.0.1:9/v1"})
        replace_deployment(gateway, fallback["id"], fallback_alias, {"mock_response": MOCK_ANSWER})
        check("router-level fallback answers a failing primary",
              MOCK_ANSWER in chat(gateway, primary_alias))

        # The runtime also sends one request-level fallback, so a gateway restart that lost the
        # router setting cannot break degradation. Exercise build_chat_model itself.
        gateway.delete(f"/fallback/{primary_alias}?fallback_type=general")
        from beework.runtime.dsl import AssistantDSLv1
        from beework.runtime.llm import build_chat_model

        dsl = AssistantDSLv1(prompt="answer", model_alias=primary_alias)
        llm = build_chat_model(dsl, api_key=MASTER_KEY, metadata={}, fallbacks=[fallback_alias])
        answer = llm.invoke([("human", "hello")])
        check("runtime request-level fallback answers a failing primary", MOCK_ANSWER in str(answer.content),
              str(answer.content)[:200])

        # A disabled model has no deployment at all: BeeWork must resolve the frozen alias to the
        # degradation model before the request reaches the gateway.
        disabled = platform.put(f"/api/v1/platform/models/{primary['id']}", json=catalog_payload(
            primary, fallback_model_id=fallback["id"], status="disabled"
        ))
        check("primary disabled", disabled.status_code == 200, disabled.text[:300])
        wait_for("primary deployment removed", lambda: read_model(primary["id"]),
                 lambda value: value["gateway_sync_status"] == "synced")
        from beework import model_catalog
        from beework.db import SessionLocal

        with SessionLocal() as db:
            resolved = model_catalog.resolve_runtime_model(db, primary_alias)
        check("disabled primary resolves to the degradation alias", resolved.alias == fallback_alias, resolved)
        check("resolved alias still serves the request", MOCK_ANSWER in chat(gateway, resolved.alias))
        print("Model catalog degradation end-to-end check passed.", flush=True)
    finally:
        gateway.delete(f"/fallback/{primary_alias}?fallback_type=general")
        gateway.delete(f"/fallback/{fallback_alias}?fallback_type=general")
        # The primary points at the fallback, so it must be deleted first (PLT-093).
        for model in (primary, fallback):
            platform.delete(f"/api/v1/platform/models/{model['id']}")
        gateway.post("/model/delete", json={"id": primary["id"]})
        gateway.post("/model/delete", json={"id": fallback["id"]})
        gateway.close()


if __name__ == "__main__":
    main()
