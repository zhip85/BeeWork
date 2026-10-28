"""End-to-end check: assistant run -> Worker -> LangGraph -> LiteLLM -> SSE events -> Langfuse trace.

Runs inside the api container against the running stack (the worker service must be up):
    docker compose exec api python /app/scripts/check_runtime_e2e.py

No provider key is needed: a throwaway catalog model is registered, then its LiteLLM deployment is
replaced by one with ``mock_response`` so the gateway answers without contacting any provider.
That means this verifies BeeWork -> LiteLLM -> BeeWork streaming and tracing, NOT a real model.
Everything created here is removed again. Knowledge retrieval is the stub (no RAGFlow).
"""

import json
import os
import sys
import time
import uuid

import httpx

API = os.environ.get("BEEWORK_API", "http://localhost:8000")
LITELLM = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000")
LITELLM_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
PLATFORM = ("platform@beework.dev", os.environ.get("BOOTSTRAP_PLATFORM_ADMIN_PASSWORD", "BeeWork-Platform-2026!"))
TENANT_ADMIN = ("admin@beework.dev", os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "Beework-Dev-2026!"))
ORIGIN = os.environ.get("APP_ORIGIN", "http://localhost:5173")
MOCK_ANSWER = "这是来自 LiteLLM mock_response 的回答，用于验证运行时链路。"
PHONE = "13812345678"


def session(email: str, password: str) -> httpx.Client:
    client = httpx.Client(base_url=API, timeout=60, headers={"Origin": ORIGIN})
    client.post("/api/v1/auth/login", json={"email": email, "password": password}).raise_for_status()
    client.headers["X-CSRF-Token"] = client.cookies["beework_csrf"]
    return client


