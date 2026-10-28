import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from beework import conversations as conv
from beework.compliance import get_policy
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
from beework.models.assistants import Assistant
from beework.models.conversations import Conversation, EndUser, Message, TenantCompliancePolicy
from beework.models.jobs import RunEvent
from beework.models.runtime import Run
from beework.pii import redact_data, redact_text
from beework.runtime.executor import execute_run
from beework.security import hash_password
from isolation import disable_external_integrations, stub_public_dns
from test_runtime import ScriptedChat, make_model

PASSWORD = "correct horse battery"


class PiiTests(unittest.TestCase):
    def test_redacts_phone_email_id_and_bank_card(self) -> None:
        text = "call 13812345678 or +86 13987654321, mail a.b+c@example.com, id 11010519491231002X, card 4111111111111111."
        redacted = redact_text(text)
        for secret in ("13812345678", "13987654321", "a.b+c@example.com", "11010519491231002X", "4111111111111111"):
            self.assertNotIn(secret, redacted)
        for label in ("[手机号]", "[邮箱]", "[身份证号]", "[银行卡号]"):
            self.assertIn(label, redacted)

    def test_leaves_ordinary_numbers_alone(self) -> None:
        text = "order 20261028 total 1234567890123 (not a card) call 4001234567 at 12:30"
        self.assertEqual(redact_text(text), text)

    def test_redact_data_walks_nested_structures(self) -> None:
        data = {"a": ["13812345678", {"b": "x@y.com"}], "n": 5}
        self.assertEqual(redact_data(data), {"a": ["[手机号]", {"b": "[邮箱]"}], "n": 5})


class ConversationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        disable_external_integrations()
        stub_public_dns()
        # A file database with one connection per session: graph nodes use their own sessions in worker
        # threads, which a shared in-memory connection (StaticPool) cannot serve safely.
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
        self._hooks = list(conv.CONVERSATION_ENDED_HOOKS)
        with self.session_factory.begin() as db:
            tenant = Tenant(name="Acme", slug="acme", plan="enterprise", seat_limit=20)
            standard = Tenant(name="Std", slug="std", plan="standard", seat_limit=20)
            other = Tenant(name="Other", slug="other", plan="enterprise", seat_limit=20)
            db.add_all([tenant, standard, other])
            db.flush()
            roots = {}
            for t in (tenant, standard, other):
                roots[t.id] = Organization(tenant_id=t.id, name=t.name)
                db.add(roots[t.id])
            db.flush()
            users = {}
            for email in ("admin@example.com", "viewer@example.com", "other@example.com", "std@example.com", "tester2@example.com"):
                users[email] = User(email=email, display_name=email.split("@")[0], password_hash=hash_password(PASSWORD))
                db.add(users[email])
            db.flush()
            db.add_all([
                Membership(tenant_id=tenant.id, user_id=users["admin@example.com"].id, organization_id=roots[tenant.id].id, role="tenant_admin"),
                Membership(tenant_id=tenant.id, user_id=users["viewer@example.com"].id, organization_id=roots[tenant.id].id, role="viewer"),
                Membership(tenant_id=tenant.id, user_id=users["tester2@example.com"].id, organization_id=roots[tenant.id].id, role="tenant_admin"),
                Membership(tenant_id=other.id, user_id=users["other@example.com"].id, organization_id=roots[other.id].id, role="tenant_admin"),
                Membership(tenant_id=standard.id, user_id=users["std@example.com"].id, organization_id=roots[standard.id].id, role="tenant_admin"),
            ])
            chat = make_model("Chat Conv")
            db.add(chat)
            db.flush()
            environment = TenantModelEnvironment(
                tenant_id=tenant.id, environment="production", gateway_sync_status="synced", gateway_key_encrypted="x"
            )
            db.add(environment)
            db.flush()
            db.add(TenantModelSelection(environment_id=environment.id, model_id=chat.id, model_type="chat", is_default=True))
            assistant = Assistant(
                tenant_id=tenant.id, organization_id=roots[tenant.id].id, name="Helper", description="d",
                draft_config={"prompt": "You are helpful.", "model_id": str(chat.id)},
            )
            db.add(assistant)
            db.flush()
            self.ids = {
                "tenant": tenant.id, "standard": standard.id, "other": other.id,
                "assistant": assistant.id, "admin": users["admin@example.com"].id,
            }

    def tearDown(self) -> None:
        conv.CONVERSATION_ENDED_HOOKS[:] = self._hooks

    # -- helpers ----------------------------------------------------------------

    def login(self, email: str, tenant: UUID | None = None) -> dict[str, str]:
        self.client.cookies.clear()
        response = self.client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return {"X-CSRF-Token": self.client.cookies.get("beework_csrf"), "X-Tenant-ID": str(tenant or self.ids["tenant"])}

    def say(self, headers: dict, message: str, conversation_id: str | None = None, reply: list[str] | None = None, llm=None):
        body = {"message": message}
        if conversation_id:
            body["conversation_id"] = conversation_id
        response = self.client.post(f"/api/v1/assistants/{self.ids['assistant']}/runs", json=body, headers=headers)
        self.assertEqual(response.status_code, 202, response.text)
        data = response.json()
        with self.session_factory() as db:
            execute_run(db, UUID(data["run_id"]), llm_factory=lambda _r, _d: llm or ScriptedChat(chunks=reply or ["ok"]))
        return data

    def set_policy(self, **values) -> None:
        with self.session_factory.begin() as db:
            row = db.scalar(select(TenantCompliancePolicy).where(TenantCompliancePolicy.tenant_id == self.ids["tenant"]))
            if row is None:
                row = TenantCompliancePolicy(tenant_id=self.ids["tenant"])
                db.add(row)
            for key, value in values.items():
                setattr(row, key, value)

    def messages(self, conversation_id: str) -> list[Message]:
        with self.session_factory() as db:
            return list(db.scalars(select(Message).where(Message.conversation_id == UUID(conversation_id)).order_by(Message.seq)))

    # -- conversations & history ------------------------------------------------

    def test_first_message_starts_a_conversation_and_reply_is_stored(self) -> None:
        headers = self.login("admin@example.com")
        started = self.say(headers, "hello", reply=["Hi ", "there"])
        messages = self.messages(started["conversation_id"])
        self.assertEqual([(m.seq, m.role, m.content) for m in messages], [(1, "user", "hello"), (2, "assistant", "Hi there")])
        self.assertEqual(messages[1].run_id, UUID(started["run_id"]))
        with self.session_factory() as db:
            conversation = db.get(Conversation, UUID(started["conversation_id"]))
            self.assertEqual((conversation.turns, conversation.status, conversation.tokens, conversation.source), (1, "in_progress", 16, "debug"))
            end_user = db.get(EndUser, conversation.end_user_id)
            self.assertEqual((end_user.channel_type, end_user.external_id), ("console", str(self.ids["admin"])))

    def test_follow_up_sends_previous_turns_as_history(self) -> None:
        headers = self.login("admin@example.com")
        first = self.say(headers, "my name is Ada", reply=["Nice to meet you"])
        llm = ScriptedChat(chunks=["Ada"])
        self.say(headers, "what is my name?", conversation_id=first["conversation_id"], llm=llm)
        sent = [(type(m).__name__, m.content) for m in llm.seen_messages[0]]
        self.assertEqual(
            sent[1:],
            [("HumanMessage", "my name is Ada"), ("AIMessageChunk", "Nice to meet you"), ("HumanMessage", "what is my name?")],
        )
        self.assertEqual(len(self.messages(first["conversation_id"])), 4)

    def test_history_window_is_limited(self) -> None:
        headers = self.login("admin@example.com")
        first = self.say(headers, "m1")
        for index in range(2, 5):
            self.say(headers, f"m{index}", conversation_id=first["conversation_id"])
        with self.session_factory.begin() as db:
            run = db.scalar(select(Run).where(Run.conversation_id == UUID(first["conversation_id"])).order_by(Run.created_at.desc()))
            window = conv.history_for_run(db, run, 3)
        self.assertEqual([m["content"] for m in window], ["ok", "m3", "ok"])  # the 3 messages right before m4

    def test_conversation_can_only_be_continued_by_its_creator_for_the_same_assistant(self) -> None:
        headers = self.login("admin@example.com")
        started = self.say(headers, "hi")
        other_user = self.login("tester2@example.com")
        denied = self.client.post(
            f"/api/v1/assistants/{self.ids['assistant']}/runs",
            json={"message": "x", "conversation_id": started["conversation_id"]}, headers=other_user,
        )
        self.assertEqual(denied.status_code, 404)
        missing = self.client.post(
            f"/api/v1/assistants/{self.ids['assistant']}/runs",
            json={"message": "x", "conversation_id": "00000000-0000-0000-0000-000000000000"}, headers=other_user,
        )
        self.assertEqual(missing.status_code, 404)

    def test_busy_conversation_rejects_a_second_message(self) -> None:
        headers = self.login("admin@example.com")
        first = self.client.post(
            f"/api/v1/assistants/{self.ids['assistant']}/runs", json={"message": "one"}, headers=headers
        ).json()
        again = self.client.post(
            f"/api/v1/assistants/{self.ids['assistant']}/runs",
            json={"message": "two", "conversation_id": first["conversation_id"]}, headers=headers,
        )
        self.assertEqual((again.status_code, again.json()["code"]), (409, "conversation.busy"))

    def test_failed_run_keeps_user_message_but_adds_no_reply(self) -> None:
        headers = self.login("admin@example.com")
        started = self.say(headers, "hi", llm=ScriptedChat(chunks=["x"], error=ValueError("boom")))
        self.assertEqual([m.role for m in self.messages(started["conversation_id"])], ["user"])

    # -- redaction ----------------------------------------------------------------

    def test_default_policy_redacts_messages_runs_and_events(self) -> None:
        headers = self.login("admin@example.com")
        started = self.say(headers, "my phone is 13812345678", reply=["Noted 13812345678"])
        stored = [m.content for m in self.messages(started["conversation_id"])]
        self.assertEqual(stored, ["my phone is [手机号]", "Noted [手机号]"])
        with self.session_factory() as db:
            run = db.get(Run, UUID(started["run_id"]))
            self.assertEqual(run.input["message"], "my phone is [手机号]")
            self.assertEqual(run.output["text"], "Noted [手机号]")
            events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run.id)))
            self.assertNotIn("13812345678", str([e.data for e in events]))

    def test_disabled_redaction_stores_original_text(self) -> None:
        self.set_policy(redact_pii=False)
        headers = self.login("admin@example.com")
        started = self.say(headers, "my phone is 13812345678", reply=["Noted 13812345678"])
        self.assertEqual(
            [m.content for m in self.messages(started["conversation_id"])],
            ["my phone is 13812345678", "Noted 13812345678"],
        )
        with self.session_factory() as db:
            tokens = list(db.scalars(select(RunEvent).where(RunEvent.run_id == UUID(started["run_id"]), RunEvent.type == "token")))
            self.assertTrue(tokens)  # raw tokens are kept when redaction is off

    # -- compliance policy --------------------------------------------------------

    def test_compliance_defaults_are_clamped_to_the_plan(self) -> None:
        with self.session_factory() as db:
            self.assertEqual(get_policy(db, self.ids["tenant"]).retention_days, 180)
            standard = get_policy(db, self.ids["standard"])
            self.assertEqual((standard.retention_days, standard.max_retention_days), (30, 30))

    def test_compliance_api_validation_permissions_and_audit(self) -> None:
        admin = self.login("admin@example.com")
        current = self.client.get("/api/v1/tenant/compliance", headers=admin).json()
        self.assertEqual(
            (current["retention_days"], current["redact_pii"], current["allow_export"], current["row_version"]),
            (180, True, True, 0),
        )
        self.assertEqual(current["retention_choices"], [30, 90, 180, 365])
        body = {"retention_days": 90, "redact_pii": False, "allow_export": False, "row_version": 0}
        self.assertEqual(self.client.put("/api/v1/tenant/compliance", json={**body, "retention_days": 45}, headers=admin).status_code, 422)
        saved = self.client.put("/api/v1/tenant/compliance", json=body, headers=admin)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual((saved.json()["retention_days"], saved.json()["row_version"]), (90, 1))
        stale = self.client.put("/api/v1/tenant/compliance", json=body, headers=admin)
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "conflict.stale"))
        again = self.client.put("/api/v1/tenant/compliance", json={**body, "row_version": 1, "redact_pii": True}, headers=admin)
        self.assertEqual(again.status_code, 200)
        with self.session_factory() as db:
            events = list(db.scalars(select(AuditEvent).where(AuditEvent.action == "compliance.updated")))
            self.assertEqual(len(events), 2)
            self.assertEqual((events[0].details["from_retention_days"], events[0].details["retention_days"]), ("180", "90"))
        viewer = self.login("viewer@example.com")
        self.assertEqual(self.client.put("/api/v1/tenant/compliance", json={**body, "row_version": 2}, headers=viewer).status_code, 403)
        self.assertEqual(self.client.get("/api/v1/tenant/compliance", headers=viewer).status_code, 403)  # settings:view

    def test_retention_cannot_exceed_plan_limit(self) -> None:
        std = self.login("std@example.com", tenant=self.ids["standard"])
        view = self.client.get("/api/v1/tenant/compliance", headers=std).json()
        self.assertEqual((view["retention_choices"], view["max_retention_days"]), ([30], 30))
        too_long = self.client.put(
            "/api/v1/tenant/compliance",
            json={"retention_days": 90, "redact_pii": True, "allow_export": True, "row_version": 0}, headers=std,
        )
        self.assertEqual((too_long.status_code, too_long.json()["code"]), (422, "compliance.retention_invalid"))

    def test_plan_cap_wins_over_a_stored_longer_retention(self) -> None:
        with self.session_factory.begin() as db:
            db.add(TenantCompliancePolicy(tenant_id=self.ids["standard"], retention_days=365))
        with self.session_factory() as db:
            self.assertEqual(get_policy(db, self.ids["standard"]).retention_days, 30)

    # -- conversations API --------------------------------------------------------

    def test_list_and_detail_permissions_and_isolation(self) -> None:
        admin = self.login("admin@example.com")
        started = self.say(admin, "hello")
        listing = self.client.get(f"/api/v1/assistants/{self.ids['assistant']}/conversations", headers=admin)
        self.assertEqual(listing.status_code, 200, listing.text)
        item = listing.json()["items"][0]
        self.assertEqual((item["id"], item["turns"], item["status"], item["channel"]), (started["conversation_id"], 1, "in_progress", "console"))
        detail = self.client.get(f"/api/v1/conversations/{started['conversation_id']}", headers=admin).json()
        self.assertEqual([m["role"] for m in detail["messages"]], ["user", "assistant"])
        self.assertEqual(
            self.client.get(f"/api/v1/assistants/{self.ids['assistant']}/conversations?status=resolved", headers=admin).json()["total"], 0
        )
        viewer = self.login("viewer@example.com")
        self.assertEqual(self.client.get(f"/api/v1/conversations/{started['conversation_id']}", headers=viewer).status_code, 200)
        self.client.cookies.clear()
        self.assertEqual(self.client.get(f"/api/v1/conversations/{started['conversation_id']}").status_code, 401)
        other = self.login("other@example.com", tenant=self.ids["other"])
        self.assertEqual(self.client.get(f"/api/v1/conversations/{started['conversation_id']}", headers=other).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/assistants/{self.ids['assistant']}/conversations", headers=other).status_code, 404)

    def test_a_deleted_assistants_conversations_and_runs_are_not_reachable(self) -> None:
        admin = self.login("admin@example.com")
        started = self.say(admin, "hello")
        with self.session_factory.begin() as db:
            from datetime import UTC as _utc

            db.get(Assistant, self.ids["assistant"]).deleted_at = datetime.now(_utc)
        self.assertEqual(self.client.get(f"/api/v1/conversations/{started['conversation_id']}", headers=admin).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/runs/{started['run_id']}", headers=admin).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/runs/{started['run_id']}/events", headers=admin).status_code, 404)

    # -- ending & retention -------------------------------------------------------

    def test_inactive_conversations_end_with_the_right_status_and_fire_hooks(self) -> None:
        headers = self.login("admin@example.com")
        answered = self.say(headers, "hi")
        unanswered = self.say(headers, "hello?", llm=ScriptedChat(chunks=["x"], error=ValueError("boom")))
        fresh = self.say(headers, "still here")
        ended: list[UUID] = []
        conv.CONVERSATION_ENDED_HOOKS.append(lambda _db, c: ended.append(c.id))
        later = datetime.now(UTC) + timedelta(minutes=31)
        with self.session_factory() as db:
            db.get(Conversation, UUID(fresh["conversation_id"])).last_active_at = datetime.now(UTC) + timedelta(minutes=10)
            db.commit()
            self.assertEqual(conv.end_inactive_conversations(db, later), 2)
        with self.session_factory() as db:
            statuses = {
                key: (db.get(Conversation, UUID(value["conversation_id"])).status, db.get(Conversation, UUID(value["conversation_id"])).ended_at is not None)
                for key, value in (("answered", answered), ("unanswered", unanswered), ("fresh", fresh))
            }
        self.assertEqual(statuses["answered"], ("resolved", True))
        self.assertEqual(statuses["unanswered"], ("abandoned", True))
        self.assertEqual(statuses["fresh"], ("in_progress", False))
        self.assertEqual(set(ended), {UUID(answered["conversation_id"]), UUID(unanswered["conversation_id"])})

    def test_conversation_with_a_run_in_flight_is_not_ended(self) -> None:
        headers = self.login("admin@example.com")
        queued = self.client.post(f"/api/v1/assistants/{self.ids['assistant']}/runs", json={"message": "slow"}, headers=headers).json()
        with self.session_factory() as db:
            self.assertEqual(conv.end_inactive_conversations(db, datetime.now(UTC) + timedelta(hours=2)), 0)
            self.assertEqual(db.get(Conversation, UUID(queued["conversation_id"])).status, "in_progress")

    def test_new_message_reopens_an_ended_conversation(self) -> None:
        headers = self.login("admin@example.com")
        started = self.say(headers, "hi")
        with self.session_factory() as db:
            conv.end_inactive_conversations(db, datetime.now(UTC) + timedelta(hours=1))
        self.say(headers, "back again", conversation_id=started["conversation_id"])
        with self.session_factory() as db:
            conversation = db.get(Conversation, UUID(started["conversation_id"]))
            self.assertEqual((conversation.status, conversation.ended_at, conversation.turns), ("in_progress", None, 2))

    def test_retention_purges_expired_conversations_per_tenant_policy(self) -> None:
        self.set_policy(retention_days=30)
        headers = self.login("admin@example.com")
        old = self.say(headers, "old one")
        new = self.say(headers, "new one")
        now = datetime.now(UTC)
        with self.session_factory.begin() as db:
            db.get(Conversation, UUID(old["conversation_id"])).last_active_at = now - timedelta(days=31)
            db.get(Conversation, UUID(new["conversation_id"])).last_active_at = now - timedelta(days=29)
        with self.session_factory() as db:
            self.assertEqual(conv.purge_expired_conversations(db, now), {self.ids["tenant"]: 1})
        with self.session_factory() as db:
            self.assertIsNone(db.get(Conversation, UUID(old["conversation_id"])))
            self.assertIsNotNone(db.get(Conversation, UUID(new["conversation_id"])))
            self.assertIsNone(db.get(Run, UUID(old["run_id"])))
            self.assertEqual(db.scalar(select(Message).where(Message.conversation_id == UUID(old["conversation_id"]))), None)
            self.assertEqual(list(db.scalars(select(RunEvent).where(RunEvent.run_id == UUID(old["run_id"])))), [])
            audit = list(db.scalars(select(AuditEvent).where(AuditEvent.action == "conversation.retention_purged")))
            self.assertEqual([(a.tenant_id, a.details["conversations"]) for a in audit], [(self.ids["tenant"], "1")])
            self.assertEqual(conv.purge_expired_conversations(db, now), {})  # nothing left, no second audit row


if __name__ == "__main__":
    unittest.main()
