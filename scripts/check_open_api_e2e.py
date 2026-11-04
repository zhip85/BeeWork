"""End-to-end check: tenant API key -> /v1 open API and website widget -> Worker -> LiteLLM -> reply (T-028, T-040).

Runs inside the api container against the running stack (the worker service must be up):
    docker compose exec -e E2E_ENDPOINT_URL=https://1.1.1.1/v1 api python /app/scripts/check_open_api_e2e.py

As in check_runtime_e2e.py, the model's LiteLLM deployment answers from ``mock_response``, so this proves the
whole external path (authentication, version policy, limits, origin allow-list, streaming) but NOT a real model.
Everything created here is removed again.
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
MOCK_ANSWER = "这是来自 LiteLLM mock_response 的回答，用于验证开放 API 与网站挂件。"
SITE = "https://www.e2e-example.com"


def session(email: str, password: str) -> httpx.Client:
    client = httpx.Client(base_url=API, timeout=60, headers={"Origin": ORIGIN})
    client.post("/api/v1/auth/login", json={"email": email, "password": password}).raise_for_status()
    client.headers["X-CSRF-Token"] = client.cookies["beework_csrf"]
    return client


def check(label: str, condition: bool, detail: object = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {label}{f' — {str(detail)[:200]}' if detail else ''}")
    if not condition:
        sys.exit(1)


def wait_for(label: str, read, done, seconds: int = 90):
    deadline = time.time() + seconds
    value = read()
    while not done(value) and time.time() < deadline:
        time.sleep(1.5)
        value = read()
    check(label, done(value))
    return value


def sse(response: httpx.Response) -> list[tuple[str, dict]]:
    frames = []
    for block in response.text.split("\n\n"):
        lines = [line for line in block.splitlines() if not line.startswith(":")]
        if len(lines) >= 2 and lines[0].startswith("event: "):
            frames.append((lines[0][7:], json.loads(lines[1][6:])))
    return frames


def cleanup_data(assistant_id: str, channel_ids: list[str]) -> None:
    from uuid import UUID

    from sqlalchemy import delete, select

    from beework.db import SessionLocal
    from beework.models.assistants import AssistantVersion
    from beework.models.channels import ChannelInstance, TenantApiKey
    from beework.models.conversations import Conversation, EndUser, Message
    from beework.models.jobs import RunEvent
    from beework.models.memory import EndUserMemory
    from beework.models.runtime import Run

    target = UUID(assistant_id)
    time.sleep(3)  # let queued memory jobs for the test conversations finish first
    with SessionLocal() as db:
        conversations = list(db.scalars(select(Conversation.id).where(Conversation.assistant_id == target)))
        end_users = list(db.scalars(select(Conversation.end_user_id).where(Conversation.assistant_id == target)))
        runs = list(db.scalars(select(Run.id).where(Run.assistant_id == target)))
        db.execute(delete(ChannelInstance).where(ChannelInstance.id.in_([UUID(i) for i in channel_ids])))
        db.execute(delete(TenantApiKey).where(TenantApiKey.name.like("e2e-%")))
        db.execute(delete(AssistantVersion).where(AssistantVersion.assistant_id == target))
        db.execute(delete(EndUserMemory).where(EndUserMemory.source_conversation_id.in_(conversations)))
        db.execute(delete(RunEvent).where(RunEvent.run_id.in_(runs)))
        db.execute(delete(Message).where(Message.conversation_id.in_(conversations)))
        db.execute(delete(Run).where(Run.id.in_(runs)))
        db.execute(delete(Conversation).where(Conversation.id.in_(conversations)))
        db.execute(delete(EndUser).where(EndUser.id.in_([e for e in end_users if e])))
        db.commit()


def main() -> None:
    platform = session(*PLATFORM)
    admin = session(*TENANT_ADMIN)
    tenant = admin.get("/api/v1/auth/me").json()["memberships"][0]
    headers = {"X-Tenant-ID": tenant["id"]}
    original_env = admin.get("/api/v1/tenant/model-environments/production", headers=headers).json()
    region = next(t for t in platform.get("/api/v1/platform/tenants").json() if t["id"] == tenant["id"])["region"]

    created = platform.post(
        "/api/v1/platform/models",
        json={
            "display_name": f"OpenAPI E2E {uuid.uuid4().hex[:6]}", "provider": "openai", "model_type": "chat",
            "availability": "all_plans", "input_price_per_million": "0", "output_price_per_million": "0",
            "input_points_per_thousand": "0", "output_points_per_thousand": "0", "status": "healthy",
            "provider_model_id": "gpt-4o-mini", "endpoint_url": os.environ.get("E2E_ENDPOINT_URL", "https://api.openai.com/v1"),
            "api_key": "sk-dummy-not-a-real-key", "region": region,
        },
    )
    check("create catalog model", created.status_code == 201, created.text[:200])
    model = created.json()
    assistant_id, policies, channel_ids = None, {}, []
    try:
        saved = admin.put(
            "/api/v1/tenant/model-environments/production", headers=headers,
            json={"expected_version": original_env["version"], "enabled_model_ids": original_env["enabled_model_ids"] + [model["id"]],
                  "default_model_ids": {**original_env["default_model_ids"], "chat": model["id"]}},
        )
        check("tenant enables model", saved.status_code == 200, saved.text[:200])
        wait_for("catalog model synced", lambda: next(m for m in platform.get("/api/v1/platform/models").json()["models"] if m["id"] == model["id"]), lambda m: m["gateway_sync_status"] == "synced")
        wait_for("environment synced", lambda: admin.get("/api/v1/tenant/model-environments/production", headers=headers).json(), lambda e: e["gateway_sync_status"] == "synced")
        gateway = httpx.Client(base_url=LITELLM, headers={"Authorization": f"Bearer {LITELLM_KEY}"}, timeout=30)
        gateway.post("/model/delete", json={"id": model["id"]}).raise_for_status()
        mock = gateway.post("/model/new", json={
            "model_name": f"bw-{model['id']}",
            "litellm_params": {"model": "openai/gpt-4o-mini", "api_key": "sk-dummy", "mock_response": MOCK_ANSWER},
            "model_info": {"id": model["id"], "mode": "chat"},
        })
        check("gateway deployment replaced by mock_response", mock.status_code == 200, mock.text[:200])

        # The dev tenant has one admin, who cannot approve their own requests: switch the two policies off for the check.
        for item in admin.get("/api/v1/tenant/approval-policies", headers=headers).json()["items"]:
            if item["event"] in ("publish", "channel") and item["enabled"]:
                policies[item["event"]] = item
                off = admin.put("/api/v1/tenant/approval-policies", headers=headers,
                                json={"items": [{"event": item["event"], "approver_role": item["approver_role"], "levels": item["levels"], "enabled": False}]})
                check(f"{item['event']} approval switched off for the check", off.status_code == 200, off.text[:200])

        assistant = admin.post("/api/v1/assistants", headers=headers, json={"name": f"OpenAPI E2E {uuid.uuid4().hex[:6]}", "description": "e2e", "config": {"prompt": "You are a test assistant.", "greeting": "你好！我是 e2e 助手"}})
        check("create assistant", assistant.status_code == 201, assistant.text[:200])
        assistant_id = assistant.json()["id"]
        published = admin.post(f"/api/v1/assistants/{assistant_id}/publish", headers=headers, json={"change_note": "e2e"})
        check("publish assistant v1.0", published.status_code == 201, published.text[:200])

        # ---- T-028: API key + open API ---------------------------------------------------------------------------
        key = admin.post("/api/v1/tenant/api-keys", headers=headers, json={"name": "e2e-key", "environment": "production", "scopes": ["chat"]})
        check("create API key", key.status_code == 201 and key.json()["key"].startswith("bw_live_"), key.text[:120])
        secret, key_id = key.json()["key"], key.json()["id"]
        client = httpx.Client(base_url=API, timeout=90)
        auth = {"Authorization": f"Bearer {secret}"}
        body = {"message": "你好，请介绍一下自己", "end_user": {"id": "e2e-user-1", "name": "E2E"}}

        denied = client.post(f"/v1/assistants/{assistant_id}/chat", json=body)
        check("no key -> 401 in the open API envelope", denied.status_code == 401 and denied.json()["error"]["code"] == "missing_api_key", denied.text[:120])
        reply = client.post(f"/v1/assistants/{assistant_id}/chat", json=body, headers=auth)
        check("real conversation through /v1 chat", reply.status_code == 200 and reply.json()["message"]["content"] == MOCK_ANSWER, reply.text[:300])
        conversation_id = reply.json()["conversation_id"]
        print(f"     usage: {reply.json()['usage']}")
        streamed = client.post(f"/v1/assistants/{assistant_id}/chat", json={**body, "message": "再说一次", "conversation_id": conversation_id, "stream": True}, headers=auth)
        frames = sse(streamed)
        names = [n for n, _ in frames]
        check("stream=true returns start / delta / done", streamed.status_code == 200 and names[0] == "start" and names[-1] == "done" and "delta" in names, names)
        check("streamed text equals the answer", "".join(d["text"] for n, d in frames if n == "delta") == MOCK_ANSWER or frames[-1][1]["message"]["content"] == MOCK_ANSWER)
        history = client.get(f"/v1/conversations/{conversation_id}/messages", headers=auth).json()
        check("messages endpoint returns the 4-message history", [m["role"] for m in history["messages"]] == ["user", "assistant", "user", "assistant"], history)
        other = client.post(f"/v1/assistants/{assistant_id}/chat", json={**body, "end_user": {"id": "someone-else"}, "conversation_id": conversation_id}, headers=auth)
        check("another end user cannot continue the conversation", other.status_code == 404, other.text[:120])
        schema = client.get("/v1/openapi.json").json()
        check("separate OpenAPI document for /v1", "/v1/assistants/{assistant_id}/chat" in schema["paths"])
        revoked = admin.post(f"/api/v1/tenant/api-keys/{key_id}/revoke", headers=headers)
        check("revoke key", revoked.status_code == 200)
        after = client.post(f"/v1/assistants/{assistant_id}/chat", json=body, headers=auth)
        check("revoked key -> 401 immediately", after.status_code == 401 and after.json()["error"]["code"] == "invalid_api_key", after.text[:120])

        # ---- T-040: website channel + widget -------------------------------------------------------------------------
        channel = admin.post("/api/v1/channels", headers=headers, json={"name": f"e2e-{uuid.uuid4().hex[:6]}", "assistant_id": assistant_id, "environment": "production", "allowed_origins": [SITE], "experience": {"primary_color": "#123456"}})
        check("create website channel", channel.status_code == 201, channel.text[:200])
        channel_ids.append(channel.json()["id"])
        live = admin.post(f"/api/v1/channels/{channel.json()['id']}/publish", headers=headers, json={})
        check("publish channel", live.status_code == 200 and live.json()["status"] == "active", live.text[:200])
        site_key = live.json()["site_key"]
        check("embed code carries the site key", site_key in live.json()["embed_code"], live.json()["embed_code"])
        script = client.get("/v1/widget.js")
        check("widget.js is served", script.status_code == 200 and "javascript" in script.headers["content-type"] and "data-site-key" in script.text)
        bad = client.post("/v1/widget/session", json={"site_key": site_key}, headers={"Origin": "https://evil.example.com"})
        check("non-allowed Origin refused", bad.status_code == 403 and bad.json()["error"]["code"] == "origin_not_allowed", bad.text[:120])
        opened = client.post("/v1/widget/session", json={"site_key": site_key}, headers={"Origin": SITE})
        check("session for the allowed Origin", opened.status_code == 200 and opened.headers.get("access-control-allow-origin") == SITE, opened.text[:200])
        check("session config inherits name / greeting and applies the colour", opened.json()["config"]["welcome"] == "你好！我是 e2e 助手" and opened.json()["config"]["primary_color"] == "#123456", opened.json()["config"])
        visitor = {"Authorization": f"Bearer {opened.json()['token']}", "Origin": SITE}
        chat = client.post("/v1/widget/chat", json={"message": "你好", "stream": True}, headers=visitor)
        frames = sse(chat)
        check("real visitor conversation through the widget API (SSE)", chat.status_code == 200 and frames[-1][0] == "done" and frames[-1][1]["message"]["content"] == MOCK_ANSWER, frames[-1] if frames else chat.text[:200])
        if os.environ.get("E2E_WIDGET_HOLD"):
            # Lets scripts/check_widget_page.mjs load the real widget.js against this live channel (see its header).
            print("WIDGET_FIXTURE " + json.dumps({"site_key": site_key, "origin": SITE, "api": API, "answer": MOCK_ANSWER, "welcome": opened.json()["config"]["welcome"]}), flush=True)
            deadline = time.time() + int(os.environ["E2E_WIDGET_HOLD"])
            while time.time() < deadline and not os.path.exists("/tmp/e2e_widget.done"):
                time.sleep(1)
        wrong_origin = client.post("/v1/widget/chat", json={"message": "hi"}, headers={**visitor, "Origin": "https://evil.example.com"})
        check("a stolen token does not work from another origin", wrong_origin.status_code == 403, wrong_origin.text[:120])
        off = admin.post(f"/api/v1/channels/{channel.json()['id']}/disable", headers=headers)
        check("disable channel", off.status_code == 200)
        gone = client.post("/v1/widget/chat", json={"message": "hi"}, headers=visitor)
        check("a disabled channel stops serving at once", gone.status_code == 403 and gone.json()["error"]["code"] == "channel_unavailable", gone.text[:120])
    finally:
        for item in policies.values():
            admin.put("/api/v1/tenant/approval-policies", headers=headers,
                      json={"items": [{"event": item["event"], "approver_role": item["approver_role"], "levels": item["levels"], "enabled": True}]})
        if assistant_id:
            cleanup_data(assistant_id, channel_ids)
            # Only drafts and disabled assistants can be deleted (AST-010), so the published one is disabled first.
            admin.post(f"/api/v1/assistants/{assistant_id}/disable", headers=headers)
            admin.delete(f"/api/v1/assistants/{assistant_id}", headers=headers)
        current = admin.get("/api/v1/tenant/model-environments/production", headers=headers).json()
        admin.put("/api/v1/tenant/model-environments/production", headers=headers, json={
            "expected_version": current["version"], "enabled_model_ids": [i for i in current["enabled_model_ids"] if i != model["id"]],
            "default_model_ids": {k: (original_env["default_model_ids"].get(k) if v == model["id"] else v) for k, v in current["default_model_ids"].items()},
        })
        payload = {k: model[k] for k in ("display_name", "provider", "model_type", "availability", "input_price_per_million", "output_price_per_million",
                                         "input_points_per_thousand", "output_points_per_thousand", "provider_model_id", "endpoint_url", "api_version",
                                         "region", "context_window", "max_output_tokens", "default_temperature", "rpm_limit", "fallback_model_id", "allowlist_tenant_ids")}
        platform.put(f"/api/v1/platform/models/{model['id']}", json={**payload, "status": "disabled"})
    print("Open API and widget end-to-end check passed (mock_response model).")


if __name__ == "__main__":
    main()