def check(label: str, condition: bool, detail: object = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {label}{f' — {detail}' if detail else ''}")
    if not condition:
        sys.exit(1)


def sse_events(client: httpx.Client, run_id: str, headers: dict[str, str]) -> list[dict]:
    events: list[dict] = []
    with client.stream("GET", f"/api/v1/runs/{run_id}/events", headers=headers, timeout=90) as response:
        check("event stream opens", response.status_code == 200, response.status_code)
        for line in response.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def langfuse_observations(trace_id: str) -> list[dict] | None:
    host, public, secret = (os.environ.get(k, "") for k in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"))
    if not (host and public and secret):
        return None
    deadline = time.time() + 60
    while time.time() < deadline:
        response = httpx.get(
            f"{host.rstrip('/')}/api/public/v2/observations",
            params={"traceId": trace_id, "limit": 50, "fields": "core,basic,io,metadata"},
            auth=(public, secret),
            timeout=30,
        )
        data = response.json().get("data", []) if response.status_code == 200 else []
        # Spans arrive in batches: wait for the root span and the last stage before judging.
        names = {o.get("name") for o in data}
        if {"assistant.run", "oc.egress"} <= names:
            return data
        if time.time() > deadline - 3:
            return data
        time.sleep(3)
    return []


def main() -> None:
    platform = session(*PLATFORM)
    tenant_admin = session(*TENANT_ADMIN)
    tenant = tenant_admin.get("/api/v1/auth/me").json()["memberships"][0]
    headers = {"X-Tenant-ID": tenant["id"]}
    environment = tenant_admin.get("/api/v1/tenant/model-environments/production", headers=headers).json()
    region = next(t for t in platform.get("/api/v1/platform/tenants").json() if t["id"] == tenant["id"])["region"]

    created = platform.post(
        "/api/v1/platform/models",
        json={
            "display_name": f"Runtime E2E {uuid.uuid4().hex[:6]}", "provider": "openai", "model_type": "chat",
            "availability": "all_plans", "input_price_per_million": "0", "output_price_per_million": "0",
            "input_points_per_thousand": "0", "output_points_per_thousand": "0", "status": "healthy",
            "provider_model_id": "gpt-4o-mini", "endpoint_url": "https://api.openai.com/v1",
            "api_key": "sk-dummy-not-a-real-key", "region": region,
        },
    )
    check("create catalog model", created.status_code == 201, created.text[:200])
    model = created.json()
    assistant_id = run_id = None
    original_env = environment
    try:
        saved = tenant_admin.put(
            "/api/v1/tenant/model-environments/production",
            headers=headers,
            json={
                "expected_version": environment["version"],
                "enabled_model_ids": environment["enabled_model_ids"] + [model["id"]],
                "default_model_ids": {**environment["default_model_ids"], "chat": model["id"]},
            },
        )
        check("tenant enables model + environment synced", saved.status_code == 200 and saved.json()["gateway_sync_status"] == "synced", saved.text[:200])

        # Swap the deployment for one that answers from mock_response (same alias, so the team key still works).
        gateway = httpx.Client(base_url=LITELLM, headers={"Authorization": f"Bearer {LITELLM_KEY}"}, timeout=30)
        gateway.post("/model/delete", json={"id": model["id"]}).raise_for_status()
        mock = gateway.post(
            "/model/new",
            json={
                "model_name": f"bw-{model['id']}",
                "litellm_params": {"model": "openai/gpt-4o-mini", "api_key": "sk-dummy", "mock_response": MOCK_ANSWER},
                "model_info": {"id": model["id"], "mode": "chat"},
            },
        )
        check("gateway deployment replaced by mock_response", mock.status_code == 200, mock.text[:200])

        assistant = tenant_admin.post(
            "/api/v1/assistants", headers=headers,
            json={"name": f"Runtime E2E {uuid.uuid4().hex[:6]}", "description": "e2e", "config": {"prompt": "You are a test assistant."}},
        )
        check("create assistant", assistant.status_code == 201, assistant.text[:200])
        assistant_id = assistant.json()["id"]

        started = tenant_admin.post(f"/api/v1/assistants/{assistant_id}/runs", headers=headers, json={"message": f"你好，我的手机号是 {PHONE}"})
        check("start run", started.status_code == 202, started.text[:200])
        run_id = started.json()["run_id"]
        conversation_id = started.json()["conversation_id"]

        events = sse_events(tenant_admin, run_id, headers)
        types = [e["type"] for e in events]
        check("events ordered queued -> running -> ... -> done", types[:2] == ["queued", "running"] and types[-1] == "done", types)
        streamed = "".join(e["data"]["text"] for e in events if e["type"] == "token")
        check("streamed tokens equal the mock answer", streamed == MOCK_ANSWER, streamed)
        check("done event carries stub retrieval mode", events[-1]["data"]["retrieval"]["mode"] == "stub")

        # Second turn in the same conversation, then the stored conversation (redacted by default).
        second = tenant_admin.post(
            f"/api/v1/assistants/{assistant_id}/runs", headers=headers,
            json={"message": "再问一次", "conversation_id": conversation_id},
        )
        check("follow-up turn in the same conversation", second.status_code == 202, second.text[:200])
        sse_events(tenant_admin, second.json()["run_id"], headers)
        detail = tenant_admin.get(f"/api/v1/conversations/{conversation_id}", headers=headers).json()
        check("conversation has 4 messages and 2 turns", len(detail["messages"]) == 4 and detail["turns"] == 2, detail["turns"])
        check("stored messages are PII-redacted", PHONE not in json.dumps(detail, ensure_ascii=False) and "[手机号]" in detail["messages"][0]["content"], detail["messages"][0]["content"])

        run = tenant_admin.get(f"/api/v1/runs/{run_id}", headers=headers).json()
        check("run recorded as succeeded", run["status"] == "succeeded" and run["output"]["text"] == MOCK_ANSWER, run)
        print(f"     tokens in/out: {run['tokens_in']}/{run['tokens_out']}, latency {run['latency_ms']} ms, pipeline overhead {run['pipeline_ms']} ms, trace_url: {run['trace_url']}")
        check("pipeline overhead recorded and well below total latency", run["pipeline_ms"] is not None and run["pipeline_ms"] < run["latency_ms"], run["stage_ms"])

        from langfuse import Langfuse

        observations = langfuse_observations(Langfuse.create_trace_id(seed=run_id))
        if observations is None:
            print("[SKIP] Langfuse not configured; trace not verified")
        else:
            names = sorted({o.get("name", "") for o in observations})
            check("Langfuse trace for the run exists", bool(observations), f"{len(observations)} observations: {names}")
            stage_spans = [f"oc.{stage}" for stage in ("ingress", "admission", "pii_input", "memory", "glossary_rewrite", "route", "execute", "components", "pii_output", "egress")]
            check("one Langfuse span per OC stage", all(name in names for name in stage_spans), [n for n in stage_spans if n not in names])
            metadata = json.dumps(observations, ensure_ascii=False)
            check("trace metadata carries tenant / assistant / run", all(v in metadata for v in (tenant["id"], assistant_id, run_id)))
            check("trace does not contain the raw phone number (redaction policy)", PHONE not in metadata)
    finally:
        if assistant_id:
            tenant_admin.delete(f"/api/v1/assistants/{assistant_id}", headers=headers)
        current = tenant_admin.get("/api/v1/tenant/model-environments/production", headers=headers).json()
        tenant_admin.put(
            "/api/v1/tenant/model-environments/production", headers=headers,
            json={
                "expected_version": current["version"],
                "enabled_model_ids": [i for i in current["enabled_model_ids"] if i != model["id"]],
                "default_model_ids": {k: (original_env["default_model_ids"].get(k) if v == model["id"] else v)
                                      for k, v in current["default_model_ids"].items()},
            },
        )
        payload = {k: model[k] for k in (
            "display_name", "provider", "model_type", "availability", "input_price_per_million",
            "output_price_per_million", "input_points_per_thousand", "output_points_per_thousand",
            "provider_model_id", "endpoint_url", "api_version", "region", "context_window",
            "max_output_tokens", "default_temperature", "rpm_limit", "fallback_model_id", "allowlist_tenant_ids")}
        platform.put(f"/api/v1/platform/models/{model['id']}", json={**payload, "status": "disabled"})
    print("Runtime end-to-end check passed (mock_response model, stub retrieval).")


if __name__ == "__main__":
    main()
