import importlib.util
import threading
import unittest
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from beework import knowledge
from beework.config import settings
from beework.db import get_db
from beework.main import app
from beework.models import AuditEvent, Base, Membership, Organization, Tenant, TenantRole, User
from beework.models.jobs import Job
from beework.models.knowledge import KnowledgeBase
from beework.security import hash_password
from beework.worker.runner import run_once
from isolation import disable_external_integrations, stub_public_dns

PASSWORD = "correct horse battery"


def load_fake_ragflow():
    path = Path(__file__).resolve().parent.parent / "scripts" / "fake_ragflow.py"
    if not path.exists():
        path = Path(__file__).resolve().parents[2] / "scripts" / "fake_ragflow.py"
    spec = importlib.util.spec_from_file_location("fake_ragflow", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Harness(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        disable_external_integrations()
        stub_public_dns()
        cls.fake = load_fake_ragflow()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cls.fake.make_handler())
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.fake_url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

        @event.listens_for(cls.engine, "connect")
        def enable_sqlite_foreign_keys(connection, _record) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        cls.session_factory = sessionmaker(bind=cls.engine, expire_on_commit=False)

        def override_get_db():
            with cls.session_factory() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        app.dependency_overrides.clear()
        cls.engine.dispose()

    def setUp(self) -> None:
        raw = self.engine.raw_connection()
        raw.cursor().execute("PRAGMA foreign_keys=OFF")
        raw.close()
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        self.client = TestClient(app)
        self.fake.datasets.clear()
        self.fake.dify_datasets.clear()
        self.fake.documents.clear()
        self.fake.fail_next = 0
        self.configure(True)
        self._providers = list(knowledge._content_providers)
        self._counters = list(knowledge.REFERENCE_COUNTERS)
        with self.session_factory.begin() as db:
            tenant = Tenant(name="Acme", slug="acme", plan="enterprise", seat_limit=9)
            other = Tenant(name="Other", slug="other", plan="enterprise", seat_limit=9)
            admin = User(email="admin@example.com", display_name="Admin", password_hash=hash_password(PASSWORD))
            viewer = User(email="viewer@example.com", display_name="Viewer", password_hash=hash_password(PASSWORD))
            scoped = User(email="scoped@example.com", display_name="Scoped", password_hash=hash_password(PASSWORD))
            other_admin = User(email="other@example.com", display_name="OtherAdmin", password_hash=hash_password(PASSWORD))
            db.add_all([tenant, other, admin, viewer, scoped, other_admin])
            db.flush()
            root = Organization(tenant_id=tenant.id, name="Acme")
            other_root = Organization(tenant_id=other.id, name="Other")
            db.add_all([root, other_root])
            db.flush()
            sales = Organization(tenant_id=tenant.id, name="Sales", parent_id=root.id)
            support = Organization(tenant_id=tenant.id, name="Support", parent_id=root.id)
            db.add_all([sales, support])
            db.flush()
            role = TenantRole(
                tenant_id=tenant.id, name="Sales KB", scope="organization", scope_organization_id=sales.id,
                permissions={"knowledge": ["view", "edit", "delete"]},
            )
            db.add(role)
            db.flush()
            db.add_all([
                Membership(tenant_id=tenant.id, user_id=admin.id, organization_id=root.id, role="tenant_admin"),
                Membership(tenant_id=tenant.id, user_id=viewer.id, organization_id=root.id, role="viewer"),
                Membership(tenant_id=tenant.id, user_id=scoped.id, organization_id=sales.id, role="member", custom_role_id=role.id),
                Membership(tenant_id=other.id, user_id=other_admin.id, organization_id=other_root.id, role="tenant_admin"),
            ])
            self.tenant_id, self.other_id = tenant.id, other.id
            self.sales_id, self.support_id = sales.id, support.id

    def tearDown(self) -> None:
        knowledge._content_providers[:] = self._providers
        knowledge.REFERENCE_COUNTERS[:] = self._counters
        self.configure(False)

    # -- helpers ----------------------------------------------------------------------------------------------

    def configure(self, on: bool, engine: str = "ragflow") -> None:
        settings.knowledge_engine = engine
        settings.ragflow_base_url = self.fake_url if on and engine == "ragflow" else ""
        settings.ragflow_api_key = self.fake.API_KEY if on and engine == "ragflow" else ""
        settings.dify_base_url = f"{self.fake_url}/v1" if on and engine == "dify" else ""
        settings.dify_api_key = self.fake.API_KEY if on and engine == "dify" else ""

    def login(self, email: str, tenant_id: UUID | None = None) -> dict[str, str]:
        response = self.client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return {"X-CSRF-Token": self.client.cookies.get("beework_csrf"), "X-Tenant-ID": str(tenant_id or self.tenant_id)}

    def create(self, headers, **overrides):
        return self.client.post("/api/v1/knowledge-bases", headers=headers, json={"name": "产品知识库", **overrides})

    def worker(self) -> int:
        return run_once("kb-worker", self.session_factory, schedule=False)

    def row(self, kb_id: str) -> KnowledgeBase:
        with self.session_factory() as db:
            return db.get(KnowledgeBase, UUID(kb_id))

    def settings_payload(self, body: dict, **overrides) -> dict:
        keys = ("name", "description", "language", "kb_type", "embedding_model", "chunking", "retrieval_threshold", "row_version")
        return {**{key: body[key] for key in keys}, **overrides}

    def provide(self, **stats) -> None:
        knowledge.register_content_provider(lambda _db, _kb: knowledge.ContentStats(**stats))


class KnowledgeTests(_Harness):
    # -- create + dataset sync --------------------------------------------------------------------------------

    def test_create_uses_the_documented_defaults_and_syncs_two_datasets_through_the_worker(self) -> None:
        headers = self.login("admin@example.com")
        response = self.create(headers, description="  产品资料  ")
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(
            (body["kb_type"], body["language"], body["embedding_model"], body["chunking"], body["retrieval_threshold"]),
            ("standard", "multilingual", "bge-m3", "semantic", "0.62"),
        )
        self.assertEqual((body["status"], body["docs_count"], body["faq_count"], body["sync_status"]), ("draft", 0, 0, "pending"))
        self.assertEqual(body["description"], "产品资料")
        self.assertEqual(self.fake.datasets, {})  # creating only queues the work
        self.assertEqual(self.worker(), 1)
        kb = self.row(body["id"])
        self.assertEqual((kb.sync_status, kb.sync_error), ("synced", None))
        names = {dataset["name"]: dataset for dataset in self.fake.datasets.values()}
        base = f"bw-{kb.id.hex[:24]}-g1"
        self.assertEqual(set(names), {base, f"{base}-faq"})  # ids only, never the typed name
        self.assertEqual(kb.engine, "ragflow")
        self.assertEqual(names[base]["chunk_method"], "naive")
        self.assertEqual(names[f"{base}-faq"]["chunk_method"], "qa")
        self.assertEqual(kb.engine_dataset_id, names[base]["id"])

    def test_an_faq_knowledge_base_only_gets_the_faq_dataset(self) -> None:
        headers = self.login("admin@example.com")
        body = self.create(headers, kb_type="faq", language="zh-CN").json()
        self.worker()
        kb = self.row(body["id"])
        self.assertIsNone(kb.engine_dataset_id)
        self.assertIsNotNone(kb.engine_faq_dataset_id)
        self.assertEqual(len(self.fake.datasets), 1)

    def test_without_an_engine_creation_still_succeeds_and_reconcile_syncs_it_later(self) -> None:
        self.configure(False)
        headers = self.login("admin@example.com")
        body = self.create(headers).json()
        self.assertEqual(body["sync_status"], "not_configured")
        with self.session_factory() as db:
            self.assertEqual(db.scalar(select(Job).where(Job.kind == "knowledge.ensure_datasets")), None)
        self.assertEqual(self.worker(), 0)
        self.configure(True)
        with self.session_factory() as db:
            knowledge.reconcile_job(db, {})
        self.assertEqual(self.worker(), 1)
        self.assertEqual(self.row(body["id"]).sync_status, "synced")
        self.assertEqual(len(self.fake.datasets), 2)

    def test_engine_failure_is_recorded_and_the_retry_endpoint_recovers_without_duplicates(self) -> None:
        headers = self.login("admin@example.com")
        body = self.create(headers).json()
        self.fake.fail_next = 1
        self.worker()
        failed = self.row(body["id"])
        self.assertEqual(failed.sync_status, "failed")
        self.assertIn("Injected failure", failed.sync_error)
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{body['id']}", headers=headers).json()["sync_status"], "failed")
        retried = self.client.post(f"/api/v1/knowledge-bases/{body['id']}/retry-sync", headers=headers)
        self.assertEqual((retried.status_code, retried.json()["sync_status"]), (200, "pending"))
        self.worker()
        self.assertEqual(self.row(body["id"]).sync_status, "synced")
        self.assertEqual(len(self.fake.datasets), 2)
        self.assertEqual(self.client.post(f"/api/v1/knowledge-bases/{body['id']}/retry-sync", headers=headers).status_code, 409)

    def test_a_retry_reuses_a_dataset_an_earlier_attempt_already_created(self) -> None:
        headers = self.login("admin@example.com")
        body = self.create(headers).json()
        # The first attempt created the document dataset but died before saving its id.
        with self.session_factory() as db:
            kb = db.get(KnowledgeBase, UUID(body["id"]))
            from beework.integrations.ragflow import RAGFlowClient

            with RAGFlowClient() as client:
                orphan = client.create_dataset(name=knowledge.dataset_name(kb), chunk_method="naive")
        self.worker()
        self.assertEqual(self.row(body["id"]).engine_dataset_id, orphan)
        self.assertEqual(len(self.fake.datasets), 2)

    # -- validation, uniqueness, permissions ------------------------------------------------------------------

    def test_names_are_unique_per_tenant_case_insensitively_and_validated(self) -> None:
        headers = self.login("admin@example.com")
        self.assertEqual(self.create(headers, name="Policies").status_code, 201)
        taken = self.create(headers, name="policies")
        self.assertEqual((taken.status_code, taken.json()["code"]), (409, "knowledge.name_taken"))
        for bad in ({"name": ""}, {"name": "   "}, {"name": "x" * 51}, {"name": "ok", "kb_type": "wiki"}, {"name": "ok", "language": "fr"}, {"name": "ok", "description": "d" * 501}):
            self.assertIn(self.client.post("/api/v1/knowledge-bases", headers=headers, json=bad).status_code, (400, 422), bad)
        self.client.post("/api/v1/auth/logout", headers=headers)
        other = self.login("other@example.com", self.other_id)
        self.assertEqual(self.create(other, name="Policies").status_code, 201)  # another tenant may reuse the name

    def test_permissions_and_tenant_isolation(self) -> None:
        admin = self.login("admin@example.com")
        kb_id = self.create(admin).json()["id"]
        self.client.post("/api/v1/auth/logout", headers=admin)
        viewer = self.login("viewer@example.com")
        self.assertEqual(self.client.get("/api/v1/knowledge-bases", headers=viewer).json()["total"], 1)
        self.assertEqual(self.create(viewer, name="Nope").status_code, 403)
        self.assertEqual(self.client.delete(f"/api/v1/knowledge-bases/{kb_id}", headers=viewer).status_code, 403)
        self.client.post("/api/v1/auth/logout", headers=viewer)
        other = self.login("other@example.com", self.other_id)
        self.assertEqual(self.client.get("/api/v1/knowledge-bases", headers=other).json()["total"], 0)
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=other).status_code, 404)  # not even its existence
        self.assertEqual(self.client.delete(f"/api/v1/knowledge-bases/{kb_id}", headers=other).status_code, 404)
        self.client.post("/api/v1/auth/logout", headers=other)
        self.assertEqual(self.client.get("/api/v1/knowledge-bases").status_code, 401)

    def test_an_organization_scoped_role_only_reaches_its_own_subtree(self) -> None:
        admin = self.login("admin@example.com")
        mine = self.create(admin, name="Sales KB", organization_id=str(self.sales_id)).json()["id"]
        theirs = self.create(admin, name="Support KB", organization_id=str(self.support_id)).json()["id"]
        self.client.post("/api/v1/auth/logout", headers=admin)
        scoped = self.login("scoped@example.com")
        listed = [item["name"] for item in self.client.get("/api/v1/knowledge-bases", headers=scoped).json()["items"]]
        self.assertEqual(listed, ["Sales KB"])
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{theirs}", headers=scoped).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/v1/knowledge-bases/{theirs}", headers=scoped).status_code, 404)
        self.assertEqual(self.create(scoped, name="Elsewhere", organization_id=str(self.support_id)).status_code, 403)
        own = self.create(scoped, name="Own")  # defaults to the caller's organization
        self.assertEqual(own.status_code, 201, own.text)
        self.assertEqual(own.json()["organization_id"], str(self.sales_id))
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{mine}", headers=scoped).status_code, 200)

    # -- settings ---------------------------------------------------------------------------------------------

    def test_settings_validate_the_threshold_and_guard_against_stale_edits(self) -> None:
        headers = self.login("admin@example.com")
        body = self.create(headers).json()
        url = f"/api/v1/knowledge-bases/{body['id']}"
        for bad in ({"retrieval_threshold": "1.01"}, {"retrieval_threshold": "-0.1"}, {"retrieval_threshold": "0.555"}, {"embedding_model": "gpt"}, {"chunking": "random"}):
            self.assertEqual(self.client.put(url, headers=headers, json=self.settings_payload(body, **bad)).status_code, 422, bad)
        saved = self.client.put(url, headers=headers, json=self.settings_payload(body, retrieval_threshold="0.70", description="新的"))
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual((saved.json()["retrieval_threshold"], saved.json()["row_version"]), ("0.70", body["row_version"] + 1))
        stale = self.client.put(url, headers=headers, json=self.settings_payload(body))
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "conflict.stale"))
        self.assertEqual(self.worker(), 1)  # only the creation sync: a threshold change never touches the datasets

    def test_changing_the_model_or_chunking_needs_confirmation_when_documents_exist_and_rebuilds_the_datasets(self) -> None:
        headers = self.login("admin@example.com")
        body = self.create(headers).json()
        self.worker()
        old = self.row(body["id"])
        old_ids = {old.engine_dataset_id, old.engine_faq_dataset_id}
        self.provide(documents=3, indexed_documents=3)
        url = f"/api/v1/knowledge-bases/{body['id']}"
        refused = self.client.put(url, headers=headers, json=self.settings_payload(body, chunking="by_heading"))
        self.assertEqual((refused.status_code, refused.json()["code"]), (409, "knowledge.reindex_required"))
        saved = self.client.put(url, headers=headers, json=self.settings_payload(body, chunking="by_heading", embedding_model="text-embedding-3-large", confirm_reindex=True))
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual((saved.json()["status"], saved.json()["reindexing"]), ("processing", True))
        self.assertEqual(self.row(body["id"]).engine_dataset_id, old.engine_dataset_id)  # the old index keeps serving
        self.worker()
        swapped = self.row(body["id"])
        self.assertFalse(swapped.reindexing)
        self.assertEqual(swapped.sync_status, "synced")
        self.assertTrue(old_ids.isdisjoint({swapped.engine_dataset_id, swapped.engine_faq_dataset_id}))
        self.worker()  # the old datasets are deleted by their own job after the switch
        self.assertEqual({dataset["id"] for dataset in self.fake.datasets.values()}, {swapped.engine_dataset_id, swapped.engine_faq_dataset_id})
        rebuilt = {dataset["id"]: dataset for dataset in self.fake.datasets.values()}[swapped.engine_dataset_id]
        self.assertEqual((rebuilt["chunk_method"], rebuilt["embedding_model"]), ("book", "text-embedding-3-large"))
        self.assertEqual(self.client.get(url, headers=headers).json()["status"], "indexed")

    def test_the_reindex_hook_runs_before_the_switch_and_a_failure_keeps_the_old_index(self) -> None:
        headers = self.login("admin@example.com")
        body = self.create(headers).json()
        self.worker()
        old = self.row(body["id"])
        seen: list[dict] = []
        broken = {"on": True}

        def hook(_db, _kb, new_ids):
            seen.append(new_ids)
            if broken["on"]:
                raise knowledge.KnowledgeEngineError("re-upload failed")

        knowledge.REINDEX_HOOKS.append(hook)
        try:
            url = f"/api/v1/knowledge-bases/{body['id']}"
            self.client.put(url, headers=headers, json=self.settings_payload(body, chunking="fixed_512"))
            self.worker()
            failed = self.row(body["id"])
            self.assertEqual((failed.sync_status, failed.engine_dataset_id), ("failed", old.engine_dataset_id))
            self.assertTrue(failed.reindexing)
            broken["on"] = False
            self.client.post(f"{url}/retry-sync", headers=headers)
            self.worker()
            done = self.row(body["id"])
            self.assertEqual((done.sync_status, done.reindexing), ("synced", False))
            self.assertNotEqual(done.engine_dataset_id, old.engine_dataset_id)
            self.assertEqual(len(seen), 2)
            self.worker()  # deletes the replaced datasets
            self.assertEqual({d["parser_config"].get("chunk_token_num") for d in self.fake.datasets.values() if d["chunk_method"] == "naive"}, {512})
        finally:
            knowledge.REINDEX_HOOKS.remove(hook)

    def test_switching_type_follows_the_documents_rule(self) -> None:
        headers = self.login("admin@example.com")
        body = self.create(headers).json()
        self.worker()
        url = f"/api/v1/knowledge-bases/{body['id']}"
        self.provide(documents=2, indexed_documents=2)
        blocked = self.client.put(url, headers=headers, json=self.settings_payload(body, kb_type="faq"))
        self.assertEqual((blocked.status_code, blocked.json()["code"]), (409, "knowledge.has_documents"))
        knowledge._content_providers.clear()
        to_faq = self.client.put(url, headers=headers, json=self.settings_payload(body, kb_type="faq"))
        self.assertEqual((to_faq.status_code, to_faq.json()["kb_type"]), (200, "faq"), to_faq.text)
        self.assertIsNone(self.row(body["id"]).engine_dataset_id)
        self.worker()
        self.assertEqual(len(self.fake.datasets), 1)  # the empty document dataset was deleted, the FAQ one kept
        again = self.client.put(url, headers=headers, json=self.settings_payload(to_faq.json(), kb_type="standard"))
        self.assertEqual(again.status_code, 200, again.text)
        self.worker()
        self.assertIsNotNone(self.row(body["id"]).engine_dataset_id)
        self.assertEqual(len(self.fake.datasets), 2)

    def test_status_follows_the_content(self) -> None:
        headers = self.login("admin@example.com")
        body = self.create(headers).json()
        url = f"/api/v1/knowledge-bases/{body['id']}"
        self.assertEqual(self.client.get(url, headers=headers).json()["status"], "draft")
        self.provide(documents=1, faqs=2, indexed_documents=1, published_faqs=0)
        item = self.client.get(url, headers=headers).json()
        self.assertEqual((item["status"], item["docs_count"], item["faq_count"]), ("indexed", 1, 2))
        self.provide(processing_documents=1)
        self.assertEqual(self.client.get(url, headers=headers).json()["status"], "processing")
        knowledge._content_providers.clear()
        self.provide(faqs=1, published_faqs=1)
        self.assertEqual(self.client.get(url, headers=headers).json()["status"], "indexed")
        listed = self.client.get("/api/v1/knowledge-bases?status=draft", headers=headers).json()["items"]
        self.assertEqual(listed, [])

    # -- list, copy, delete -----------------------------------------------------------------------------------

    def test_list_searches_names_and_filters_by_type(self) -> None:
        headers = self.login("admin@example.com")
        self.create(headers, name="Product Guide")
        self.create(headers, name="常见问题", kb_type="faq")
        self.assertEqual(self.client.get("/api/v1/knowledge-bases?q=PRODUCT", headers=headers).json()["total"], 1)
        self.assertEqual(self.client.get("/api/v1/knowledge-bases?q=问题", headers=headers).json()["items"][0]["kb_type"], "faq")
        self.assertEqual(self.client.get("/api/v1/knowledge-bases?kb_type=standard", headers=headers).json()["total"], 1)
        self.assertEqual(self.client.get("/api/v1/knowledge-bases?kb_type=wiki", headers=headers).status_code, 422)

    def test_copy_makes_a_draft_with_its_own_datasets(self) -> None:
        headers = self.login("admin@example.com")
        body = self.create(headers).json()
        self.provide(documents=5, indexed_documents=5)
        self.client.put(f"/api/v1/knowledge-bases/{body['id']}", headers=headers, json=self.settings_payload(body, retrieval_threshold="0.8"))
        knowledge._content_providers.clear()
        self.worker()
        copy = self.client.post(f"/api/v1/knowledge-bases/{body['id']}/copy", headers=headers)
        self.assertEqual(copy.status_code, 201, copy.text)
        again = self.client.post(f"/api/v1/knowledge-bases/{body['id']}/copy", headers=headers).json()
        self.assertEqual((copy.json()["name"], again["name"]), ("产品知识库（副本）", "产品知识库（副本） 2"))
        self.assertEqual((copy.json()["status"], copy.json()["retrieval_threshold"]), ("draft", "0.80"))
        self.worker()
        self.assertEqual(len(self.fake.datasets), 6)
        self.assertNotEqual(self.row(copy.json()["id"]).engine_dataset_id, self.row(body["id"]).engine_dataset_id)

    def test_delete_is_soft_removes_the_datasets_and_reports_references(self) -> None:
        headers = self.login("admin@example.com")
        body = self.create(headers).json()
        self.worker()
        knowledge.REFERENCE_COUNTERS.append(lambda _db, _kb: 2)
        refs = self.client.get(f"/api/v1/knowledge-bases/{body['id']}/references", headers=headers)
        self.assertEqual(refs.json(), {"count": 2})
        self.assertEqual(self.client.delete(f"/api/v1/knowledge-bases/{body['id']}", headers=headers).status_code, 204)
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{body['id']}", headers=headers).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/knowledge-bases", headers=headers).json()["total"], 0)
        self.assertIsNotNone(self.row(body["id"]).deleted_at)  # kept, not dropped
        self.worker()
        self.assertEqual(self.fake.datasets, {})
        with self.session_factory() as db:
            audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "knowledge.deleted"))
            self.assertEqual(audit.details["references"], "2")
        # The name is free again.
        self.assertEqual(self.create(headers).status_code, 201)

    def test_deleting_before_the_datasets_exist_leaves_nothing_behind(self) -> None:
        headers = self.login("admin@example.com")
        body = self.create(headers).json()
        self.client.delete(f"/api/v1/knowledge-bases/{body['id']}", headers=headers)
        self.worker()  # the queued creation job finds the knowledge base deleted
        self.assertEqual(self.fake.datasets, {})

    def test_the_deletion_job_retries_until_the_engine_is_reachable(self) -> None:
        headers = self.login("admin@example.com")
        body = self.create(headers).json()
        self.worker()
        self.fake.fail_next = 1
        self.client.delete(f"/api/v1/knowledge-bases/{body['id']}", headers=headers)
        self.worker()
        self.assertEqual(len(self.fake.datasets), 2)  # the injected failure left them in place
        with self.session_factory() as db:
            job = db.scalar(select(Job).where(Job.kind == "knowledge.delete_datasets"))
            self.assertEqual((job.status, job.attempts), ("queued", 1))
            job.run_after = job.created_at
            db.commit()
        self.worker()
        self.assertEqual(self.fake.datasets, {})

    def test_a_knowledge_base_deleted_during_a_rebuild_leaves_no_orphan_datasets(self) -> None:
        headers = self.login("admin@example.com")
        body = self.create(headers).json()
        self.worker()
        self.client.put(f"/api/v1/knowledge-bases/{body['id']}", headers=headers, json=self.settings_payload(body, chunking="fixed_512"))

        def delete_midway(_db, _kb, _new):
            self.client.delete(f"/api/v1/knowledge-bases/{body['id']}", headers=headers)

        knowledge.REINDEX_HOOKS.append(delete_midway)
        try:
            self.worker()  # the rebuild creates generation 2, then finds the knowledge base deleted
            self.worker()  # deletion jobs
        finally:
            knowledge.REINDEX_HOOKS.remove(delete_midway)
        self.assertEqual(self.fake.datasets, {})

    def test_a_knowledge_base_deleted_while_its_datasets_are_created_leaves_none_behind(self) -> None:
        headers = self.login("admin@example.com")
        body = self.create(headers).json()
        original = knowledge._create_or_reuse
        calls = {"n": 0}

        def create_then_delete(client, name, **options):
            result = original(client, name, **options)
            calls["n"] += 1
            if calls["n"] == 2:
                self.client.delete(f"/api/v1/knowledge-bases/{body['id']}", headers=headers)
            return result

        knowledge._create_or_reuse = create_then_delete
        try:
            self.worker()
            self.worker()
        finally:
            knowledge._create_or_reuse = original
        self.assertEqual(self.fake.datasets, {})

    def test_the_status_filter_pages_and_counts_the_filtered_result(self) -> None:
        headers = self.login("admin@example.com")
        for index in range(3):
            self.create(headers, name=f"KB {index}")
        self.provide(documents=1, indexed_documents=1)
        listed = self.client.get("/api/v1/knowledge-bases?status=indexed&limit=2", headers=headers).json()
        self.assertEqual((listed["total"], len(listed["items"])), (3, 2))
        second = self.client.get("/api/v1/knowledge-bases?status=indexed&limit=2&offset=2", headers=headers).json()
        self.assertEqual((second["total"], len(second["items"])), (3, 1))
        self.assertEqual(self.client.get("/api/v1/knowledge-bases?status=draft", headers=headers).json()["total"], 0)

    def test_renaming_to_a_taken_name_is_refused(self) -> None:
        headers = self.login("admin@example.com")
        self.create(headers, name="Alpha")
        beta = self.create(headers, name="Beta").json()
        clash = self.client.put(f"/api/v1/knowledge-bases/{beta['id']}", headers=headers, json=self.settings_payload(beta, name="ALPHA"))
        self.assertEqual((clash.status_code, clash.json()["code"]), (409, "knowledge.name_taken"))
        own = self.client.put(f"/api/v1/knowledge-bases/{beta['id']}", headers=headers, json=self.settings_payload(beta, name="beta"))
        self.assertEqual(own.status_code, 200, own.text)  # changing only the case of its own name is fine

    def test_audit_trail_covers_create_update_and_copy(self) -> None:
        headers = self.login("admin@example.com")
        body = self.create(headers).json()
        self.client.put(f"/api/v1/knowledge-bases/{body['id']}", headers=headers, json=self.settings_payload(body, retrieval_threshold="0.7"))
        self.client.post(f"/api/v1/knowledge-bases/{body['id']}/copy", headers=headers)
        with self.session_factory() as db:
            actions = sorted(db.scalars(select(AuditEvent.action).where(AuditEvent.action.like("knowledge.%"))))
        self.assertEqual(actions, ["knowledge.copied", "knowledge.created", "knowledge.updated"])
        self.assertEqual(Decimal(self.row(body["id"]).retrieval_threshold), Decimal("0.70"))


