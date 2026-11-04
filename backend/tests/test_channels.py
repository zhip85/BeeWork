import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from beework import channel_chat
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
from beework.models.conversations import Conversation, EndUser
from beework.ratelimit import Limiter, MemoryStore
from beework.security import hash_password
from beework.worker.runner import run_once
from isolation import disable_external_integrations, stub_public_dns
from test_runtime import ScriptedChat, make_model

PASSWORD = "correct horse battery"


def fake_gateway_llm(_db, run, dsl):
    """Answers with the prompt it was given and the message, so tests can see which configuration ran."""
    return ScriptedChat(chunks=[f"[{dsl.prompt}] ", f"echo: {run.input['message']}"])


class ChannelBase(unittest.TestCase):
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
            for owner in (tenant, other):  # the tests switch the approval policies on where they need them
                ensure_policies(db, owner.id)["publish"].enabled = False
                ensure_policies(db, owner.id)["channel"].enabled = False
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



class ChannelTests(ChannelBase):
    ORIGIN = "https://www.example.com"

    # -- helpers ------------------------------------------------------------------------------------------------

    def make_channel(self, headers: dict | None = None, **fields) -> dict:
        headers = headers or self.admin
        body = {"name": "Website", "assistant_id": self.assistant_id, "environment": "production",
                "allowed_origins": [self.ORIGIN], "experience": {}, **fields}
        response = self.client.post("/api/v1/channels", json=body, headers=headers)
        self.assertEqual(response.status_code, 201, response.text)
        self.admin = self.login("admin") if headers is self.admin else self.admin
        return response.json()

    def publish_channel(self, channel_id: str, headers: dict | None = None):
        headers = headers or self.admin
        response = self.client.post(f"/api/v1/channels/{channel_id}/publish", json={}, headers=headers)
        self.admin = self.login("admin") if headers is self.admin else self.admin
        return response

    def live_channel(self, **fields) -> dict:
        channel = self.make_channel(**fields)
        published = self.publish_channel(channel["id"])
        self.assertEqual(published.status_code, 200, published.text)
        return published.json()

    def session(self, site_key: str, origin: str | None = ORIGIN, **body):
        self.client.cookies.clear()
        headers = {"Origin": origin} if origin else {}
        return self.client.post("/v1/widget/session", json={"site_key": site_key, **body}, headers=headers)

    def widget_chat(self, token: str, message: str = "hello", origin: str = ORIGIN, **extra):
        self.client.cookies.clear()
        return self.client.post("/v1/widget/chat", json={"message": message, **extra}, headers={"Authorization": f"Bearer {token}", "Origin": origin})

    def open_widget(self, channel: dict | None = None, origin: str = ORIGIN, **body) -> tuple[dict, dict]:
        channel = channel or self.live_channel()
        response = self.session(channel["site_key"], origin, **body)
        self.assertEqual(response.status_code, 200, response.text)
        return channel, response.json()

    def require_approval(self) -> None:
        with self.session_factory.begin() as db:
            ensure_policies(db, self.ids["tenant"])["channel"].enabled = True

    def drain(self) -> int:
        return run_once("t", self.session_factory, schedule=False)

    # -- management -------------------------------------------------------------------------------------------------

    def test_creating_a_draft_channel_normalises_origins_and_never_exposes_embed_code(self) -> None:
        channel = self.make_channel(allowed_origins=["HTTPS://Www.Example.com:443/", "https://www.example.com", "http://localhost:3000"],
                                    experience={"primary_color": "#AABBCC", "welcome": "  Hello!  "})
        self.assertEqual(channel["allowed_origins"], ["https://www.example.com", "http://localhost:3000"])
        self.assertEqual((channel["status"], channel["published_version"], channel["embed_code"], channel["has_unpublished_changes"]), ("draft", "—", None, False))
        self.assertRegex(channel["site_key"], r"^bw_site_[a-z0-9]{20}$")
        self.assertEqual(channel["experience"], {"primary_color": "#aabbcc", "assistant_name": "", "welcome": "Hello!"})
        self.assertEqual(channel["effective_experience"], {"primary_color": "#aabbcc", "assistant_name": "Helper", "welcome": "Hello!"})
        with self.session_factory() as db:
            audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "channel.created"))
            self.assertEqual(audit.category, "assistants")

    def test_channel_input_is_validated(self) -> None:
        for bad in ({"name": "  "}, {"allowed_origins": ["ftp://x.example.com"]}, {"allowed_origins": ["http://example.com"]},
                    {"allowed_origins": ["https://example.com/path"]}, {"allowed_origins": ["https://u:p@example.com"]},
                    {"allowed_origins": ["https://a.example.com"] * 21 + [f"https://s{i}.example.com" for i in range(21)]},
                    {"experience": {"primary_color": "red"}}, {"environment": "prod"}):
            body = {"name": "X", "assistant_id": self.assistant_id, "allowed_origins": [self.ORIGIN], **bad}
            self.assertEqual(self.client.post("/api/v1/channels", json=body, headers=self.admin).status_code, 422, bad)
        self.make_channel(name="Website")
        duplicate = self.client.post("/api/v1/channels", json={"name": "WEBSITE", "assistant_id": self.assistant_id}, headers=self.admin)
        self.assertEqual((duplicate.status_code, duplicate.json()["code"]), (409, "channel.name_taken"))

    def test_plan_and_tenant_rules_apply_to_the_binding(self) -> None:
        foreign = self.login("foreign", self.ids["other"])
        foreign_assistant = self.make_assistant(foreign, "Theirs", "p")
        denied = self.client.post("/api/v1/channels", json={"name": "S", "assistant_id": foreign_assistant, "environment": "staging"}, headers=foreign)
        self.assertEqual((denied.status_code, denied.json()["code"]), (403, "environment.unavailable"))  # standard plan: production only
        # An assistant of another tenant cannot be bound.
        stolen = self.client.post("/api/v1/channels", json={"name": "S", "assistant_id": foreign_assistant}, headers=self.login("admin"))
        self.assertEqual(stolen.status_code, 404)

    def test_permissions_and_tenant_isolation(self) -> None:
        channel = self.make_channel()
        viewer = self.login("viewer")
        self.assertEqual(self.client.get("/api/v1/channels", headers=viewer).status_code, 200)
        self.assertEqual(self.client.post("/api/v1/channels", json={"name": "N", "assistant_id": self.assistant_id}, headers=viewer).status_code, 403)
        for method, suffix in (("post", "publish"), ("post", "disable"), ("post", "rotate-key"), ("delete", "")):
            response = getattr(self.client, method)(f"/api/v1/channels/{channel['id']}/{suffix}".rstrip("/"), headers=viewer, **({"json": {}} if method == "post" else {}))
            self.assertEqual(response.status_code, 403, suffix)
        foreign = self.login("foreign", self.ids["other"])
        self.assertEqual(self.client.get("/api/v1/channels", headers=foreign).json()["items"], [])
        for method, suffix in (("get", ""), ("put", ""), ("post", "/publish"), ("post", "/enable"), ("delete", "")):
            kwargs = {"json": {"name": "N", "assistant_id": self.assistant_id}} if method == "put" else ({"json": {}} if method == "post" else {})
            self.assertEqual(getattr(self.client, method)(f"/api/v1/channels/{channel['id']}{suffix}", headers=foreign, **kwargs).status_code, 404, (method, suffix))

    def test_list_filters_and_assistant_binding(self) -> None:
        other_assistant = self.make_assistant(self.admin, "Second", "p")
        self.admin = self.login("admin")
        self.make_channel(name="Main site")
        self.make_channel(name="Docs site", assistant_id=other_assistant)
        self.assertEqual([c["name"] for c in self.client.get(f"/api/v1/channels?assistant_id={other_assistant}", headers=self.admin).json()["items"]], ["Docs site"])
        self.assertEqual([c["name"] for c in self.client.get("/api/v1/channels?q=MAIN", headers=self.admin).json()["items"]], ["Main site"])
        # An assistant that still has channels cannot be deleted.
        blocked = self.client.delete(f"/api/v1/assistants/{other_assistant}", headers=self.admin)
        self.assertEqual((blocked.status_code, blocked.json()["code"]), (409, "assistant.delete_blocked"))

    def test_update_edits_the_draft_with_optimistic_locking(self) -> None:
        channel = self.make_channel()
        url = f"/api/v1/channels/{channel['id']}"
        body = {"name": "Renamed", "assistant_id": self.assistant_id, "allowed_origins": [self.ORIGIN], "row_version": channel["row_version"]}
        self.assertEqual(self.client.put(url, json=body, headers=self.admin).json()["name"], "Renamed")
        stale = self.client.put(url, json=body, headers=self.admin)
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "conflict.stale"))

    # -- publishing and the snapshot (RUL-003) ---------------------------------------------------------------------------

    def test_publishing_freezes_the_snapshot_and_edits_do_not_touch_what_is_live(self) -> None:
        channel = self.live_channel(experience={"welcome": "Welcome v1"})
        self.assertEqual((channel["status"], channel["published_version"], channel["has_unpublished_changes"]), ("active", "v1.0", False))
        self.assertEqual(channel["embed_code"], f'<script src="http://testserver/v1/widget.js" data-site-key="{channel["site_key"]}" async></script>')
        edited = self.client.put(
            f"/api/v1/channels/{channel['id']}",
            json={"name": "Renamed", "assistant_id": self.assistant_id, "allowed_origins": ["https://other.example.com"], "experience": {"welcome": "Welcome v2"}, "row_version": channel["row_version"]},
            headers=self.login("admin"),
        ).json()
        self.assertTrue(edited["has_unpublished_changes"])
        live = self.session(channel["site_key"]).json()
        self.assertEqual(live["config"]["welcome"], "Welcome v1")  # the draft is invisible to visitors
        self.assertEqual(self.session(channel["site_key"], "https://other.example.com").status_code, 403)  # old allow-list still applies
        republished = self.publish_channel(channel["id"], self.login("admin")).json()
        self.assertEqual((republished["published_version"], republished["has_unpublished_changes"]), ("v1.1", False))
        self.assertEqual(self.session(channel["site_key"], "https://other.example.com").json()["config"]["welcome"], "Welcome v2")
        self.assertEqual(self.session(channel["site_key"]).status_code, 403)  # removed from the allow-list by the republish

    def test_publish_preconditions(self) -> None:
        channel = self.make_channel(allowed_origins=[])
        incomplete = self.publish_channel(channel["id"])
        self.assertEqual((incomplete.status_code, incomplete.json()["code"]), (422, "channel.incomplete"))
        self.admin = self.login("admin")
        draft_only = self.make_assistant(self.admin, "Draft only", "p")
        self.admin = self.login("admin")
        unpublished = self.make_channel(name="Needs live assistant", assistant_id=draft_only)
        blocked = self.publish_channel(unpublished["id"])
        self.assertEqual((blocked.status_code, blocked.json()["code"]), (409, "channel.publish_blocked"))
        # Development channels use the latest draft, so they do not need a published assistant.
        dev = self.make_channel(name="Dev site", assistant_id=draft_only, environment="development")
        self.assertEqual(self.publish_channel(dev["id"]).status_code, 200)

    def test_the_first_publish_goes_through_the_channel_approval_policy(self) -> None:
        self.require_approval()
        channel = self.make_channel()
        pending = self.publish_channel(channel["id"])
        self.assertEqual(pending.status_code, 202, pending.text)
        approval_id = pending.json()["approval_id"]
        shown = self.client.get(f"/api/v1/channels/{channel['id']}", headers=self.login("admin")).json()
        self.assertEqual((shown["status"], shown["pending_approval_id"], shown["embed_code"]), ("draft", approval_id, None))
        self.assertEqual(self.session(channel["site_key"]).status_code, 403)  # nothing is live yet
        approval = self.client.get(f"/api/v1/approvals/{approval_id}", headers=self.login("admin")).json()
        self.assertEqual((approval["title"], approval["type"]), ("发布渠道 Website v1.0", "publish"))
        self.assertIn("允许域名", [d["field"] for d in approval["change_diff"]])
        again = self.publish_channel(channel["id"], self.login("admin"))
        self.assertEqual((again.status_code, again.json()["code"]), (409, "approval.pending_exists"))
        approved = self.client.post(f"/api/v1/approvals/{approval_id}/approve", json={}, headers=self.login("admin2"))
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(self.drain() >= 1, True)
        live = self.client.get(f"/api/v1/channels/{channel['id']}", headers=self.login("admin")).json()
        self.assertEqual((live["status"], live["published_version"], live["pending_approval_id"]), ("active", "v1.0", None))
        self.assertEqual(self.session(channel["site_key"]).status_code, 200)

    def test_a_rejected_publish_leaves_the_channel_a_draft(self) -> None:
        self.require_approval()
        channel = self.make_channel()
        approval_id = self.publish_channel(channel["id"]).json()["approval_id"]
        self.client.post(f"/api/v1/approvals/{approval_id}/reject", json={"comment": "no"}, headers=self.login("admin2"))
        self.drain()
        self.assertEqual(self.client.get(f"/api/v1/channels/{channel['id']}", headers=self.login("admin")).json()["status"], "draft")

    def test_enable_disable_rotate_and_delete(self) -> None:
        draft = self.make_channel(name="Never published")
        refused = self.client.post(f"/api/v1/channels/{draft['id']}/enable", headers=self.admin)
        self.assertEqual((refused.status_code, refused.json()["code"]), (409, "channel.not_published"))
        channel = self.live_channel()
        admin = self.login("admin")
        off = self.client.post(f"/api/v1/channels/{channel['id']}/disable", headers=admin).json()
        self.assertEqual(off["status"], "disabled")
        unavailable = self.session(channel["site_key"])
        self.assertEqual((unavailable.status_code, unavailable.json()["error"]["code"]), (403, "channel_unavailable"))
        self.assertEqual(self.client.post(f"/api/v1/channels/{channel['id']}/enable", headers=self.login("admin")).json()["status"], "active")
        self.assertEqual(self.session(channel["site_key"]).status_code, 200)
        rotated = self.client.post(f"/api/v1/channels/{channel['id']}/rotate-key", headers=self.login("admin")).json()
        self.assertNotEqual(rotated["site_key"], channel["site_key"])
        self.assertEqual(self.session(channel["site_key"]).json()["error"]["code"], "site_not_found")  # embeds with the old key stop
        self.assertEqual(self.session(rotated["site_key"]).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/v1/channels/{channel['id']}", headers=self.login("admin")).status_code, 204)
        self.assertEqual(self.session(rotated["site_key"]).status_code, 404)
        with self.session_factory() as db:
            actions = {a.action for a in db.scalars(select(AuditEvent).where(AuditEvent.action.like("channel.%")))}
        self.assertTrue({"channel.created", "channel.published", "channel.disabled", "channel.enabled", "channel.key_rotated", "channel.deleted"} <= actions)

    # -- the widget -------------------------------------------------------------------------------------------------------------

    def test_a_session_returns_the_inherited_experience_and_a_cors_header_for_the_allowed_origin(self) -> None:
        channel, opened = self.open_widget(self.live_channel(experience={"primary_color": "#112233"}))
        self.assertEqual(opened["config"], {"assistant_name": "Helper", "welcome": "hi", "primary_color": "#112233"})  # name + greeting inherited
        self.assertRegex(opened["visitor_id"], r"^v_")
        self.assertEqual(opened["expires_in"], 3600)
        response = self.session(channel["site_key"])
        self.assertEqual(response.headers["access-control-allow-origin"], self.ORIGIN)
        kept = self.session(channel["site_key"], visitor_id="my-visitor-123").json()
        self.assertEqual(kept["visitor_id"], "my-visitor-123")
        self.assertEqual(self.session(channel["site_key"], visitor_id="bad id!").status_code, 422)

    def test_origins_that_are_not_allowed_are_refused_without_cors_access(self) -> None:
        channel = self.live_channel()
        for origin in ("https://evil.example.com", "https://www.example.com.evil.io", "http://www.example.com", "https://www.example.com:8443", "null"):
            response = self.session(channel["site_key"], origin)
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "origin_not_allowed"), origin)
            self.assertNotIn("access-control-allow-origin", response.headers)
        missing = self.session(channel["site_key"], None)
        self.assertEqual((missing.status_code, missing.json()["error"]["code"]), (403, "origin_required"))
        self.assertEqual(self.session("bw_site_unknown", self.ORIGIN).json()["error"]["code"], "site_not_found")
        self.admin = self.login("admin")
        draft = self.make_channel(name="Draft site")
        self.assertEqual(self.session(draft["site_key"]).json()["error"]["code"], "channel_unavailable")

    def test_tokens_are_bound_to_one_channel_and_one_origin_and_cannot_be_forged(self) -> None:
        import jwt

        from beework.api.widget import issue_token
        from beework.config import settings
        from beework.models.channels import ChannelInstance

        channel, opened = self.open_widget()
        self.assertEqual(self.widget_chat(opened["token"], origin="https://other.example.com").json()["error"]["code"], "origin_mismatch")
        self.assertEqual(self.widget_chat("garbage").json()["error"]["code"], "invalid_token")
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/v1/widget/chat", json={"message": "x"}, headers={"Origin": self.ORIGIN}).json()["error"]["code"], "missing_token")
        # A token signed with the console's secret (not the derived widget key) is rejected, and so are other token types.
        forged = jwt.encode({"typ": "widget", "ch": channel["id"], "vis": "v_x", "org": self.ORIGIN, "exp": int(time.time()) + 600}, settings.auth_secret, algorithm="HS256")
        self.assertEqual(self.widget_chat(forged).json()["error"]["code"], "invalid_token")
        # An expired token asks the widget to start a new session.
        with self.session_factory() as db:
            row = db.get(ChannelInstance, UUID(channel["id"]))
            expired = issue_token(row, "v_x", self.ORIGIN, now=time.time() - 7200)
        self.assertEqual(self.widget_chat(expired).json()["error"]["code"], "expired_token")
        # A console session token is not a widget token.
        self.assertEqual(self.widget_chat(self.client.cookies.get("beework_session") or "console-token").status_code, 401)

    def test_a_visitor_chats_with_the_published_assistant_version(self) -> None:
        channel, opened = self.open_widget()
        current = self.client.get(f"/api/v1/assistants/{self.assistant_id}", headers=self.login("admin")).json()
        self.client.put(
            f"/api/v1/assistants/{self.assistant_id}",
            json={"name": "Helper", "description": "d", "category": current["category"], "visibility": "own_org",
                  "config": {**current["config"], "prompt": "Draft prompt."}, "row_version": current["row_version"]},
            headers=self.login("admin"),
        )
        first = self.widget_chat(opened["token"], "hello")
        self.assertEqual(first.status_code, 200, first.text)
        body = first.json()
        self.assertEqual(body["message"]["content"], "[You are helpful.] echo: hello")  # the live version, not the draft
        self.assertEqual(first.headers["access-control-allow-origin"], self.ORIGIN)
        again = self.widget_chat(opened["token"], "more", conversation_id=body["conversation_id"])
        self.assertEqual(again.json()["conversation_id"], body["conversation_id"])
        with self.session_factory() as db:
            conversation = db.get(Conversation, UUID(body["conversation_id"]))
            self.assertEqual((conversation.source, str(conversation.channel_instance_id), conversation.assistant_version, conversation.turns), ("website", channel["id"], "v1.0", 2))
            end_user = db.get(EndUser, conversation.end_user_id)
            self.assertEqual((end_user.channel_type, end_user.external_id), ("website", opened["visitor_id"]))
        history = self.client.get(f"/v1/widget/conversations/{body['conversation_id']}/messages", headers={"Authorization": f"Bearer {opened['token']}", "Origin": self.ORIGIN}).json()
        self.assertEqual([m["role"] for m in history["messages"]], ["user", "assistant", "user", "assistant"])

    def test_visitors_cannot_read_or_continue_each_others_conversations(self) -> None:
        channel = self.live_channel()
        _, alice = self.open_widget(channel, visitor_id="alice-visitor-1")
        _, bob = self.open_widget(channel, visitor_id="bob-visitor-001")
        conversation_id = self.widget_chat(alice["token"]).json()["conversation_id"]
        headers = {"Authorization": f"Bearer {bob['token']}", "Origin": self.ORIGIN}
        self.assertEqual(self.client.get(f"/v1/widget/conversations/{conversation_id}/messages", headers=headers).status_code, 404)
        self.assertEqual(self.widget_chat(bob["token"], conversation_id=conversation_id).json()["error"]["code"], "conversation_not_found")
        # Open-API conversations are not visible to visitors either.
        key = self.create_key_for_tests()
        api_conversation = self.client.post(f"/v1/assistants/{self.assistant_id}/chat", json={"message": "x", "end_user": {"id": "u"}}, headers={"Authorization": f"Bearer {key}"}).json()["conversation_id"]
        self.assertEqual(self.client.get(f"/v1/widget/conversations/{api_conversation}/messages", headers={"Authorization": f"Bearer {alice['token']}", "Origin": self.ORIGIN}).status_code, 404)

    def create_key_for_tests(self) -> str:
        response = self.client.post("/api/v1/tenant/api-keys", json={"name": "k"}, headers=self.login("admin"))
        return response.json()["key"]

    def test_a_site_key_only_reaches_the_assistant_its_channel_is_bound_to(self) -> None:
        mine = self.live_channel()
        foreign = self.login("foreign", self.ids["other"])
        theirs_assistant = self.make_assistant(foreign, "Theirs", "Their prompt.")
        self.client.post(f"/api/v1/assistants/{theirs_assistant}/publish", json={}, headers=self.login("foreign", self.ids["other"]))
        theirs = self.make_channel(headers=self.login("foreign", self.ids["other"]), name="Their site", assistant_id=theirs_assistant, allowed_origins=["https://theirs.example.com"])
        self.assertEqual(self.publish_channel(theirs["id"], self.login("foreign", self.ids["other"])).status_code, 200)
        _, opened = self.open_widget(mine)
        # There is no parameter that selects another assistant: the answer is always from the channel's own assistant.
        reply = self.widget_chat(opened["token"], "who are you").json()
        self.assertEqual(reply["message"]["content"], "[You are helpful.] echo: who are you")
        # Using my site key from their origin, or their key from my origin, fails.
        self.assertEqual(self.session(mine["site_key"], "https://theirs.example.com").status_code, 403)
        self.assertEqual(self.session(theirs["site_key"], self.ORIGIN).status_code, 403)

    def test_streaming_replies_are_relayed(self) -> None:
        _, opened = self.open_widget()
        self.client.cookies.clear()
        with self.client.stream("POST", "/v1/widget/chat", json={"message": "stream", "stream": True}, headers={"Authorization": f"Bearer {opened['token']}", "Origin": self.ORIGIN}) as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["access-control-allow-origin"], self.ORIGIN)
            text = response.read().decode()
        names = [b.splitlines()[0].removeprefix("event: ") for b in text.split("\n\n") if b.startswith("event:")]
        self.assertEqual((names[0], names[-1]), ("start", "done"))
        self.assertIn("echo: stream", text)

    def test_preflight_and_the_embed_script(self) -> None:
        preflight = self.client.options("/v1/widget/chat", headers={"Origin": self.ORIGIN, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"})
        self.assertEqual(preflight.status_code, 204)
        self.assertEqual(preflight.headers["access-control-allow-origin"], self.ORIGIN)
        self.assertIn("Authorization", preflight.headers["access-control-allow-headers"])
        script = self.client.get("/v1/widget.js")
        self.assertEqual(script.status_code, 200)
        self.assertTrue(script.headers["content-type"].startswith("application/javascript"))
        self.assertEqual(script.headers["access-control-allow-origin"], "*")
        self.assertIn("data-site-key", script.text)
        self.assertNotIn("innerHTML", script.text.replace("never innerHTML", ""))  # replies are rendered as text only

    def test_widget_traffic_is_rate_limited_per_visitor_address_and_per_tenant(self) -> None:
        channel, opened = self.open_widget()
        now = [10.0]
        limiter = Limiter(primary=MemoryStore(clock=lambda: now[0]))
        with mock.patch.object(channel_chat, "limiter", limiter):
            codes = [self.widget_chat(opened["token"], f"m{i}").status_code for i in range(12)]
        self.assertEqual(codes[:10], [200] * 10)
        self.assertEqual(codes[10:], [429, 429])
        limited = self.widget_chat(opened["token"])  # still inside the same instant of the fake clock? new limiter: fresh bucket
        self.assertIn(limited.status_code, (200, 429))
        tenant_limiter = Limiter(primary=MemoryStore(clock=lambda: now[0]))
        with mock.patch.object(channel_chat, "limiter", tenant_limiter), mock.patch.object(channel_chat, "quota_limit", return_value=1):
            self.assertEqual(self.widget_chat(opened["token"], "a").status_code, 200)
            over = self.widget_chat(opened["token"], "b")
            self.assertEqual((over.status_code, over.json()["error"]["code"]), (429, "rate_limited"))
            self.assertEqual(over.headers["retry-after"], "1")
        self.assertEqual(channel["status"], "active")

    def test_a_development_channel_serves_the_latest_draft(self) -> None:
        self.admin = self.login("admin")
        draft_only = self.make_assistant(self.admin, "Dev bot", "Dev prompt.")
        self.admin = self.login("admin")
        channel = self.make_channel(name="Dev site", assistant_id=draft_only, environment="development")
        self.assertEqual(self.publish_channel(channel["id"], self.login("admin")).status_code, 200)
        _, opened = self.open_widget(self.client.get(f"/api/v1/channels/{channel['id']}", headers=self.login("admin")).json())
        reply = self.widget_chat(opened["token"], "hi").json()
        self.assertEqual(reply["message"]["content"], "[Dev prompt.] echo: hi")
        with self.session_factory() as db:
            self.assertEqual({c.environment for c in db.scalars(select(Conversation))}, {"development"})

    def test_disabling_the_assistant_or_the_channel_stops_a_valid_token(self) -> None:
        channel, opened = self.open_widget()
        self.client.post(f"/api/v1/assistants/{self.assistant_id}/disable", headers=self.login("admin"))
        self.assertEqual(self.widget_chat(opened["token"]).json()["error"]["code"], "assistant_disabled")
        self.client.post(f"/api/v1/assistants/{self.assistant_id}/enable", headers=self.login("admin"))
        self.assertEqual(self.widget_chat(opened["token"]).status_code, 200)
        self.client.post(f"/api/v1/channels/{channel['id']}/disable", headers=self.login("admin"))
        self.assertEqual(self.widget_chat(opened["token"]).json()["error"]["code"], "channel_unavailable")


if __name__ == "__main__":
    unittest.main()
