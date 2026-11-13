"""T-029: AI prompt suggestions (AST-021 / AST-052) — generation, billing metadata, audit and draft flag."""

import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from beework.api import prompt_suggestions as api
from beework.db import get_db
from beework.integrations.litellm import GatewayError
from beework.main import app
from beework.models import (
    AUDIT_ACTION_DESCRIPTIONS,
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
from beework.security import hash_password
from isolation import disable_external_integrations, stub_public_dns
from test_assistants import PASSWORD, make_model
from test_runtime import ScriptedChat

URL = "/api/v1/assistants/prompt-suggestions"


class PromptSuggestionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        disable_external_integrations()
        stub_public_dns()
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
        app.dependency_overrides.clear()
        cls.engine.dispose()

    def setUp(self) -> None:
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

            def user(email: str, name: str) -> User:
                account = User(email=email, display_name=name, password_hash=hash_password(PASSWORD))
                db.add(account)
                db.flush()
                return account

            admin = user("admin@example.com", "Admin")
            viewer = user("viewer@example.com", "Viewer")
            other_admin = user("other@example.com", "OtherAdmin")
            db.add_all([
                Membership(tenant_id=tenant.id, user_id=admin.id, organization_id=root.id, role="tenant_admin"),
                Membership(tenant_id=tenant.id, user_id=viewer.id, organization_id=root.id, role="viewer"),
                Membership(tenant_id=other.id, user_id=other_admin.id, organization_id=other_root.id, role="tenant_admin"),
            ])
            chat = make_model("Chat One")
            db.add(chat)
            db.flush()
            environment = TenantModelEnvironment(tenant_id=tenant.id, environment="production")
            db.add(environment)
            db.flush()
            db.add(TenantModelSelection(environment_id=environment.id, model_id=chat.id, model_type="chat", is_default=True))

            def assistant(name: str, owner: Tenant, org: Organization) -> Assistant:
                row = Assistant(
                    tenant_id=owner.id, organization_id=org.id, name=name, description="保险问答",
                    draft_config={"prompt": "You are helpful."}, created_by="Admin",
                )
                db.add(row)
                db.flush()
                return row

            primary = assistant("保险智能体", tenant, root)
            foreign = assistant("他租户智能体", other, other_root)
            self.ids = {
                "tenant": str(tenant.id), "other": str(other.id), "chat": str(chat.id),
                "assistant": str(primary.id), "other_assistant": str(foreign.id),
            }
        self.headers = self.login("admin@example.com")

    def login(self, email: str, tenant: str | None = None) -> dict[str, str]:
        self.client.cookies.clear()
        response = self.client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return {
            "X-CSRF-Token": self.client.cookies.get("beework_csrf"),
            "X-Tenant-ID": tenant or self.ids["tenant"],
        }

    def suggest(self, body: dict | None = None, *, headers=None, llm=None, gateway_error=False):
        """Inject a fake chat model and a fake gateway key; capture the model alias and billing metadata."""
        payload = {
            "name": "保险智能体", "scenario": "帮助代理人查询保单", "assistant_id": self.ids["assistant"],
            **(body or {}),
        }
        fake = llm or ScriptedChat(chunks=["# 核心身份\n你是一名保险顾问。"], seen_messages=[])
        self.captured: dict = {}

        def factory(key, alias, metadata):
            self.captured = {"key": key, "alias": alias, "metadata": metadata}
            return fake

        with mock.patch.object(api, "tenant_gateway_key", return_value="sk-test") as key, mock.patch.object(api, "SUGGESTION_MODEL_FACTORY", factory):
            if gateway_error:
                key.side_effect = GatewayError("no key")
            return self.client.post(URL, json=payload, headers=headers or self.headers)

    # -- generation ---------------------------------------------------------------

    def test_generates_with_chinese_template_debug_metadata_and_audit(self) -> None:
        llm = ScriptedChat(chunks=["# 核心身份\n你是一名保险顾问。"], seen_messages=[])
        response = self.suggest(llm=llm)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["prompt"], body["truncated"], body["environment"], body["model_id"]),
                         ("# 核心身份\n你是一名保险顾问。", False, "production", self.ids["chat"]))
        system, human = llm.seen_messages[0]
        self.assertIn("资深的企业 AI 智能体提示词工程师", system.content)
        self.assertIn("智能体名称：保险智能体", human.content)
        self.assertIn("使用场景：帮助代理人查询保单", human.content)
        self.assertEqual(self.captured["metadata"]["source"], "debug")
        self.assertEqual(self.captured["metadata"]["purpose"], "prompt_suggestion")
        self.assertEqual(self.captured["metadata"]["tenant_id"], self.ids["tenant"])
        self.assertEqual(self.captured["metadata"]["assistant_id"], self.ids["assistant"])
        self.assertTrue(self.captured["alias"].startswith("bw-"))
        with self.session_factory() as db:
            event = db.scalar(select(AuditEvent).where(AuditEvent.action == "assistant.prompt_suggestion"))
            self.assertIsNotNone(event)
            self.assertEqual((event.category, event.description, event.details["locale"], event.details["truncated"]),
                             ("assistants", "使用 AI 生成智能体提示词建议", "zh-CN", "false"))
        # The generated text is never saved by the endpoint.
        state = self.client.get(f"/api/v1/assistants/{self.ids['assistant']}", headers=self.headers).json()
        self.assertEqual(state["config"]["prompt"], "You are helpful.")
        self.assertIs(state["config"]["prompt_ai_optimized"], False)

    def test_english_template_is_selected_by_the_ui_language(self) -> None:
        llm = ScriptedChat(chunks=["# Core identity\nYou are an insurance adviser."], seen_messages=[])
        response = self.suggest({"locale": "en"}, llm=llm)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["locale"], "en")
        self.assertIn("senior prompt engineer", llm.seen_messages[0][0].content)
        self.assertIn("Agent name: 保险智能体", llm.seen_messages[0][1].content)
        self.assertNotIn("资深", llm.seen_messages[0][0].content)
        self.assertEqual(AUDIT_ACTION_DESCRIPTIONS["assistant.prompt_suggestion"], "使用 AI 生成智能体提示词建议")

    def test_output_is_capped_at_8000_characters_and_fences_are_removed(self) -> None:
        response = self.suggest(llm=ScriptedChat(chunks=["x" * 9000], seen_messages=[]))
        self.assertEqual((len(response.json()["prompt"]), response.json()["truncated"]), (8000, True))
        fenced = self.suggest(llm=ScriptedChat(chunks=["```markdown\n# 核心身份\n你是顾问\n```"], seen_messages=[]))
        self.assertEqual(fenced.json()["prompt"], "# 核心身份\n你是顾问")

    def test_existing_prompt_is_passed_as_reference_material(self) -> None:
        llm = ScriptedChat(chunks=["# 核心身份"], seen_messages=[])
        self.suggest({"current_prompt": "保留这条约束"}, llm=llm)
        self.assertIn("保留这条约束", llm.seen_messages[0][1].content)

    # -- validation, permissions, isolation ---------------------------------------

    def test_blank_or_oversized_material_is_422(self) -> None:
        for body in ({"name": ""}, {"name": "   "}, {"scenario": ""}, {"scenario": "  "},
                     {"name": "x" * 51}, {"scenario": "x" * 501}, {"locale": "fr"},
                     {"current_prompt": "x" * 8001}):
            self.assertEqual(self.suggest(body).status_code, 422, body)

    def test_viewer_is_403(self) -> None:
        viewer = self.login("viewer@example.com")
        self.assertEqual(self.suggest(headers=viewer).status_code, 403)

    def test_cross_tenant_or_out_of_scope_assistant_is_404(self) -> None:
        self.assertEqual(self.suggest({"assistant_id": self.ids["other_assistant"]}).status_code, 404)
        other = self.login("other@example.com", self.ids["other"])
        self.assertEqual(self.suggest({"assistant_id": self.ids["assistant"]}, headers=other).status_code, 404)

    def test_missing_default_model_or_gateway_key_is_reported(self) -> None:
        with self.session_factory.begin() as db:
            selection = db.scalar(select(TenantModelSelection))
            selection.is_default = False
        self.assertEqual((self.suggest().status_code, self.suggest().json()["code"]), (422, "assistant.no_chat_model"))
        with self.session_factory.begin() as db:
            db.scalar(select(TenantModelSelection)).is_default = True
        response = self.suggest(gateway_error=True)
        self.assertEqual((response.status_code, response.json()["code"]), (409, "runtime.gateway_not_ready"))

    def test_monthly_points_quota_is_enforced_before_the_call(self) -> None:
        from beework.errors import ApiError

        with mock.patch.object(api, "enforce_quota", side_effect=ApiError(403, "plan.quota_exceeded", "本月点数已用完")):
            response = self.suggest()
        self.assertEqual((response.status_code, response.json()["code"]), (403, "plan.quota_exceeded"))

    # -- failures -----------------------------------------------------------------

    def test_timeout_model_error_and_empty_output_are_reported_and_not_audited(self) -> None:
        slow = ScriptedChat(chunks=["x"], seen_messages=[], before_chunk=lambda _i: time.sleep(0.2))
        with mock.patch.object(api, "SUGGESTION_TIMEOUT_SECONDS", 0.05):
            timeout = self.suggest(llm=slow)
        self.assertEqual((timeout.status_code, timeout.json()["code"]), (504, "prompt_suggestion.timeout"))
        broken = self.suggest(llm=ScriptedChat(chunks=["x"], seen_messages=[], error=RuntimeError("down")))
        self.assertEqual((broken.status_code, broken.json()["code"]), (502, "prompt_suggestion.failed"))
        empty = self.suggest(llm=ScriptedChat(chunks=["   "], seen_messages=[]))
        self.assertEqual((empty.status_code, empty.json()["code"]), (502, "prompt_suggestion.failed"))
        with self.session_factory() as db:
            self.assertIsNone(db.scalar(select(AuditEvent).where(AuditEvent.action == "assistant.prompt_suggestion")))

    # -- AST-052 draft flag -------------------------------------------------------

    def test_prompt_ai_optimized_round_trips_when_the_draft_is_saved(self) -> None:
        url = f"/api/v1/assistants/{self.ids['assistant']}"
        state = self.client.get(url, headers=self.headers).json()
        self.assertIs(state["config"]["prompt_ai_optimized"], False)
        config = {**state["config"], "prompt": "# 核心身份\n你是一名保险顾问。", "prompt_ai_optimized": True}
        response = self.client.put(url, json={
            "name": state["name"], "description": state["description"], "category": state["category"],
            "visibility": state["visibility"], "visible_org_ids": [], "row_version": state["row_version"],
            "config": config,
        }, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["config"]["prompt_ai_optimized"], True)
        self.assertIs(self.client.get(url, headers=self.headers).json()["config"]["prompt_ai_optimized"], True)


if __name__ == "__main__":
    unittest.main()