class DifyTests(_Harness):
    """The same knowledge-base flows against the Dify API (fake), plus the Dify client's own contract."""

    def setUp(self) -> None:
        super().setUp()
        self.configure(True, "dify")

    def dify(self):
        return list(self.fake.dify_datasets.values())

    def test_creation_syncs_two_knowledge_bases_named_within_dify_limits(self) -> None:
        headers = self.login("admin@example.com")
        body = self.create(headers, name="产品知识库").json()
        self.assertEqual((body["engine"], body["sync_status"]), ("dify", "pending"))
        self.worker()
        kb = self.row(body["id"])
        self.assertEqual((kb.sync_status, kb.engine), ("synced", "dify"))
        names = sorted(dataset["name"] for dataset in self.dify())
        self.assertEqual(names, [f"bw-{kb.id.hex[:24]}-g1", f"bw-{kb.id.hex[:24]}-g1-faq"])
        self.assertTrue(all(len(name) <= 40 for name in names))
        self.assertTrue(all(d["indexing_technique"] == "high_quality" for d in self.dify()))
        self.assertEqual(self.fake.datasets, {})  # nothing went to RAGFlow
        self.assertEqual(kb.engine_dataset_id in {d["id"] for d in self.dify()}, True)

    def test_the_embedding_choice_maps_to_a_dify_provider_model_or_the_workspace_default(self) -> None:
        headers = self.login("admin@example.com")
        body = self.create(headers).json()
        self.worker()
        self.assertTrue(all(d["embedding_model"] is None for d in self.dify()))  # bge-m3: workspace default
        saved = self.client.put(
            f"/api/v1/knowledge-bases/{body['id']}", headers=headers,
            json=self.settings_payload(body, embedding_model="text-embedding-3-large"),
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.worker()
        self.worker()
        self.assertEqual(len(self.dify()), 2)  # rebuilt: old generation removed
        self.assertEqual({(d["embedding_model_provider"], d["embedding_model"]) for d in self.dify()}, {("openai", "text-embedding-3-large")})

    def test_rebuild_and_delete_work_and_deleting_is_repeatable(self) -> None:
        headers = self.login("admin@example.com")
        body = self.create(headers).json()
        self.worker()
        original = {d["id"] for d in self.dify()}
        self.client.put(f"/api/v1/knowledge-bases/{body['id']}", headers=headers, json=self.settings_payload(body, chunking="by_heading"))
        self.worker()
        self.worker()
        after = {d["id"] for d in self.dify()}
        self.assertTrue(original.isdisjoint(after) and len(after) == 2)
        # One dataset disappears out-of-band; deleting the knowledge base must still remove the rest.
        gone = next(iter(after))
        del self.fake.dify_datasets[gone]
        self.assertEqual(self.client.delete(f"/api/v1/knowledge-bases/{body['id']}", headers=headers).status_code, 204)
        self.worker()
        self.assertEqual(self.dify(), [])

    def test_a_knowledge_base_keeps_its_engine_when_the_default_changes(self) -> None:
        headers = self.login("admin@example.com")
        body = self.create(headers).json()
        self.worker()
        settings.knowledge_engine = "ragflow"
        settings.ragflow_base_url, settings.ragflow_api_key = self.fake_url, self.fake.API_KEY
        self.client.put(f"/api/v1/knowledge-bases/{body['id']}", headers=headers, json=self.settings_payload(body, chunking="fixed_512"))
        self.worker()
        self.worker()
        self.assertEqual(self.fake.datasets, {})  # still Dify
        self.assertEqual(len(self.dify()), 2)
        other = self.create(headers, name="New one").json()
        self.assertEqual(other["engine"], "ragflow")  # new knowledge bases follow the new default
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{body['id']}", headers=headers).json()["engine"], "dify")

    def test_without_dify_credentials_creation_succeeds_and_reconcile_syncs_it_later(self) -> None:
        self.configure(False, "dify")
        headers = self.login("admin@example.com")
        body = self.create(headers).json()
        self.assertEqual((body["engine"], body["sync_status"]), ("dify", "not_configured"))
        self.configure(True, "dify")
        with self.session_factory() as db:
            knowledge.reconcile_job(db, {})
        self.worker()
        self.assertEqual(self.row(body["id"]).sync_status, "synced")

    def test_the_client_contract(self) -> None:
        from beework.integrations.dify import DifyClient
        from beework.integrations.knowledge_engine import KnowledgeEngineError

        with DifyClient(f"{self.fake_url}/v1/", "wrong-key") as bad, self.assertRaises(KnowledgeEngineError) as caught:
            bad.health()
        self.assertIn("401", str(caught.exception))
        with DifyClient(f"{self.fake_url}/v1", self.fake.API_KEY) as client:
            self.assertTrue(client.health())
            first = client.create_knowledge_dataset("bw-a", faq=False, chunking="semantic", embedding_model="bge-m3")
            client.create_knowledge_dataset("bw-ab", faq=False, chunking="semantic", embedding_model="bge-m3")
            self.assertEqual(client.find_dataset("bw-a"), first)  # exact match, not the keyword-matching "bw-ab"
            self.assertIsNone(client.find_dataset("bw"))
            with self.assertRaises(KnowledgeEngineError) as duplicate:
                client.create_knowledge_dataset("bw-a", faq=False, chunking="semantic", embedding_model="bge-m3")
            self.assertIn("409", str(duplicate.exception))
            with self.assertRaises(KnowledgeEngineError):
                client.create_knowledge_dataset("x" * 41, faq=False, chunking="semantic", embedding_model="bge-m3")
            client.delete_datasets([first, first, "missing"])  # repeatable, missing ids are fine
            self.assertIsNone(client.find_dataset("bw-a"))
            self.fake.fail_next = 1
            with self.assertRaises(KnowledgeEngineError) as injected:
                client.health()
            self.assertIn("500", str(injected.exception))


if __name__ == "__main__":
    unittest.main()
