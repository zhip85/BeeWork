"""End-to-end check of knowledge retrieval (T-033): upload -> index -> mount -> ask -> cited answer.

Run inside an api container whose worker shares the same database and knowledge engine settings:

    # engine: the FAKE engine of scripts/fake_ragflow.py (RAGFlow flavour; E2E_ENGINE=dify for Dify's API)
    docker compose exec api python /app/scripts/check_retrieval_e2e.py

What is real: the BeeWork API and Worker, the knowledge engine HTTP contract (against the fake), LangGraph, LiteLLM and
Langfuse. What is NOT: the engine itself (a fake with word-overlap "embeddings") and the model (LiteLLM
``mock_response``). It says nothing about retrieval quality in a real RAGFlow / Dify.
"""

import os
import sys
import uuid

import httpx
from check_runtime_e2e import (
    LITELLM,
    LITELLM_KEY,
    PLATFORM,
    TENANT_ADMIN,
    check,
    session,
    sse_events,
    wait_for,
)

ENGINE = os.environ.get("E2E_ENGINE", "ragflow")
MOCK_ANSWER = "根据知识库，购买后 7 天内可以申请无理由退货。"
DOCUMENT = ("# 退货政策\n\n" + "购买后 7 天内可申请无理由退货，商品需保持完好。\n" * 8).encode()
QUESTION = "购买后 7 天内可申请无理由退货吗"


