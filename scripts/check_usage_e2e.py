"""End-to-end check of usage metering (T-011): real run -> LiteLLM spend log -> points -> API.

Runs inside the api container against the running stack:
    docker compose exec api python /app/scripts/check_usage_e2e.py

Like check_runtime_e2e.py it registers a throwaway catalog model (priced 1 point / 1k input tokens and 2 points / 1k
output tokens) and swaps its gateway deployment for one answering from ``mock_response``, so no provider is
contacted. It then runs the assistant once, pulls LiteLLM's spend logs with the same code the Worker's ``usage.sync``
job runs, and checks attribution, the points formula, idempotency, the API and the CSV export. Everything it created
(except the usage rows, which are real metering of those calls) is removed or restored at the end.
"""

import csv
import io
import os
import sys
import time
import uuid
from decimal import Decimal

import httpx
from check_runtime_e2e import (
    LITELLM,
    LITELLM_KEY,
    PLATFORM,
    TENANT_ADMIN,
    _remove_test_data,
    check,
    session,
    sse_events,
    wait_for,
)

ANSWER = "用量计量端到端检查的回答。"


def main() -> None:
    from sqlalchemy import select

    from beework import usage
    from beework.db import SessionLocal
    from beework.models.usage import UsageRecord

    platform = session(*PLATFORM)
    tenant_admin = session(*TENANT_ADMIN)
    tenant = tenant_admin.get("/api/v1/auth/me").json()["memberships"][0]
    tenant_id = uuid.UUID(tenant["id"])
    headers = {"X-Tenant-ID": tenant["id"]}
    environment = tenant_admin.get("/api/v1/tenant/model-environments/production", headers=headers).json()
    region = next(t for t in platform.get("/api/v1/platform/tenants").json() if t["id"] == tenant["id"])["region"]

    created = platform.post(
        "/api/v1/platform/models",
        json={
            "display_name": f"Usage E2E {uuid.uuid4().hex[:6]}", "provider": "openai", "model_type": "chat",
            "availability": "all_plans", "input_price_per_million": "0", "output_price_per_million": "0",
            "input_points_per_thousand": "1", "output_points_per_thousand": "2", "status": "healthy",
            "provider_model_id": "gpt-4o-mini", "endpoint_url": os.environ.get("E2E_ENDPOINT_URL", "https://api.openai.com/v1"),
            "api_key": "sk-dummy-not-a-real-key", "region": region,
        },
    )
    check("create catalog model", created.status_code == 201, created.text[:200])
    model = created.json()
    assistant_id = publish_policy_restore = None
    before_summary = tenant_admin.get("/api/v1/tenant/usage", headers=headers).json()
    try:
        saved = tenant_admin.put(
            "/api/v1/tenant/model-environments/production", headers=headers,
            json={
                "expected_version": environment["version"],
                "enabled_model_ids": environment["enabled_model_ids"] + [model["id"]],
                "default_model_ids": {**environment["default_model_ids"], "chat": model["id"]},
            },
        )
        check("tenant enables model", saved.status_code == 200, saved.text[:200])
        wait_for(
            "catalog model synced",
            lambda: next(m for m in platform.get("/api/v1/platform/models").json()["models"] if m["id"] == model["id"]),
            lambda m: m["gateway_sync_status"] == "synced",
        )
        wait_for(
            "environment synced",
            lambda: tenant_admin.get("/api/v1/tenant/model-environments/production", headers=headers).json(),
            lambda e: e["gateway_sync_status"] == "synced",
        )
        gateway = httpx.Client(base_url=LITELLM, headers={"Authorization": f"Bearer {LITELLM_KEY}"}, timeout=30)
        gateway.post("/model/delete", json={"id": model["id"]}).raise_for_status()
        mock = gateway.post(
            "/model/new",
            json={
                "model_name": f"bw-{model['id']}",
                "litellm_params": {"model": "openai/gpt-4o-mini", "api_key": "sk-dummy", "mock_response": ANSWER},
                "model_info": {"id": model["id"], "mode": "chat"},
            },
        )
        check("gateway deployment replaced by mock_response", mock.status_code == 200, mock.text[:200])

        assistant = tenant_admin.post(
            "/api/v1/assistants", headers=headers,
            json={"name": f"Usage E2E {uuid.uuid4().hex[:6]}", "description": "e2e", "config": {"prompt": "You are a test assistant."}},
        )
        check("create assistant", assistant.status_code == 201, assistant.text[:200])
        assistant_id = assistant.json()["id"]
        started = tenant_admin.post(f"/api/v1/assistants/{assistant_id}/runs", headers=headers, json={"message": "用量检查：你好"})
        check("start run", started.status_code == 202, started.text[:200])
        events = sse_events(tenant_admin, started.json()["run_id"], headers)
        check("run finished", events[-1]["type"] == "done", [e["type"] for e in events])

        # LiteLLM writes spend logs in the background, so pull until the call shows up (the Worker does this every 5 minutes).
        def pull_and_read() -> UsageRecord | None:
            with SessionLocal() as db:
                usage.sync_usage(db)
                return db.scalar(select(UsageRecord).where(
                    UsageRecord.tenant_id == tenant_id, UsageRecord.assistant_id == uuid.UUID(assistant_id)))

        record = wait_for("spend log pulled and attributed to the assistant", pull_and_read, lambda r: r is not None, seconds=120)
        expected = Decimal(record.tokens_in) / 1000 * 1 + Decimal(record.tokens_out) / 1000 * 2
        check("tenant / environment / source attributed from the gateway log", (record.environment, record.source, record.requests) == ("production", "debug", 1),
              (record.environment, record.source, record.requests))
        check("model attributed", str(record.model_id) == model["id"])
        check("tokens come from the gateway", record.tokens_in > 0 and record.tokens_out > 0, (record.tokens_in, record.tokens_out))
        check("points = tokens/1000 x catalog points", Decimal(record.points) == expected, (record.points, expected))

        with SessionLocal() as db:
            again = usage.sync_usage(db)
            same = db.scalar(select(UsageRecord).where(UsageRecord.id == record.id))
            check("pulling the same window again counts nothing twice", again.counted == 0 and Decimal(same.points) == expected and same.requests == 1,
                  (again, same.points, same.requests))

        summary = tenant_admin.get("/api/v1/tenant/usage", headers=headers).json()
        grew = summary["points_used"] - before_summary["points_used"]
        check("API month total grew by the metered points", grew >= int(expected), (grew, expected))
        check("assistant appears in the breakdown", any(item["assistant_id"] == assistant_id for item in summary["by_assistant"])
              or any(item["other"] for item in summary["by_assistant"]), summary["by_assistant"])
        csv_text = tenant_admin.get("/api/v1/tenant/usage/export", headers=headers).content.decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(csv_text)))
        check("CSV export has the call", any(row["tokens_in"] == str(record.tokens_in) and row["environment"] == "production" for row in rows), len(rows))
    finally:
        if assistant_id:
            tenant_admin.post(f"/api/v1/assistants/{assistant_id}/disable", headers=headers)
            tenant_admin.delete(f"/api/v1/assistants/{assistant_id}", headers=headers)
            _remove_test_data(assistant_id)
        current = tenant_admin.get("/api/v1/tenant/model-environments/production", headers=headers).json()
        tenant_admin.put(
            "/api/v1/tenant/model-environments/production", headers=headers,
            json={
                "expected_version": current["version"],
                "enabled_model_ids": [i for i in current["enabled_model_ids"] if i != model["id"]],
                "default_model_ids": {k: (environment["default_model_ids"].get(k) if v == model["id"] else v)
                                      for k, v in current["default_model_ids"].items()},
            },
        )
        payload = {k: model[k] for k in (
            "display_name", "provider", "model_type", "availability", "input_price_per_million",
            "output_price_per_million", "input_points_per_thousand", "output_points_per_thousand",
            "provider_model_id", "endpoint_url", "api_version", "region", "context_window",
            "max_output_tokens", "default_temperature", "rpm_limit", "fallback_model_id", "allowlist_tenant_ids")}
        platform.put(f"/api/v1/platform/models/{model['id']}", json={**payload, "status": "disabled"})
    print("Usage metering end-to-end check passed (mock_response model).")


if __name__ == "__main__":
    sys.exit(main())
