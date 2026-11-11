import shutil
import tempfile
import unittest
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from beework.approvals import ensure_policies
from beework.db import get_db
from beework.main import app
from beework.models import (
    AuditEvent,
    Base,
    Membership,
    Organization,
    Tenant,
    TenantModelEnvironment,
    TenantModelSelection,
    User,
)
from beework.models.assistants import Assistant, AssistantVersion
from beework.models.runtime import Run
from beework.runtime.executor import execute_run
from beework.security import hash_password
from isolation import disable_external_integrations, stub_public_dns
from test_runtime import ScriptedChat, make_model

PASSWORD = "correct horse battery"


class VersionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        disable_external_integrations()
        stub_public_dns()
        cls._db_dir = tempfile.mkdtemp(prefix="beework-test-")
        cls.engine = create_engine(
            f"sqlite:///{cls._db_dir}/test.db", connect_args={"check_same_thread": False, "timeout": 30}
        )

        @event.listens_for(cls.engine, "connect")
        def enable_sqlite_foreign_keys(connection, _record) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        cls.session_factory = sessionmaker(bind=cls.engine, expire_on_commit=False)

        def override_get_db():
            with cls.session_factory() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db

    @classmethod
    def tearDownClass(cls) -> None:
        app.dependency_overrides.clear()
        cls.engine.dispose()
        shutil.rmtree(cls._db_dir, ignore_errors=True)

    def setUp(self) -> None:
        raw = self.engine.raw_connection()
        raw.cursor().execute("PRAGMA foreign_keys=OFF")
        raw.close()
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        self.client = TestClient(app)
        with self.session_factory.begin() as db:
            tenant = Tenant(name="Acme", slug="acme", plan="enterprise", seat_limit=20)
            other = Tenant(name="Other", slug="other", plan="enterprise", seat_limit=20)
            db.add_all([tenant, other])
            db.flush()
            root = Organization(tenant_id=tenant.id, name="Acme")
            other_root = Organization(tenant_id=other.id, name="Other")
            db.add_all([root, other_root])
            db.flush()
            users = {}
            for email in ("admin@example.com", "admin2@example.com", "viewer@example.com", "other@example.com"):
                users[email] = User(email=email, display_name=email.split("@")[0], password_hash=hash_password(PASSWORD))
                db.add(users[email])
            db.flush()
            db.add_all([
                Membership(tenant_id=tenant.id, user_id=users["admin@example.com"].id, organization_id=root.id, role="tenant_admin"),
                Membership(tenant_id=tenant.id, user_id=users["admin2@example.com"].id, organization_id=root.id, role="tenant_admin"),
                Membership(tenant_id=tenant.id, user_id=users["viewer@example.com"].id, organization_id=root.id, role="viewer"),
                Membership(tenant_id=other.id, user_id=users["other@example.com"].id, organization_id=other_root.id, role="tenant_admin"),
            ])
            chat = make_model("Chat Ver")
            db.add(chat)
            db.flush()
            environment = TenantModelEnvironment(
                tenant_id=tenant.id, environment="production", gateway_sync_status="synced", gateway_key_encrypted="x"
            )
            db.add(environment)
            db.flush()
            db.add(TenantModelSelection(environment_id=environment.id, model_id=chat.id, model_type="chat", is_default=True))
            other_assistant = Assistant(tenant_id=other.id, organization_id=other_root.id, name="Foreign", description="d",
                                        draft_config={"prompt": "p", "model_id": str(chat.id)})
            db.add(other_assistant)
            db.flush()
            # Most tests exercise publishing itself; the approval flow tests switch the policy on.
            ensure_policies(db, tenant.id)["publish"].enabled = False
            self.ids = {"tenant": tenant.id, "other": other.id, "chat": chat.id, "environment": environment.id,
                        "other_assistant": other_assistant.id}
        self.admin = self.login("admin@example.com")
        created = self.client.post(
            "/api/v1/assistants",
            json={"name": "Helper", "description": "answers", "config": {"prompt": "You are helpful."}},
            headers=self.admin,
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.assistant = created.json()

    # -- helpers ----------------------------------------------------------------

    def login(self, email: str, tenant: UUID | None = None) -> dict[str, str]:
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).status_code, 200)
        return {"X-CSRF-Token": self.client.cookies.get("beework_csrf"), "X-Tenant-ID": str(tenant or self.ids["tenant"])}

    @property
    def aid(self) -> str:
        return self.assistant["id"]

    def refresh(self) -> dict:
        self.assistant = self.client.get(f"/api/v1/assistants/{self.aid}", headers=self.admin).json()
        return self.assistant

    def edit_draft(self, **config) -> dict:
        current = self.refresh()
        body = {
            "name": config.pop("name", current["name"]), "description": current["description"],
            "category": current["category"], "visibility": "own_org",
            "config": {**current["config"], **config}, "row_version": current["row_version"],
        }
        response = self.client.put(f"/api/v1/assistants/{self.aid}", json=body, headers=self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        self.assistant = response.json()
        return self.assistant

    def publish(self, note: str = "", headers: dict | None = None):
        return self.client.post(f"/api/v1/assistants/{self.aid}/publish", json={"change_note": note}, headers=headers or self.admin)

    def run_assistant(self, target: str, message: str = "hi", llm: ScriptedChat | None = None):
        response = self.client.post(
            f"/api/v1/assistants/{self.aid}/runs", json={"message": message, "target": target}, headers=self.admin
        )
        if response.status_code != 202:
            return response, None
        with self.session_factory() as db:
            execute_run(db, UUID(response.json()["run_id"]), llm_factory=lambda *_: llm or ScriptedChat(chunks=["ok"]))
        return response, llm

    # -- publish ----------------------------------------------------------------

    def test_first_publish_creates_v1_0_and_goes_live(self) -> None:
        before = self.refresh()
        self.assertEqual((before["status"], before["draft_version"], before["live_version_id"]), ("draft", "0.1", None))
        response = self.publish("first release")
        self.assertEqual(response.status_code, 201, response.text)
        version = response.json()
        self.assertEqual((version["version"], version["kind"], version["change_note"], version["is_live"]), ("1.0", "publish", "first release", True))
        self.assertEqual(version["snapshot"]["config"]["prompt"], "You are helpful.")
        self.assertEqual(version["snapshot"]["dsl"]["model_alias"], f"bw-{self.ids['chat']}")
        self.assertEqual(version["snapshot"]["model_id"], str(self.ids["chat"]))
        after = self.refresh()
        self.assertEqual((after["status"], after["live_version_id"], after["draft_version"]), ("published", version["id"], "1.1"))
        self.assertGreater(after["row_version"], before["row_version"])
        with self.session_factory() as db:
            event_row = db.scalar(select(AuditEvent).where(AuditEvent.action == "assistant.published"))
            self.assertEqual((event_row.category, event_row.details["version"]), ("assistants", "1.0"))

    def test_preview_lists_blockers_and_changes(self) -> None:
        preview = self.client.get(f"/api/v1/assistants/{self.aid}/publish-preview", headers=self.admin).json()
        self.assertEqual((preview["next_version"], preview["blockers"], preview["has_changes"], preview["can_publish"]), ("1.0", [], True, True))
        with self.session_factory.begin() as db:
            db.get(TenantModelEnvironment, self.ids["environment"]).gateway_sync_status = "failed"
            db.query(TenantModelSelection).delete()
        blocked = self.client.get(f"/api/v1/assistants/{self.aid}/publish-preview", headers=self.admin).json()
        self.assertEqual({b["code"] for b in blocked["blockers"]}, {"runtime.model_unavailable", "runtime.gateway_not_ready"})
        self.assertFalse(blocked["can_publish"])
        response = self.publish()
        self.assertEqual((response.status_code, response.json()["code"]), (409, "assistant.publish_blocked"))

    def test_incomplete_drafts_cannot_be_published(self) -> None:
        draft = self.client.post("/api/v1/assistants", json={"draft": True}, headers=self.admin).json()
        response = self.client.post(f"/api/v1/assistants/{draft['id']}/publish", json={}, headers=self.admin)
        self.assertEqual((response.status_code, response.json()["code"]), (409, "assistant.publish_blocked"))
        preview = self.client.get(f"/api/v1/assistants/{draft['id']}/publish-preview", headers=self.admin).json()
        codes = {b["code"] for b in preview["blockers"]}
        self.assertTrue({"name_missing", "description_missing"} <= codes)

    def test_publishing_without_changes_is_rejected_and_a_change_makes_v1_1(self) -> None:
        self.assertEqual(self.publish("one").status_code, 201)
        again = self.publish("same")
        self.assertEqual((again.status_code, again.json()["code"]), (409, "assistant.no_changes"))
        self.edit_draft(prompt="You are helpful.\nAlways answer in French.")
        second = self.publish("french")
        self.assertEqual(second.status_code, 201, second.text)
        version = second.json()
        self.assertEqual(version["version"], "1.1")
        diff = {(c["section"], c["field"]): c for c in version["change_diff"]}
        self.assertEqual(set(diff), {("prompt", "prompt")})
        self.assertIn("+Always answer in French.", diff[("prompt", "prompt")]["lines"])

    def test_disabled_and_foreign_and_unauthorised(self) -> None:
        self.assertEqual(self.publish().status_code, 201)
        self.client.post(f"/api/v1/assistants/{self.aid}/disable", headers=self.admin)
        self.edit_draft(prompt="changed")
        disabled = self.publish()
        self.assertEqual((disabled.status_code, disabled.json()["code"]), (409, "assistant.invalid_transition"))
        self.client.post(f"/api/v1/assistants/{self.aid}/enable", headers=self.admin)
        viewer = self.login("viewer@example.com")
        self.assertEqual(self.publish(headers=viewer).status_code, 403)
        self.assertEqual(self.client.get(f"/api/v1/assistants/{self.aid}/versions", headers=viewer).status_code, 200)
        other = self.login("other@example.com", tenant=self.ids["other"])
        for method, path in (
            ("post", f"/api/v1/assistants/{self.aid}/publish"),
            ("get", f"/api/v1/assistants/{self.aid}/versions"),
            ("get", f"/api/v1/assistants/{self.aid}/publish-preview"),
        ):
            kwargs = {"json": {}} if method == "post" else {}
            self.assertEqual(getattr(self.client, method)(path, headers=other, **kwargs).status_code, 404, path)

    # -- immutability and the live snapshot -----------------------------------------

    def test_editing_the_draft_never_changes_the_live_version(self) -> None:
        version = self.publish("v1").json()
        self.edit_draft(prompt="A brand new draft prompt")
        with self.session_factory() as db:
            stored = db.get(AssistantVersion, UUID(version["id"]))
            self.assertEqual(stored.snapshot["config"]["prompt"], "You are helpful.")
        live_llm, draft_llm = ScriptedChat(chunks=["ok"]), ScriptedChat(chunks=["ok"])
        self.run_assistant("live", llm=live_llm)
        self.run_assistant("draft", llm=draft_llm)
        self.assertEqual(live_llm.seen_messages[0][0].content, "You are helpful.")
        self.assertEqual(draft_llm.seen_messages[0][0].content, "A brand new draft prompt")

    def test_versions_cannot_be_updated(self) -> None:
        version = self.publish().json()
        with self.session_factory() as db:
            stored = db.get(AssistantVersion, UUID(version["id"]))
            stored.change_note = "tampered"
            with self.assertRaises(ValueError):
                db.flush()

    def test_live_runs_execute_the_snapshot_with_version_metadata(self) -> None:
        response, _ = self.run_assistant("live")
        self.assertEqual((response.status_code, response.json()["code"]), (409, "runtime.not_published"))
        version = self.publish().json()
        response, _ = self.run_assistant("live")
        self.assertEqual(response.status_code, 202, response.text)
        with self.session_factory() as db:
            run = db.get(Run, UUID(response.json()["run_id"]))
            self.assertEqual((str(run.version_id), run.source, run.draft_row_version, run.status), (version["id"], "test", None, "succeeded"))
            from beework.models.conversations import Conversation

            self.assertEqual(db.get(Conversation, run.conversation_id).assistant_version, "v1.0")
        with self.session_factory.begin() as db:
            db.query(TenantModelSelection).delete()  # the snapshot's model is no longer enabled
        response, _ = self.run_assistant("live")
        self.assertEqual((response.status_code, response.json()["code"]), (409, "runtime.model_unavailable"))

    def test_published_assistants_cannot_be_deleted_and_copies_start_as_drafts(self) -> None:
        self.publish()
        self.assertEqual(self.client.delete(f"/api/v1/assistants/{self.aid}", headers=self.admin).status_code, 409)
        copy = self.client.post(f"/api/v1/assistants/{self.aid}/copy", headers=self.admin).json()
        self.assertEqual((copy["status"], copy["live_version_id"], copy["draft_version"]), ("draft", None, "0.1"))

    # -- list / get / compare ---------------------------------------------------------

    def test_version_list_detail_and_isolation(self) -> None:
        first = self.publish("one").json()
        self.edit_draft(greeting="Hello!")
        second = self.publish("two").json()
        listing = self.client.get(f"/api/v1/assistants/{self.aid}/versions", headers=self.admin).json()
        self.assertEqual([(v["version"], v["is_live"]) for v in listing["items"]], [("1.1", True), ("1.0", False)])
        self.assertEqual((listing["live_version_id"], listing["draft_version"]), (second["id"], "1.2"))
        detail = self.client.get(f"/api/v1/assistants/{self.aid}/versions/{first['id']}", headers=self.admin).json()
        self.assertEqual(detail["snapshot"]["config"]["greeting"], first["snapshot"]["config"]["greeting"])
        other = self.login("other@example.com", tenant=self.ids["other"])
        self.assertEqual(self.client.get(f"/api/v1/assistants/{self.aid}/versions/{first['id']}", headers=other).status_code, 404)
        # A version id from another assistant is not addressable through this assistant.
        admin = self.login("admin@example.com")
        self.assertEqual(
            self.client.get(f"/api/v1/assistants/{self.ids['other_assistant']}/versions/{first['id']}", headers=admin).status_code, 404
        )
        missing = self.client.get(f"/api/v1/assistants/{self.aid}/versions/00000000-0000-0000-0000-000000000000", headers=admin)
        self.assertEqual(missing.status_code, 404)

    def test_compare_versions_and_draft(self) -> None:
        first = self.publish().json()
        self.edit_draft(prompt="You are helpful.\nBe brief.", greeting="Hello", oc={"memory": {"short_term": {"window_rounds": 9}}})
        second = self.publish().json()
        self.edit_draft(prompt="You are helpful.\nBe brief.\nNo emojis.")

        def compare(a: str, b: str):
            return self.client.get(f"/api/v1/assistants/{self.aid}/versions/compare?from={a}&to={b}", headers=self.admin)

        changes = compare(first["id"], second["id"]).json()["changes"]
        fields = {(c["section"], c["field"]) for c in changes}
        self.assertEqual(fields, {("prompt", "prompt"), ("experience", "greeting"), ("oc.memory", "oc.memory.short_term.window_rounds")})
        self.assertEqual([c["to"] for c in changes if c["section"] == "oc.memory"], [9])
        draft_changes = compare(second["id"], "draft").json()["changes"]
        self.assertEqual({(c["section"], c["field"]) for c in draft_changes}, {("prompt", "prompt")})
        self.assertEqual(compare(first["id"], first["id"]).json()["changes"], [])
        self.assertEqual(compare("not-a-uuid", "draft").status_code, 422)
        self.assertEqual(compare("00000000-0000-0000-0000-000000000000", "draft").status_code, 404)

    # -- restore / rollback -----------------------------------------------------------

    def test_restore_overwrites_the_draft_but_not_the_live_version(self) -> None:
        first = self.publish().json()
        self.edit_draft(prompt="second prompt")
        self.publish()
        draft = self.refresh()
        stale = self.client.post(
            f"/api/v1/assistants/{self.aid}/versions/{first['id']}/restore", json={"row_version": 1}, headers=self.admin
        )
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "conflict.stale"))
        restored = self.client.post(
            f"/api/v1/assistants/{self.aid}/versions/{first['id']}/restore", json={"row_version": draft["row_version"]}, headers=self.admin
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()["config"]["prompt"], "You are helpful.")
        self.assertEqual(restored.json()["live_version_id"], draft["live_version_id"])  # still the second version
        self.assertEqual(restored.json()["row_version"], draft["row_version"] + 1)
        with self.session_factory() as db:
            self.assertIsNotNone(db.scalar(select(AuditEvent).where(AuditEvent.action == "assistant.version_restored")))
        viewer = self.login("viewer@example.com")
        self.assertEqual(self.client.post(
            f"/api/v1/assistants/{self.aid}/versions/{first['id']}/restore", json={"row_version": 99}, headers=viewer).status_code, 403)

    def test_restore_fails_when_the_old_name_is_taken(self) -> None:
        first = self.publish().json()
        self.edit_draft(name="Renamed")
        self.client.post("/api/v1/assistants", json={"name": "Helper", "description": "d", "config": {"prompt": "p"}}, headers=self.admin)
        draft = self.refresh()
        response = self.client.post(
            f"/api/v1/assistants/{self.aid}/versions/{first['id']}/restore", json={"row_version": draft["row_version"]}, headers=self.admin
        )
        self.assertEqual((response.status_code, response.json()["code"]), (409, "assistant.name_taken"))

    def test_rollback_creates_a_new_live_version_and_leaves_the_draft(self) -> None:
        first = self.publish("one").json()
        self.edit_draft(prompt="second prompt")
        second = self.publish("two").json()
        self.edit_draft(prompt="third draft, unpublished")
        draft_before = self.refresh()
        response = self.client.post(f"/api/v1/assistants/{self.aid}/versions/{first['id']}/rollback", json={}, headers=self.admin)
        self.assertEqual(response.status_code, 201, response.text)
        rolled = response.json()
        self.assertEqual((rolled["version"], rolled["kind"], rolled["source_version_id"], rolled["is_live"]), ("1.2", "rollback", first["id"], True))
        self.assertEqual(rolled["change_note"], "回滚到 v1.0")
        self.assertEqual(rolled["snapshot"]["config"]["prompt"], "You are helpful.")
        self.assertEqual({(c["section"]) for c in rolled["change_diff"]}, {"prompt"})  # diff against the previous live version
        after = self.refresh()
        self.assertEqual(after["live_version_id"], rolled["id"])
        self.assertEqual(after["config"]["prompt"], draft_before["config"]["prompt"])  # draft untouched
        listing = self.client.get(f"/api/v1/assistants/{self.aid}/versions", headers=self.admin).json()
        self.assertEqual([v["version"] for v in listing["items"]], ["1.2", "1.1", "1.0"])
        self.assertEqual(self.client.get(f"/api/v1/assistants/{self.aid}/versions/{second['id']}", headers=self.admin).json()["is_live"], False)
        # The live run now uses the rolled-back prompt.
        llm = ScriptedChat(chunks=["ok"])
        self.run_assistant("live", llm=llm)
        self.assertEqual(llm.seen_messages[0][0].content, "You are helpful.")
        # Rolling back to the version that is already live is rejected.
        again = self.client.post(f"/api/v1/assistants/{self.aid}/versions/{rolled['id']}/rollback", json={}, headers=self.admin)
        self.assertEqual((again.status_code, again.json()["code"]), (409, "assistant.no_changes"))
        with self.session_factory() as db:
            self.assertIsNotNone(db.scalar(select(AuditEvent).where(AuditEvent.action == "assistant.rolled_back")))

    def test_rollback_is_blocked_when_the_snapshot_model_is_gone_or_assistant_disabled(self) -> None:
        first = self.publish().json()
        self.edit_draft(prompt="changed")
        self.publish()
        with self.session_factory.begin() as db:
            db.query(TenantModelSelection).delete()
        blocked = self.client.post(f"/api/v1/assistants/{self.aid}/versions/{first['id']}/rollback", json={}, headers=self.admin)
        self.assertEqual((blocked.status_code, blocked.json()["code"]), (409, "assistant.publish_blocked"))
        with self.session_factory.begin() as db:
            environment = db.get(TenantModelEnvironment, self.ids["environment"])
            db.add(TenantModelSelection(environment_id=environment.id, model_id=self.ids["chat"], model_type="chat", is_default=True))
        self.client.post(f"/api/v1/assistants/{self.aid}/disable", headers=self.admin)
        disabled = self.client.post(f"/api/v1/assistants/{self.aid}/versions/{first['id']}/rollback", json={}, headers=self.admin)
        self.assertEqual((disabled.status_code, disabled.json()["code"]), (409, "assistant.invalid_transition"))

    # -- publishing through the approval engine (T-024) -----------------------------------

    def require_approval(self, **fields) -> None:
        with self.session_factory.begin() as db:
            policy = ensure_policies(db, self.ids["tenant"])["publish"]
            policy.enabled = True
            for key, value in fields.items():
                setattr(policy, key, value)

    def decide(self, approval_id: str, who: str = "admin2@example.com", action: str = "approve", comment: str = ""):
        headers = self.login(who)
        response = self.client.post(f"/api/v1/approvals/{approval_id}/{action}", json={"comment": comment}, headers=headers)
        self.admin = self.login("admin@example.com")
        return response

    def drain(self) -> int:
        from beework.worker.runner import run_once

        return run_once("test-worker", self.session_factory, schedule=False)

    def user_id(self, email: str) -> UUID:
        with self.session_factory() as db:
            return db.scalar(select(User.id).where(User.email == email))

    def test_publish_waits_for_approval_and_goes_live_only_after_it(self) -> None:
        self.require_approval()
        preview = self.client.get(f"/api/v1/assistants/{self.aid}/publish-preview", headers=self.admin).json()
        self.assertEqual(preview["approval"], {"required": True, "levels": 1, "approver_role": "tenant_admin", "pending_approval_id": None})
        response = self.publish("first release")
        self.assertEqual(response.status_code, 202, response.text)
        pending = response.json()
        self.assertEqual((pending["pending_approval"], pending["next_version"]), (True, "1.0"))
        shown = self.refresh()
        self.assertEqual((shown["status"], shown["live_version_id"], shown["pending_approval_id"]), ("draft", None, pending["approval_id"]))
        self.assertEqual(self.client.get(f"/api/v1/assistants/{self.aid}/versions", headers=self.admin).json()["items"], [])
        approval = self.client.get(f"/api/v1/approvals/{pending['approval_id']}", headers=self.admin).json()
        self.assertEqual((approval["title"], approval["type"], approval["description"]), ("发布 Helper v1.0", "publish", "first release"))
        self.assertEqual({d["section"] for d in approval["change_diff"]} >= {"prompt", "model"}, True)

        self.assertEqual(self.decide(pending["approval_id"]).status_code, 200)
        self.assertEqual(self.refresh()["live_version_id"], None)  # approved, applied by the worker next
        self.assertEqual(self.drain(), 1)
        after = self.refresh()
        self.assertEqual((after["status"], after["pending_approval_id"]), ("published", None))
        version = self.client.get(f"/api/v1/assistants/{self.aid}/versions/{after['live_version_id']}", headers=self.admin).json()
        self.assertEqual((version["version"], version["kind"], version["approval_id"], version["published_by"]), ("1.0", "publish", pending["approval_id"], "admin"))
        with self.session_factory() as db:
            from beework.models.notifications import InAppNotification

            note = db.scalar(select(InAppNotification).where(InAppNotification.type == "assistant.published"))
            self.assertEqual((note.recipient_id, note.link), (self.user_id("admin@example.com"), f"/assistants/{self.aid}/versions"))
            self.assertIsNotNone(db.scalar(select(AuditEvent).where(AuditEvent.action == "approval.applied")))

    def test_what_goes_live_is_the_snapshot_that_was_reviewed(self) -> None:
        self.require_approval()
        pending = self.publish().json()
        self.edit_draft(prompt="Changed after submitting for approval.")
        self.decide(pending["approval_id"])
        self.drain()
        version = self.client.get(f"/api/v1/assistants/{self.aid}/versions/{self.refresh()['live_version_id']}", headers=self.admin).json()
        self.assertEqual(version["snapshot"]["config"]["prompt"], "You are helpful.")
        self.assertEqual(self.refresh()["config"]["prompt"], "Changed after submitting for approval.")  # the draft keeps the edit

    def test_rejection_publishes_nothing_and_keeps_the_draft(self) -> None:
        self.require_approval()
        pending = self.publish().json()
        self.assertEqual(self.decide(pending["approval_id"], action="reject", comment="not ready").status_code, 200)
        self.assertEqual(self.drain(), 0)
        after = self.refresh()
        self.assertEqual((after["status"], after["live_version_id"], after["pending_approval_id"]), ("draft", None, None))
        self.assertEqual(self.publish().status_code, 202)  # it can be submitted again

    def test_second_submission_needs_replace_and_delete_is_blocked_while_pending(self) -> None:
        self.require_approval()
        first = self.publish().json()
        again = self.publish()
        self.assertEqual((again.status_code, again.json()["code"]), (409, "approval.pending_exists"))
        self.assertEqual(self.client.delete(f"/api/v1/assistants/{self.aid}", headers=self.admin).status_code, 409)
        self.edit_draft(prompt="Second attempt.")
        replaced = self.client.post(f"/api/v1/assistants/{self.aid}/publish", json={"replace_pending": True}, headers=self.admin)
        self.assertEqual(replaced.status_code, 202, replaced.text)
        self.assertNotEqual(replaced.json()["approval_id"], first["approval_id"])
        old = self.client.get(f"/api/v1/approvals/{first['approval_id']}", headers=self.admin).json()
        self.assertEqual(old["status"], "withdrawn")

    def test_rollback_goes_through_the_same_approval(self) -> None:
        self.assertEqual(self.publish("one").status_code, 201)  # policy off: direct
        first = self.client.get(f"/api/v1/assistants/{self.aid}/versions", headers=self.admin).json()["items"][0]
        self.edit_draft(prompt="Version two.")
        self.assertEqual(self.publish("two").status_code, 201)
        self.require_approval()
        response = self.client.post(f"/api/v1/assistants/{self.aid}/versions/{first['id']}/rollback", json={}, headers=self.admin)
        self.assertEqual(response.status_code, 202, response.text)
        approval_id = response.json()["approval_id"]
        approval = self.client.get(f"/api/v1/approvals/{approval_id}", headers=self.admin).json()
        self.assertEqual(approval["title"], "回滚 Helper 到 v1.0")
        self.assertEqual(self.refresh()["config"]["prompt"], "Version two.")
        self.decide(approval_id)
        self.drain()
        versions = self.client.get(f"/api/v1/assistants/{self.aid}/versions", headers=self.admin).json()["items"]
        self.assertEqual([(v["version"], v["kind"], v["is_live"]) for v in versions][0], ("1.2", "rollback", True))
        self.assertEqual(versions[0]["approval_id"], approval_id)

    def test_apply_failure_is_visible_and_retry_publishes(self) -> None:
        self.require_approval()
        pending = self.publish().json()
        self.decide(pending["approval_id"])
        with self.session_factory.begin() as db:  # the model is switched off between approval and apply
            db.query(TenantModelSelection).delete()
        self.drain()
        approval = self.client.get(f"/api/v1/approvals/{pending['approval_id']}", headers=self.admin).json()
        self.assertEqual((approval["status"], approval["apply_status"], approval["apply_error"]), ("approved", "failed", "所选模型未在当前环境启用"))
        self.assertEqual(self.refresh()["live_version_id"], None)
        with self.session_factory.begin() as db:
            db.add(TenantModelSelection(environment_id=self.ids["environment"], model_id=self.ids["chat"], model_type="chat", is_default=True))
        retried = self.client.post(f"/api/v1/approvals/{pending['approval_id']}/retry", headers=self.admin)
        self.assertEqual(retried.status_code, 200, retried.text)
        self.drain()
        self.assertEqual(self.refresh()["status"], "published")

    def test_apply_is_refused_for_a_deleted_or_disabled_assistant(self) -> None:
        self.require_approval()
        self.assertEqual(self.publish("one").status_code, 202)
        approval_id = self.refresh()["pending_approval_id"]
        self.decide(approval_id)
        with self.session_factory.begin() as db:
            db.get(Assistant, UUID(self.aid)).status = "disabled"
        self.drain()
        failed = self.client.get(f"/api/v1/approvals/{approval_id}", headers=self.admin).json()
        self.assertEqual((failed["apply_status"], failed["apply_error"]), ("failed", "智能体已停用，请启用后重试"))

    def test_a_lone_admin_cannot_submit_and_the_policy_can_be_turned_off(self) -> None:
        self.require_approval()
        with self.session_factory.begin() as db:
            db.query(Membership).filter(Membership.user_id == self.user_id("admin2@example.com")).delete()
        response = self.publish()
        self.assertEqual((response.status_code, response.json()["code"]), (409, "approval.no_approver"))
        with self.session_factory.begin() as db:
            ensure_policies(db, self.ids["tenant"])["publish"].enabled = False
        self.assertEqual(self.publish().status_code, 201)

    def test_publish_warnings_reach_the_approval_checks(self) -> None:
        self.require_approval()
        pending = self.publish().json()
        approval = self.client.get(f"/api/v1/approvals/{pending['approval_id']}", headers=self.admin).json()
        self.assertEqual([c["label"] for c in approval["checks"]], ["未设置开场白"])
        self.assertEqual(approval["checks"][0]["level"], "warn")

    def test_a_stale_pending_request_cannot_overwrite_a_newer_direct_publish(self) -> None:
        self.require_approval()
        old = self.publish("old").json()  # pending, frozen with the original prompt
        with self.session_factory.begin() as db:
            ensure_policies(db, self.ids["tenant"])["publish"].enabled = False
        self.edit_draft(prompt="Newer prompt published directly.")
        direct = self.publish("direct")
        self.assertEqual(direct.status_code, 201, direct.text)
        # The superseded request was withdrawn, so approving it is no longer possible.
        self.assertEqual(self.decide(old["approval_id"]).status_code, 409)
        self.assertEqual(self.drain(), 0)
        live = self.client.get(f"/api/v1/assistants/{self.aid}/versions/{self.refresh()['live_version_id']}", headers=self.admin).json()
        self.assertEqual(live["snapshot"]["config"]["prompt"], "Newer prompt published directly.")
        approval = self.client.get(f"/api/v1/approvals/{old['approval_id']}", headers=self.admin).json()
        self.assertEqual(approval["status"], "withdrawn")
        with self.session_factory() as db:
            from beework.models.notifications import InAppNotification

            types = set(db.scalars(select(InAppNotification.type).where(InAppNotification.recipient_id == self.user_id("admin@example.com"))))
        self.assertIn("approval.withdrawn", types)

    # -- configuration check and mounts (T-021) -----------------------------------------

    def checks(self, headers: dict | None = None, assistant_id: str | None = None):
        return self.client.get(f"/api/v1/assistants/{assistant_id or self.aid}/checks", headers=headers or self.admin)

    def test_checks_pass_for_a_complete_draft_and_report_info_only(self) -> None:
        body = self.checks().json()
        self.assertEqual((body["blockers"], body["high_impact"]), (0, 0))
        self.assertEqual([issue["code"] for issue in body["issues"]], ["greeting_empty"])
        self.assertEqual(body["issues"][0]["severity"], "info")

    def test_checks_cover_model_prompt_and_environment_rules(self) -> None:
        with self.session_factory.begin() as db:
            assistant = db.get(Assistant, UUID(self.aid))
            assistant.draft_config = {**assistant.draft_config, "prompt": "   ", "greeting": "hello"}
        self.assertEqual({i["code"] for i in self.checks().json()["issues"]}, {"runtime.prompt_missing"})
        with self.session_factory.begin() as db:
            db.query(TenantModelSelection).delete()
            db.get(TenantModelEnvironment, self.ids["environment"]).gateway_sync_status = "failed"
        body = self.checks().json()
        by_code = {i["code"]: i for i in body["issues"]}
        self.assertEqual(set(by_code), {"runtime.prompt_missing", "runtime.model_unavailable", "runtime.gateway_not_ready"})
        self.assertTrue(all(i["severity"] == "blocker" for i in body["issues"]))
        self.assertEqual(body["blockers"], 3)
        self.assertEqual(by_code["runtime.model_unavailable"]["message"], "所选模型未在当前环境启用")

    def test_checks_report_missing_model_and_unnamed_draft(self) -> None:
        draft = self.client.post("/api/v1/assistants", json={"draft": True}, headers=self.admin).json()
        codes = {i["code"] for i in self.checks(assistant_id=draft["id"]).json()["issues"]}
        self.assertTrue({"name_missing", "description_missing", "runtime.prompt_missing"} <= codes)

    def test_stale_mounts_are_high_impact_and_listed_in_red(self) -> None:
        ghost = "00000000-0000-4000-8000-0000000000aa"
        with self.session_factory.begin() as db:
            assistant = db.get(Assistant, UUID(self.aid))
            assistant.draft_config = {**assistant.draft_config, "mounts": {"kb": [ghost]}}
        body = self.checks().json()
        stale = [i for i in body["issues"] if i["code"] == "mount.invalid"]
        self.assertEqual((len(stale), stale[0]["severity"], stale[0]["target"]), (1, "high", "capabilities"))
        self.assertEqual((body["blockers"], body["high_impact"]), (0, 1))
        mounts = self.client.get(f"/api/v1/assistants/{self.aid}/mounts", headers=self.admin).json()
        self.assertEqual(mounts["total"], 1)
        self.assertEqual((mounts["groups"][0]["kind"], mounts["groups"][0]["items"][0]["state"]), ("kb", "deleted"))
        # High-impact findings warn in the publish preview but do not block it.
        preview = self.client.get(f"/api/v1/assistants/{self.aid}/publish-preview", headers=self.admin).json()
        self.assertEqual(preview["blockers"], [])
        self.assertIn("mount.invalid", [w["code"] for w in preview["warnings"]])
        self.assertTrue(preview["can_publish"])

    def test_registered_resolvers_report_disabled_and_available_mounts(self) -> None:
        from beework.capabilities import MOUNT_RESOLVERS, MountItem

        live, off = UUID("00000000-0000-4000-8000-000000000001"), UUID("00000000-0000-4000-8000-000000000002")
        original = MOUNT_RESOLVERS.get("kb")
        MOUNT_RESOLVERS["kb"] = lambda _db, _tenant, ids: {
            live: MountItem(live, "kb", "FAQ", "available"),
            off: MountItem(off, "kb", "Old", "disabled"),
        }
        self.addCleanup(lambda: MOUNT_RESOLVERS.__setitem__("kb", original) if original else MOUNT_RESOLVERS.pop("kb", None))
        with self.session_factory.begin() as db:
            assistant = db.get(Assistant, UUID(self.aid))
            assistant.draft_config = {**assistant.draft_config, "mounts": {"kb": [str(live), str(off)]}}
        mounts = self.client.get(f"/api/v1/assistants/{self.aid}/mounts", headers=self.admin).json()
        self.assertEqual([(i["name"], i["state"]) for i in mounts["groups"][0]["items"]], [("FAQ", "available"), ("Old", "disabled")])
        self.assertEqual(mounts["mountable_kinds"], ["agent", "kb", "mcp", "skill"])  # leaf agents (T-066), MCP (T-072) and Skill (T-074) register their resolvers
        issues = [i for i in self.checks().json()["issues"] if i["code"] == "mount.invalid"]
        self.assertEqual([i["message"] for i in issues], ["挂载的能力“Old”已停用"])

    def test_checks_and_mounts_respect_permissions_and_tenant_isolation(self) -> None:
        viewer = self.login("viewer@example.com")
        self.assertEqual(self.checks(viewer).status_code, 200)
        other = self.login("other@example.com", self.ids["other"])
        for path in ("checks", "mounts"):
            response = self.client.get(f"/api/v1/assistants/{self.aid}/{path}", headers=other)
            self.assertEqual(response.status_code, 404, path)
        self.admin = self.login("admin@example.com")
        self.assertEqual(self.checks(self.admin, str(self.ids["other_assistant"])).status_code, 404)
        self.client.cookies.clear()
        self.assertEqual(self.client.get(f"/api/v1/assistants/{self.aid}/checks").status_code, 401)


if __name__ == "__main__":
    unittest.main()