def main() -> None:
    platform = session(*PLATFORM)
    admin = session(*TENANT_ADMIN)
    tenant = admin.get("/api/v1/auth/me").json()["memberships"][0]
    headers = {"X-Tenant-ID": tenant["id"]}
    environment = admin.get("/api/v1/tenant/model-environments/production", headers=headers).json()
    region = next(t for t in platform.get("/api/v1/platform/tenants").json() if t["id"] == tenant["id"])["region"]
    suffix = uuid.uuid4().hex[:6]

    created = platform.post("/api/v1/platform/models", json={
        "display_name": f"Retrieval E2E {suffix}", "provider": "openai", "model_type": "chat", "availability": "all_plans",
        "input_price_per_million": "0", "output_price_per_million": "0", "input_points_per_thousand": "0",
        "output_points_per_thousand": "0", "status": "healthy", "provider_model_id": "gpt-4o-mini",
        "endpoint_url": os.environ.get("E2E_ENDPOINT_URL", "https://api.openai.com/v1"),
        "api_key": "sk-dummy-not-a-real-key", "region": region,
    })
    check("create catalog model", created.status_code == 201, created.text[:200])
    model = created.json()
    kb_id = assistant_id = None
    try:
        saved = admin.put("/api/v1/tenant/model-environments/production", headers=headers, json={
            "expected_version": environment["version"],
            "enabled_model_ids": environment["enabled_model_ids"] + [model["id"]],
            "default_model_ids": {**environment["default_model_ids"], "chat": model["id"]},
        })
        check("tenant enables model", saved.status_code == 200, saved.text[:200])
        wait_for("catalog model synced", lambda: next(m for m in platform.get("/api/v1/platform/models").json()["models"] if m["id"] == model["id"]),
                 lambda m: m["gateway_sync_status"] == "synced")
        wait_for("environment synced", lambda: admin.get("/api/v1/tenant/model-environments/production", headers=headers).json(),
                 lambda e: e["gateway_sync_status"] == "synced")
        gateway = httpx.Client(base_url=LITELLM, headers={"Authorization": f"Bearer {LITELLM_KEY}"}, timeout=30)
        gateway.post("/model/delete", json={"id": model["id"]}).raise_for_status()
        gateway.post("/model/new", json={
            "model_name": f"bw-{model['id']}",
            "litellm_params": {"model": "openai/gpt-4o-mini", "api_key": "sk-dummy", "mock_response": MOCK_ANSWER},
            "model_info": {"id": model["id"], "mode": "chat"},
        }).raise_for_status()

        kb = admin.post("/api/v1/knowledge-bases", headers=headers, json={"name": f"E2E 知识库 {suffix}"})
        check("create knowledge base", kb.status_code == 201, kb.text[:200])
        kb_id = kb.json()["id"]
        wait_for("datasets created by the Worker", lambda: admin.get(f"/api/v1/knowledge-bases/{kb_id}", headers=headers).json(),
                 lambda k: k["sync_status"] == "synced")
        upload = admin.post(f"/api/v1/knowledge-bases/{kb_id}/documents", params={"filename": "退货政策.md"},
                            headers={**headers, "Content-Type": "application/octet-stream"}, content=DOCUMENT)
        check("upload document", upload.status_code == 201, upload.text[:200])
        wait_for("document indexed (parse -> poll)", lambda: admin.get(f"/api/v1/knowledge-bases/{kb_id}", headers=headers).json(),
                 lambda k: k["status"] == "indexed")

        candidates = admin.get("/api/v1/capabilities/candidates?kind=kb", headers=headers).json()["items"]
        check("indexed knowledge base is offered for mounting", kb_id in [c["id"] for c in candidates])
        assistant = admin.post("/api/v1/assistants", headers=headers, json={
            "name": f"Retrieval E2E {suffix}", "description": "e2e",
            "config": {"prompt": "你是客服助理，请依据参考资料回答。", "mounts": {"kb": [kb_id]}},
        })
        check("create assistant with the knowledge base mounted", assistant.status_code == 201, assistant.text[:200])
        assistant_id = assistant.json()["id"]
        check("references count the mount", admin.get(f"/api/v1/knowledge-bases/{kb_id}/references", headers=headers).json()["count"] == 1)

        started = admin.post(f"/api/v1/assistants/{assistant_id}/runs", headers=headers, json={"message": QUESTION})
        check("start run", started.status_code == 202, started.text[:200])
        events = sse_events(admin, started.json()["run_id"], headers)
        check("run completed", events[-1]["type"] == "done", [e["type"] for e in events])
        retrieval = events[-1]["data"]["retrieval"]
        check(f"retrieval used the {ENGINE} engine (not the stub)", retrieval["mode"] == ENGINE, retrieval["mode"])
        check("answer cites the uploaded document", any(c["document_name"] == "退货政策.md" for c in retrieval["citations"]), retrieval["citations"])
        detail = admin.get(f"/api/v1/conversations/{started.json()['conversation_id']}", headers=headers).json()
        check("the stored assistant message carries the citations", bool(detail["messages"][-1]["citations"]))

        admin.delete(f"/api/v1/knowledge-bases/{kb_id}", headers=headers)
        kb_id = None
        after = admin.post(f"/api/v1/assistants/{assistant_id}/runs", headers=headers, json={"message": QUESTION})
        done = sse_events(admin, after.json()["run_id"], headers)[-1]
        check("a deleted knowledge base is no longer searched", done["type"] == "done" and done["data"]["retrieval"]["mode"] == "none")
    finally:
        if assistant_id:
            admin.post(f"/api/v1/assistants/{assistant_id}/disable", headers=headers)
            admin.delete(f"/api/v1/assistants/{assistant_id}", headers=headers)
        if kb_id:
            admin.delete(f"/api/v1/knowledge-bases/{kb_id}", headers=headers)
        current = admin.get("/api/v1/tenant/model-environments/production", headers=headers).json()
        admin.put("/api/v1/tenant/model-environments/production", headers=headers, json={
            "expected_version": current["version"],
            "enabled_model_ids": [i for i in current["enabled_model_ids"] if i != model["id"]],
            "default_model_ids": {k: (environment["default_model_ids"].get(k) if v == model["id"] else v)
                                  for k, v in current["default_model_ids"].items()},
        })
        keys = ("display_name", "provider", "model_type", "availability", "input_price_per_million", "output_price_per_million",
                "input_points_per_thousand", "output_points_per_thousand", "provider_model_id", "endpoint_url", "api_version",
                "region", "context_window", "max_output_tokens", "default_temperature", "rpm_limit", "fallback_model_id",
                "allowlist_tenant_ids")
        platform.put(f"/api/v1/platform/models/{model['id']}", json={**{k: model[k] for k in keys}, "status": "disabled"})
    print(f"Retrieval end-to-end check passed ({ENGINE} flavour of the FAKE engine, mock_response model).")


if __name__ == "__main__":
    sys.exit(main())
