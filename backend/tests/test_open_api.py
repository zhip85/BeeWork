import shutil
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from beework import channel_chat
from beework.api_keys import generate_key, hash_key
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
from beework.models.channels import TenantApiKey
from beework.models.conversations import Conversation, EndUser
from beework.models.runtime import Run
from beework.ratelimit import Limiter, MemoryStore
from beework.security import hash_password
from beework.worker.runner import run_once
from isolation import disable_external_integrations, stub_public_dns
from test_runtime import ScriptedChat, make_model

PASSWORD = "correct horse battery"


def fake_gateway_llm(_db, run, dsl):
    """Answers with the prompt it was given and the message, so tests can see which configuration ran."""
    return ScriptedChat(chunks=[f"[{dsl.prompt}] ", f"echo: {run.input['message']}"])


class OpenApiTests(unittest.TestCase):
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
        patcher = mock.patch("beework.runtime.executor.gateway_llm", fake_gateway_llm)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Every test gets its own rate-limit buckets (the shared Valkey would carry state from test to test).
        isolated = mock.patch.object(channel_chat, "limiter", Limiter(primary=MemoryStore()))
        isolated.start()
        self.addCleanup(isolated.stop)
        # A background Worker executes the queued runs while a request waits for its reply.
        self._stop = threading.Event()
        thread = threading.Thread(target=self._worker, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self._stop.set(), thread.join(timeout=5)))
        with self.session_factory.begin() as db:
            tenant = Tenant(name="Acme", slug="acme", plan="enterprise", seat_limit=20)
            other = Tenant(name="Other", slug="other", plan="standard", seat_limit=20)
            db.add_all([tenant, other])
            db.flush()
            roots = {t.slug: Organization(tenant_id=t.id, name=t.name) for t in (tenant, other)}
            db.add_all(roots.values())
            db.flush()
            users = {n: User(email=f"{n}@example.com", display_name=n, password_hash=hash_password(PASSWORD))
                     for n in ("admin", "admin2", "viewer", "foreign")}
            db.add_all(users.values())
            db.flush()
            for name, role in (("admin", "tenant_admin"), ("admin2", "tenant_admin"), ("viewer", "viewer")):
                db.add(Membership(tenant_id=tenant.id, user_id=users[name].id, organization_id=roots["acme"].id, role=role))
            db.add(Membership(tenant_id=other.id, user_id=users["foreign"].id, organization_id=roots["other"].id, role="tenant_admin"))
            chat = make_model("Chat Open")
            db.add(chat)
            db.flush()
            for owner in (tenant, other):
                for environment in (("production", "development") if owner is tenant else ("production",)):
                    config = TenantModelEnvironment(tenant_id=owner.id, environment=environment, gateway_sync_status="synced", gateway_key_encrypted="x")
                    db.add(config)
                    db.flush()
                    db.add(TenantModelSelection(environment_id=config.id, model_id=chat.id, model_type="chat", is_default=True))
            ensure_policies(db, tenant.id)["publish"].enabled = False
            ensure_policies(db, other.id)["publish"].enabled = False
            self.ids = {"tenant": tenant.id, "other": other.id, "chat": chat.id, **{n: u.id for n, u in users.items()}}
        self.admin = self.login("admin")
        self.assistant_id = self.make_assistant(self.admin, "Helper", "You are helpful.")
        self.client.post(f"/api/v1/assistants/{self.assistant_id}/publish", json={}, headers=self.admin)

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                run_once("test-worker", self.session_factory, schedule=False)
            except Exception:  # noqa: BLE001 - the test thread must survive teardown races
                pass
            time.sleep(0.05)

    # -- helpers --------------------------------------------------------------------------------------

    def login(self, name: str, tenant: UUID | None = None) -> dict[str, str]:
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/api/v1/auth/login", json={"email": f"{name}@example.com", "password": PASSWORD}).status_code, 200)
        return {"X-CSRF-Token": self.client.cookies.get("beework_csrf"), "X-Tenant-ID": str(tenant or self.ids["tenant"])}

    def make_assistant(self, headers: dict, name: str, prompt: str) -> str:
        created = self.client.post(
            "/api/v1/assistants", json={"name": name, "description": "d", "config": {"prompt": prompt, "greeting": "hi"}}, headers=headers
        )
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()["id"]

    def create_key(self, headers: dict | None = None, **body) -> dict:
        headers = headers or self.admin
        response = self.client.post("/api/v1/tenant/api-keys", json={"name": "ci", **body}, headers=headers)
        self.assertEqual(response.status_code, 201, response.text)
        self.admin = self.login("admin") if headers is self.admin else self.admin
        return response.json()

    def chat(self, key: str, message: str = "hello", assistant: str | None = None, **extra):
        self.client.cookies.clear()
        body = {"message": message, "end_user": {"id": "visitor-1", "name": "Visitor"}, **extra}
        return self.client.post(f"/v1/assistants/{assistant or self.assistant_id}/chat", json=body, headers={"Authorization": f"Bearer {key}"})

    # -- key management -----------------------------------------------------------------------------------

    def test_a_key_is_shown_once_stored_hashed_and_listed_masked(self) -> None:
        created = self.create_key(environment="production", scopes=["chat", "analytics.read"])
        key = created["key"]
        self.assertRegex(key, r"^bw_live_[A-Za-z0-9]{24}$")
        self.assertEqual(created["masked"], f"{key[:8]}••••{key[-4:]}")
        self.assertEqual((created["status"], created["scopes"], created["environment"]), ("active", ["analytics.read", "chat"], "production"))
        listed = self.client.get("/api/v1/tenant/api-keys", headers=self.admin).json()["items"]
        self.assertEqual([i["masked"] for i in listed], [created["masked"]])
        self.assertNotIn("key", listed[0])
        with self.session_factory() as db:
            row = db.scalar(select(TenantApiKey))
            self.assertEqual(row.key_hash, hash_key(key))
            self.assertNotIn(key, f"{row.key_hash}{row.masked}")
            audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "api_key.created"))
            self.assertEqual(audit.category, "developer")
            self.assertNotIn(key, str(audit.details))
        test_key = self.create_key(environment="development")["key"]
        self.assertTrue(test_key.startswith("bw_test_"))

    def test_creating_keys_is_validated_and_permissioned(self) -> None:
        for bad in ({"scopes": []}, {"scopes": ["root"]}, {"name": "  "}, {"environment": "prod"}, {"expires_at": "2020-01-01T00:00:00Z"}):
            response = self.client.post("/api/v1/tenant/api-keys", json={"name": "x", **bad}, headers=self.admin)
            self.assertEqual(response.status_code, 422, bad)
        viewer = self.login("viewer")
        self.assertEqual(self.client.post("/api/v1/tenant/api-keys", json={"name": "x"}, headers=viewer).status_code, 403)
        foreign = self.login("foreign", self.ids["other"])
        denied = self.client.post("/api/v1/tenant/api-keys", json={"name": "x", "environment": "staging"}, headers=foreign)
        self.assertEqual((denied.status_code, denied.json()["code"]), (403, "environment.unavailable"))  # standard plan: production only
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/v1/tenant/api-keys").status_code, 401)

    def test_keys_are_tenant_scoped_for_listing_and_revoking(self) -> None:
        created = self.create_key()
        foreign = self.login("foreign", self.ids["other"])
        self.assertEqual(self.client.get("/api/v1/tenant/api-keys", headers=foreign).json()["items"], [])
        self.assertEqual(self.client.post(f"/api/v1/tenant/api-keys/{created['id']}/revoke", headers=foreign).status_code, 404)

    # -- authentication ----------------------------------------------------------------------------------------

    def test_missing_malformed_and_unknown_keys_get_401_in_the_open_api_envelope(self) -> None:
        self.client.cookies.clear()
        url = f"/v1/assistants/{self.assistant_id}/chat"
        body = {"message": "hi", "end_user": {"id": "u"}}
        missing = self.client.post(url, json=body)
        self.assertEqual((missing.status_code, missing.json()), (401, {"error": {"code": "missing_api_key", "message": missing.json()["error"]["message"]}}))
        for header in ("Bearer nonsense", "Basic abc", "bearer "):
            response = self.client.post(url, json=body, headers={"Authorization": header})
            self.assertEqual(response.status_code, 401, header)
            self.assertIn(response.json()["error"]["code"], ("invalid_api_key", "missing_api_key"))

    def test_revoking_takes_effect_immediately(self) -> None:
        created = self.create_key()
        self.assertEqual(self.chat(created["key"]).status_code, 200)
        revoked = self.client.post(f"/api/v1/tenant/api-keys/{created['id']}/revoke", headers=self.login("admin"))
        self.assertEqual((revoked.status_code, revoked.json()["status"]), (200, "revoked"))
        after = self.chat(created["key"])
        self.assertEqual((after.status_code, after.json()["error"]["code"]), (401, "invalid_api_key"))
        again = self.client.post(f"/api/v1/tenant/api-keys/{created['id']}/revoke", headers=self.login("admin"))
        self.assertEqual(again.status_code, 409)
        with self.session_factory() as db:
            self.assertIsNotNone(db.scalar(select(AuditEvent).where(AuditEvent.action == "api_key.revoked")))

    def test_expired_keys_are_rejected(self) -> None:
        key = self.create_key(expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())["key"]
        self.assertEqual(self.chat(key).status_code, 200)
        with self.session_factory.begin() as db:
            db.scalar(select(TenantApiKey)).expires_at = datetime.now(UTC) - timedelta(seconds=1)
        expired = self.chat(key)
        self.assertEqual((expired.status_code, expired.json()["error"]["code"]), (401, "expired_api_key"))
        listed = self.client.get("/api/v1/tenant/api-keys", headers=self.login("admin")).json()["items"]
        self.assertEqual(listed[0]["status"], "expired")

    def test_a_key_without_the_chat_scope_is_forbidden(self) -> None:
        key = self.create_key(scopes=["analytics.read"])["key"]
        response = self.chat(key)
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "insufficient_scope"))
        messages = self.client.get(f"/v1/conversations/{self.assistant_id}/messages", headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(messages.status_code, 403)

    def test_suspending_the_creator_revokes_their_keys(self) -> None:
        admin2 = self.login("admin2")
        key = self.create_key(headers=admin2)["key"]
        self.assertEqual(self.chat(key).status_code, 200)
        suspended = self.client.patch(f"/api/v1/members/{self.ids['admin2']}/status", json={"status": "suspended"}, headers=self.login("admin"))
        self.assertEqual(suspended.status_code, 200, suspended.text)
        self.assertEqual(self.chat(key).status_code, 401)
        with self.session_factory() as db:
            self.assertEqual(db.scalar(select(TenantApiKey.revoked_reason)), "member_suspended")

    def test_a_suspended_workspace_cannot_use_its_keys(self) -> None:
        key = self.create_key()["key"]
        with self.session_factory.begin() as db:
            db.get(Tenant, self.ids["tenant"]).status = "suspended"
        response = self.chat(key)
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "tenant_unavailable"))

    def test_last_used_is_recorded(self) -> None:
        key = self.create_key()["key"]
        self.assertIsNone(self.client.get("/api/v1/tenant/api-keys", headers=self.login("admin")).json()["items"][0]["last_used_at"])
        self.assertEqual(self.chat(key).status_code, 200)
        self.assertIsNotNone(self.client.get("/api/v1/tenant/api-keys", headers=self.login("admin")).json()["items"][0]["last_used_at"])

    # -- chat ------------------------------------------------------------------------------------------------------

    def test_a_conversation_works_end_to_end_with_the_published_version(self) -> None:
        key = self.create_key()["key"]
        first = self.chat(key, "hello")
        self.assertEqual(first.status_code, 200, first.text)
        body = first.json()
        self.assertEqual(body["message"], {"role": "assistant", "content": "[You are helpful.] echo: hello"})
        self.assertEqual(body["usage"], {"input_tokens": 11, "output_tokens": 5})
        second = self.chat(key, "again", conversation_id=body["conversation_id"])
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["conversation_id"], body["conversation_id"])
        with self.session_factory() as db:
            conversation = db.get(Conversation, UUID(body["conversation_id"]))
            self.assertEqual((conversation.source, conversation.assistant_version, conversation.environment, conversation.turns), ("api", "v1.0", "production", 2))
            end_user = db.get(EndUser, conversation.end_user_id)
            self.assertEqual((end_user.channel_type, end_user.external_id, end_user.name), ("open-api", "visitor-1", "Visitor"))
            runs = list(db.scalars(select(Run)))
            self.assertEqual({(r.source, r.status) for r in runs}, {("api", "succeeded")})
        messages = self.client.get(f"/v1/conversations/{body['conversation_id']}/messages", headers={"Authorization": f"Bearer {key}"}).json()
        self.assertEqual([(m["role"], m["content"]) for m in messages["messages"]][:2], [("user", "hello"), ("assistant", "[You are helpful.] echo: hello")])
        self.assertEqual((len(messages["messages"]), messages["has_more"]), (4, False))

    def test_messages_are_paged(self) -> None:
        key = self.create_key()["key"]
        conversation_id = self.chat(key, "one").json()["conversation_id"]
        self.chat(key, "two", conversation_id=conversation_id)
        auth = {"Authorization": f"Bearer {key}"}
        page = self.client.get(f"/v1/conversations/{conversation_id}/messages?limit=3", headers=auth).json()
        self.assertEqual((len(page["messages"]), page["has_more"]), (3, True))
        rest = self.client.get(f"/v1/conversations/{conversation_id}/messages?after_seq=3", headers=auth).json()
        self.assertEqual([m["content"] for m in rest["messages"]], ["[You are helpful.] echo: two"])
        self.assertEqual(self.client.get(f"/v1/conversations/{conversation_id}/messages?limit=0", headers=auth).status_code, 422)

    def test_streaming_returns_server_sent_events(self) -> None:
        key = self.create_key()["key"]
        self.client.cookies.clear()
        with self.client.stream("POST", f"/v1/assistants/{self.assistant_id}/chat", json={"message": "stream me", "end_user": {"id": "u1"}, "stream": True}, headers={"Authorization": f"Bearer {key}"}) as response:
            self.assertEqual((response.status_code, response.headers["content-type"].split(";")[0]), (200, "text/event-stream"))
            frames = [block for block in response.read().decode().split("\n\n") if block.strip() and not block.startswith(":")]
        names = [block.splitlines()[0].removeprefix("event: ") for block in frames]
        self.assertEqual(names[0], "start")
        self.assertEqual(names[-1], "done")
        self.assertNotIn("step", names)  # internal pipeline events are not exposed
        import json

        deltas = "".join(json.loads(b.splitlines()[1].removeprefix("data: "))["text"] for b in frames if b.startswith("event: delta"))
        done = json.loads(frames[-1].splitlines()[1].removeprefix("data: "))
        self.assertEqual(done["message"]["content"], "[You are helpful.] echo: stream me")
        self.assertIn("echo: stream me", deltas or done["message"]["content"])

    def test_production_serves_the_published_version_and_development_the_latest_draft(self) -> None:
        live_key = self.create_key(environment="production")["key"]
        dev_key = self.create_key(environment="development")["key"]
        current = self.client.get(f"/api/v1/assistants/{self.assistant_id}", headers=self.login("admin")).json()
        edited = self.client.put(
            f"/api/v1/assistants/{self.assistant_id}",
            json={"name": "Helper", "description": "d", "category": current["category"], "visibility": "own_org",
                  "config": {**current["config"], "prompt": "Draft prompt."}, "row_version": current["row_version"]},
            headers=self.login("admin"),
        )
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(self.chat(live_key).json()["message"]["content"], "[You are helpful.] echo: hello")
        self.assertEqual(self.chat(dev_key).json()["message"]["content"], "[Draft prompt.] echo: hello")
        with self.session_factory() as db:
            seen = {(c.environment, c.assistant_version) for c in db.scalars(select(Conversation))}
        self.assertEqual(seen, {("production", "v1.0"), ("development", "draft:1.1")})

    def test_unavailable_assistants_and_other_tenants_are_404_or_409(self) -> None:
        key = self.create_key()["key"]
        draft_only = self.make_assistant(self.login("admin"), "Unpublished", "p")
        response = self.chat(key, assistant=draft_only)
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (409, "assistant_not_published"))
        self.client.post(f"/api/v1/assistants/{self.assistant_id}/disable", headers=self.login("admin"))
        self.assertEqual(self.chat(key).json()["error"]["code"], "assistant_disabled")
        self.client.post(f"/api/v1/assistants/{self.assistant_id}/enable", headers=self.login("admin"))
        self.assertEqual(self.chat(key, assistant="00000000-0000-4000-8000-000000000001").json()["error"]["code"], "assistant_not_found")
        # A key of the other tenant cannot reach this tenant's assistant, nor its conversations.
        foreign_headers = self.login("foreign", self.ids["other"])
        foreign_key = self.create_key(headers=foreign_headers)["key"]
        leak = self.chat(foreign_key)
        self.assertEqual((leak.status_code, leak.json()["error"]["code"]), (404, "assistant_not_found"))
        conversation_id = self.chat(key).json()["conversation_id"]
        stolen = self.client.get(f"/v1/conversations/{conversation_id}/messages", headers={"Authorization": f"Bearer {foreign_key}"})
        self.assertEqual((stolen.status_code, stolen.json()["error"]["code"]), (404, "conversation_not_found"))

    def test_conversations_belong_to_their_end_user_and_do_not_overlap(self) -> None:
        key = self.create_key()["key"]
        conversation_id = self.chat(key).json()["conversation_id"]
        other_user = self.client.post(
            f"/v1/assistants/{self.assistant_id}/chat",
            json={"message": "x", "conversation_id": conversation_id, "end_user": {"id": "someone-else"}},
            headers={"Authorization": f"Bearer {key}"},
        )
        self.assertEqual((other_user.status_code, other_user.json()["error"]["code"]), (404, "conversation_not_found"))
        # Console conversations are invisible to the open API.
        with self.session_factory() as db:
            console = Conversation(tenant_id=self.ids["tenant"], assistant_id=UUID(self.assistant_id), source="debug")
            db.add(console)
            db.commit()
            console_id = console.id
        hidden = self.client.get(f"/v1/conversations/{console_id}/messages", headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(hidden.status_code, 404)

    def test_request_validation_uses_the_open_api_envelope(self) -> None:
        key = self.create_key()["key"]
        for body in ({"end_user": {"id": "u"}}, {"message": "", "end_user": {"id": "u"}}, {"message": "hi"}, {"message": "hi", "end_user": {"id": ""}}):
            response = self.client.post(f"/v1/assistants/{self.assistant_id}/chat", json=body, headers={"Authorization": f"Bearer {key}"})
            self.assertEqual(response.status_code, 422, body)
            self.assertEqual(set(response.json()), {"error"})
            self.assertEqual(response.json()["error"]["code"], "invalid_request")
        unknown = self.client.get("/v1/nothing-here", headers={"Authorization": f"Bearer {key}"})
        self.assertEqual((unknown.status_code, unknown.json()["error"]["code"]), (404, "not_found"))

    def test_a_failing_model_call_is_a_502_with_the_open_api_envelope(self) -> None:
        key = self.create_key()["key"]

        def broken(_db, _run, _dsl):
            return ScriptedChat(error=RuntimeError("upstream down"))

        with mock.patch("beework.runtime.executor.gateway_llm", broken):
            response = self.chat(key)
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (502, "run_failed"))
        self.assertNotIn("upstream down", response.text)  # provider details are not leaked

    # -- rate limiting --------------------------------------------------------------------------------------------------

    def test_a_test_key_cannot_read_or_continue_a_production_conversation_and_vice_versa(self) -> None:
        live_key = self.create_key(environment="production")["key"]
        dev_key = self.create_key(environment="development")["key"]
        live_conversation = self.chat(live_key).json()["conversation_id"]
        dev_conversation = self.chat(dev_key).json()["conversation_id"]
        for key, conversation in ((dev_key, live_conversation), (live_key, dev_conversation)):
            read = self.client.get(f"/v1/conversations/{conversation}/messages", headers={"Authorization": f"Bearer {key}"})
            self.assertEqual(read.status_code, 404)
            cont = self.chat(key, conversation_id=conversation)
            self.assertEqual((cont.status_code, cont.json()["error"]["code"]), (404, "conversation_not_found"))
        own = self.client.get(f"/v1/conversations/{live_conversation}/messages", headers={"Authorization": f"Bearer {live_key}"})
        self.assertEqual(own.status_code, 200)

    def test_when_valkey_is_down_the_limiter_falls_back_quickly_and_stops_retrying_for_a_while(self) -> None:
        import redis

        from beework.ratelimit import Limiter, MemoryStore

        calls = []

        class Broken:
            def take(self, *_a):
                calls.append(1)
                raise redis.ConnectionError("down")

        now = [0.0]
        limiter = Limiter(primary=Broken(), fallback=MemoryStore(clock=lambda: now[0]), clock=lambda: now[0], retry_primary_after=10)
        results = [limiter.check("k", per_second=2).allowed for _ in range(3)]
        self.assertEqual(results, [True, True, False])  # the per-process bucket still enforces the limit
        self.assertEqual(len(calls), 1)  # Valkey was tried once, then skipped
        now[0] += 11
        limiter.check("k", per_second=2)
        self.assertEqual(len(calls), 2)  # and tried again after the back-off

    def test_the_tenant_qps_quota_returns_429_with_retry_after(self) -> None:
        key = self.create_key()["key"]
        now = [100.0]
        limiter = Limiter(primary=MemoryStore(clock=lambda: now[0]))
        with mock.patch.object(channel_chat, "limiter", limiter), mock.patch.object(channel_chat, "quota_limit", return_value=2):
            auth = {"Authorization": f"Bearer {key}"}
            url = "/v1/conversations/00000000-0000-4000-8000-000000000001/messages"
            statuses = [self.client.get(url, headers=auth).status_code for _ in range(4)]
            self.assertEqual(statuses, [404, 404, 429, 429])  # the first two pass the limiter (then 404: no such conversation)
            limited = self.client.get(url, headers=auth)
            self.assertEqual((limited.json()["error"]["code"], limited.headers["Retry-After"]), ("rate_limited", "1"))
            now[0] += 1.0
            self.assertEqual(self.client.get(url, headers=auth).status_code, 404)

    def test_the_bucket_refills_over_time_and_isolates_keys(self) -> None:
        now = [0.0]
        limiter = Limiter(primary=MemoryStore(clock=lambda: now[0]))
        take = lambda key, **kw: limiter.check(key, per_second=2, **kw).allowed  # noqa: E731
        self.assertEqual([take("a"), take("a"), take("a")], [True, True, False])
        self.assertTrue(take("b"))  # another tenant has its own bucket
        denied = limiter.check("a", per_second=2)
        self.assertEqual(denied.retry_after, 1)
        now[0] += 0.5
        self.assertTrue(take("a"))
        now[0] += 10
        self.assertEqual([take("a"), take("a"), take("a")], [True, True, False])  # never more than one second's burst
        self.assertEqual(limiter.check("slow", per_second=0.2).allowed, True)
        self.assertEqual(limiter.check("slow", per_second=0.2).retry_after, 5)

    # -- documentation --------------------------------------------------------------------------------------------------------

    def test_the_open_api_has_its_own_versioned_schema(self) -> None:
        schema = self.client.get("/v1/openapi.json").json()
        self.assertEqual(schema["info"]["title"], "BeeWork Open API")
        self.assertEqual(set(schema["paths"]), {"/v1/assistants/{assistant_id}/chat", "/v1/conversations/{conversation_id}/messages"})
        self.assertEqual(self.client.get("/v1/docs").status_code, 200)
        self.assertEqual(generate_key("production")[0][:8], "bw_live_")


if __name__ == "__main__":
    unittest.main()
