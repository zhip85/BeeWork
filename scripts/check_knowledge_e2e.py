"""End-to-end check of knowledge bases (T-030) against the FAKE engine (scripts/fake_ragflow.py), not a real one.

    # RAGFlow flavour
    RAGFLOW_BASE_URL=http://ragflow-fake:9380 RAGFLOW_API_KEY=fake-ragflow-key \\
      docker compose --profile fake-ragflow up -d ragflow-fake api worker
    docker compose exec api python /app/scripts/check_knowledge_e2e.py
    # Dify flavour
    KNOWLEDGE_ENGINE=dify DIFY_BASE_URL=http://ragflow-fake:9380/v1 DIFY_API_KEY=fake-ragflow-key \\
      docker compose --profile fake-ragflow up -d ragflow-fake api worker
    E2E_ENGINE=dify docker compose exec -e E2E_ENGINE=dify api python /app/scripts/check_knowledge_e2e.py

Creates a knowledge base through the API, lets the real Worker create its datasets, edits the settings (rebuild),
copies and deletes it, and checks the fake's state after each step. Proves BeeWork's side of the contract as written
from RAGFlow's documentation; it says nothing about how a real RAGFlow behaves.
"""

import os
import sys
import time
import uuid

import httpx
from check_runtime_e2e import PLATFORM, TENANT_ADMIN, check, session, wait_for

FAKE = os.environ.get("FAKE_RAGFLOW_URL", "http://ragflow-fake:9380")
ENGINE = os.environ.get("E2E_ENGINE", "ragflow")


def fake_datasets() -> dict[str, dict]:
    state = "dify-state" if ENGINE == "dify" else "state"
    return {item["id"]: item for item in httpx.get(f"{FAKE}/__fake/{state}", timeout=10).json()["data"]}


def tag(kb_id: str) -> str:
    return f"bw-{kb_id.replace('-', '')[:24]}"  # what the dataset names are built from


def main() -> None:
    platform = session(*PLATFORM)
    tenant_admin = session(*TENANT_ADMIN)
    tenant = tenant_admin.get("/api/v1/auth/me").json()["memberships"][0]
    headers = {"X-Tenant-ID": tenant["id"]}
    integrations = {item["key"]: item for item in platform.get("/api/v1/platform/integrations").json()}
    check(f"{ENGINE} (fake) reachable from the API", integrations[ENGINE]["reachable"] is True, integrations[ENGINE])

    name = f"KB E2E {uuid.uuid4().hex[:6]}"
    created = tenant_admin.post("/api/v1/knowledge-bases", headers=headers, json={"name": name, "description": "e2e"})
    check("create knowledge base", created.status_code == 201, created.text[:200])
    kb = created.json()
    kb_id = kb["id"]
    copy_id = None
    try:
        check("defaults: bge-m3 / semantic / 0.62 / draft", (kb["embedding_model"], kb["chunking"], kb["retrieval_threshold"], kb["status"]) == ("bge-m3", "semantic", "0.62", "draft"), kb)
        url = f"/api/v1/knowledge-bases/{kb_id}"
        synced = wait_for("Worker created the datasets", lambda: tenant_admin.get(url, headers=headers).json(), lambda k: k["sync_status"] == "synced")
        datasets = [d for d in fake_datasets().values() if tag(kb_id) in d["name"]]
        check("two datasets named from the id, not from the typed name", len(datasets) == 2 and all(name not in d["name"] and len(d["name"]) <= 40 for d in datasets) and kb["engine"] == ENGINE, [d["name"] for d in datasets])

        body = {k: synced[k] for k in ("name", "description", "language", "kb_type", "embedding_model", "chunking", "retrieval_threshold", "row_version")}
        saved = tenant_admin.put(url, headers=headers, json={**body, "chunking": "by_heading", "embedding_model": "text-embedding-3-large"})
        check("settings saved, index rebuild queued", saved.status_code == 200 and saved.json()["reindexing"] is True, saved.text[:200])
        wait_for("Worker rebuilt the datasets", lambda: tenant_admin.get(url, headers=headers).json(), lambda k: k["sync_status"] == "synced" and not k["reindexing"])
        wait_for("replaced datasets deleted", lambda: [d for d in fake_datasets().values() if tag(kb_id) in d["name"]], lambda items: len(items) == 2 and all("-g2" in d["name"] for d in items))

        copied = tenant_admin.post(f"{url}/copy", headers=headers)
        check("copy is a draft with a suffixed name", copied.status_code == 201 and copied.json()["name"] == f"{name}（副本）" and copied.json()["status"] == "draft", copied.text[:200])
        copy_id = copied.json()["id"]
        wait_for("copy got its own datasets", lambda: [d for d in fake_datasets().values() if tag(copy_id) in d["name"]], lambda items: len(items) == 2)

        check("delete", tenant_admin.delete(url, headers=headers).status_code == 204)
        check("deleted knowledge base is gone", tenant_admin.get(url, headers=headers).status_code == 404)
        wait_for("datasets removed from the engine", lambda: [d for d in fake_datasets().values() if tag(kb_id) in d["name"]], lambda items: not items)
    finally:
        for target in (kb_id, copy_id):
            if target:
                tenant_admin.delete(f"/api/v1/knowledge-bases/{target}", headers=headers)
        time.sleep(3)
    print("Knowledge base end-to-end check passed (FAKE engine: " + ENGINE + ").")


if __name__ == "__main__":
    sys.exit(main())
