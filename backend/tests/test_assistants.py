import unittest
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from beework.api import assistants as assistants_api
from beework.db import get_db
from beework.main import app
from beework.models import (
    AuditEvent,
    Base,
    Membership,
    Organization,
    PlatformModel,
    Tenant,
    TenantModelEnvironment,
    TenantModelSelection,
    TenantRole,
    User,
)
from beework.models.assistants import Assistant
from beework.security import hash_password
from isolation import disable_external_integrations, stub_public_dns

PASSWORD = "correct horse battery"


def make_model(name: str, model_type: str = "chat") -> PlatformModel:
    return PlatformModel(
        display_name=name, provider="private", model_type=model_type, availability="all_plans",
        input_price_per_million="1", output_price_per_million="2",
        input_points_per_thousand="1", output_points_per_thousand="2", status="healthy",
        provider_model_id=name.lower().replace(" ", "-"), endpoint_url="https://models.example.test/v1",
        encrypted_api_key="encrypted", api_key_last_four="cret", region="ap_singapore",
    )


class AssistantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        disable_external_integrations()
        stub_public_dns()
        cls.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )

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
        raw = self.engine.raw_connection()
        raw.cursor().execute("PRAGMA foreign_keys=OFF")
        raw.close()
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        self.client = TestClient(app)
        self._blockers = list(assistants_api.DELETE_BLOCKERS)
        with self.session_factory.begin() as db:
            tenant = Tenant(name="Acme", slug="acme", plan="enterprise", seat_limit=20)
            other = Tenant(name="Other", slug="other", plan="enterprise", seat_limit=20)
            db.add_all([tenant, other])
            db.flush()
            root = Organization(tenant_id=tenant.id, name="Acme")
            other_root = Organization(tenant_id=other.id, name="Other")
            db.add_all([root, other_root])
            db.flush()
            sales = Organization(tenant_id=tenant.id, name="Sales", parent_id=root.id)
            support = Organization(tenant_id=tenant.id, name="Support", parent_id=root.id)
            db.add_all([sales, support])
            db.flush()

            def user(email: str, name: str) -> User:
                account = User(email=email, display_name=name, password_hash=hash_password(PASSWORD))
                db.add(account)
                db.flush()
                return account

            admin = user("admin@example.com", "Admin")
            builder = user("builder@example.com", "Builder")
            viewer = user("viewer@example.com", "Viewer")
            sales_user = user("sales@example.com", "SalesUser")
            scoped_user = user("scoped@example.com", "ScopedUser")
            other_admin = user("other@example.com", "OtherAdmin")
            db.add_all([
                Membership(tenant_id=tenant.id, user_id=admin.id, organization_id=root.id, role="tenant_admin"),
                Membership(tenant_id=tenant.id, user_id=builder.id, organization_id=root.id, role="builder"),
                Membership(tenant_id=tenant.id, user_id=viewer.id, organization_id=root.id, role="viewer"),
                Membership(tenant_id=other.id, user_id=other_admin.id, organization_id=other_root.id, role="tenant_admin"),
            ])
            sales_role = TenantRole(
                tenant_id=tenant.id, name="Sales builders", scope="organization",
                scope_organization_id=sales.id,
                permissions={"assistants": ["view", "edit", "delete", "publish"]}, created_by=admin.id,
            )
            db.add(sales_role)
            db.flush()
            db.add(Membership(
                tenant_id=tenant.id, user_id=sales_user.id, organization_id=sales.id,
                role="custom", custom_role_id=sales_role.id,
            ))
            chat, embedding = make_model("Chat One"), make_model("Embed One", "embedding")
            db.add_all([chat, embedding])
            db.flush()
            environment = TenantModelEnvironment(tenant_id=tenant.id, environment="production")
            db.add(environment)
            db.flush()
            db.add_all([
                TenantModelSelection(environment_id=environment.id, model_id=chat.id, model_type="chat", is_default=True),
                TenantModelSelection(environment_id=environment.id, model_id=embedding.id, model_type="embedding", is_default=True),
            ])
            self.ids = {
                "tenant": str(tenant.id), "other": str(other.id), "root": str(root.id),
                "sales": str(sales.id), "support": str(support.id), "chat": str(chat.id),
                "embedding": str(embedding.id), "scoped_user": scoped_user.id,
            }

    def tearDown(self) -> None:
        assistants_api.DELETE_BLOCKERS[:] = self._blockers

    # -- helpers ----------------------------------------------------------------

    def login(self, email: str, tenant: str | None = None) -> dict[str, str]:
        self.client.cookies.clear()
        response = self.client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return {
            "X-CSRF-Token": self.client.cookies.get("beework_csrf"),
            "X-Tenant-ID": tenant or self.ids["tenant"],
        }

    def body(self, name: str | None = "Helper", **overrides) -> dict:
        data = {
            "name": name, "description": "Answers questions", "category": "sales",
            "config": {"prompt": "You are helpful.", "greeting": "Hi"},
        }
        data.update(overrides)
        return data

    def create(self, headers: dict, name: str | None = "Helper", **overrides):
        return self.client.post("/api/v1/assistants", json=self.body(name, **overrides), headers=headers)

    def set_status(self, assistant_id: str, status: str) -> None:
        with self.session_factory.begin() as db:
            db.get(Assistant, UUID(assistant_id)).status = status

    # -- create / validate ------------------------------------------------------

    def test_create_fills_defaults_and_is_audited(self) -> None:
        headers = self.login("admin@example.com")
        response = self.create(headers)
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertEqual((data["status"], data["draft_version"], data["organization_id"]), ("draft", "0.1", self.ids["root"]))
        self.assertEqual(data["config"]["model_id"], self.ids["chat"])
        self.assertEqual(data["config"]["dsl_version"], 1)
        self.assertEqual(data["created_by"], "Admin")
        with self.session_factory() as db:
            event_row = db.scalar(select(AuditEvent).where(AuditEvent.action == "assistant.created"))
            self.assertEqual(event_row.category, "assistants")
            self.assertEqual(event_row.details["assistant_id"], data["id"])

    def test_validation_failures(self) -> None:
        headers = self.login("admin@example.com")
        self.assertEqual(self.create(headers, name="  ").status_code, 400)
        self.assertEqual(self.create(headers, name="x" * 51).status_code, 400)
        self.assertEqual(self.create(headers, description="").status_code, 422)
        self.assertEqual(
            self.create(headers, config={"prompt": "x" * 8001}).status_code, 422
        )
        self.assertEqual(self.create(headers, config={"prompt": " "}).status_code, 422)
        self.assertEqual(
            self.create(headers, config={"prompt": "ok", "model_id": self.ids["embedding"]}).status_code, 422
        )
        mounted = self.create(headers, config={"prompt": "ok", "mounts": {"kb": [self.ids["chat"]]}})
        self.assertEqual((mounted.status_code, mounted.json()["code"]), (422, "assistant.mount_unavailable"))
        driven = self.create(headers, config={"prompt": "ok", "driven_by": self.ids["chat"]})
        self.assertEqual(driven.status_code, 422)
        self.assertEqual(self.create(headers, visibility="specified").status_code, 422)
        self.assertEqual(self.create(headers, category="other").status_code, 422)

    def test_oc_config_is_validated_defaulted_and_copied(self) -> None:
        headers = self.login("admin@example.com")
        bad = self.create(headers, config={"prompt": "ok", "oc": {"memory": {"short_term": {"window_rounds": 99}}}})
        self.assertEqual(bad.status_code, 422)
        unknown = self.create(headers, config={"prompt": "ok", "oc": {"routing": {"bogus": True}}})
        self.assertEqual(unknown.status_code, 422)
        created = self.create(headers, config={"prompt": "ok", "oc": {"memory": {"short_term": {"window_rounds": 5}}}})
        self.assertEqual(created.status_code, 201, created.text)
        oc = created.json()["config"]["oc"]
        self.assertEqual((oc["oc_schema_version"], oc["memory"]["short_term"]["window_rounds"], oc["pii"]["enabled"]), (1, 5, True))
        plain = self.create(headers, name="Plain").json()
        self.assertEqual(plain["config"]["oc"]["memory"]["short_term"]["window_rounds"], 3)  # defaults
        copy = self.client.post(f"/api/v1/assistants/{created.json()['id']}/copy", headers=headers).json()
        self.assertEqual(copy["config"]["oc"]["memory"]["short_term"]["window_rounds"], 5)

    def test_names_are_unique_per_tenant_case_insensitively_and_freed_on_delete(self) -> None:
        headers = self.login("admin@example.com")
        first = self.create(headers, name="Helper").json()
        duplicate = self.create(headers, name="helper")
        self.assertEqual((duplicate.status_code, duplicate.json()["code"]), (409, "assistant.name_taken"))
        self.assertEqual(
            self.client.delete(f"/api/v1/assistants/{first['id']}", headers=headers).status_code, 204
        )
        self.assertEqual(self.create(headers, name="helper").status_code, 201)

    def test_draft_save_relaxes_required_fields_and_names_unnamed(self) -> None:
        headers = self.login("admin@example.com")
        first = self.client.post(
            "/api/v1/assistants", json={"draft": True, "description": "", "config": {}}, headers=headers
        )
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()["name"], "未命名助理")
        second = self.client.post("/api/v1/assistants", json={"draft": True}, headers=headers)
        self.assertEqual(second.json()["name"], "未命名助理 2")
        self.assertEqual(self.client.post("/api/v1/assistants", json={}, headers=headers).status_code, 400)

    def test_permissions_and_cross_tenant_isolation(self) -> None:
        admin = self.login("admin@example.com")
        created = self.create(admin).json()
        viewer = self.login("viewer@example.com")
        self.assertEqual(self.create(viewer, name="Nope").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/assistants", headers=viewer).status_code, 200)
        self.assertEqual(
            self.client.delete(f"/api/v1/assistants/{created['id']}", headers=viewer).status_code, 403
        )
        self.assertEqual(
            self.client.post(f"/api/v1/assistants/{created['id']}/copy", headers=viewer).status_code, 403
        )
        other = self.login("other@example.com", tenant=self.ids["other"])
        for method, path in (
            ("get", f"/api/v1/assistants/{created['id']}"),
            ("delete", f"/api/v1/assistants/{created['id']}"),
            ("post", f"/api/v1/assistants/{created['id']}/copy"),
            ("post", f"/api/v1/assistants/{created['id']}/disable"),
        ):
            self.assertEqual(getattr(self.client, method)(path, headers=other).status_code, 404, (method, path))
        self.assertEqual(self.client.get("/api/v1/assistants", headers=other).json()["items"], [])
        # A member of Acme cannot address the other tenant at all.
        wrong_header = self.login("admin@example.com", tenant=self.ids["other"])
        self.assertEqual(self.client.get("/api/v1/assistants", headers=wrong_header).status_code, 403)
        # An organization from another tenant is not a valid target.
        admin = self.login("admin@example.com")
        with self.session_factory() as db:
            foreign_org = str(db.scalar(select(Organization.id).where(Organization.tenant_id == UUID(self.ids["other"]))))
        self.assertEqual(self.create(admin, name="X", organization_id=foreign_org).status_code, 404)

    # -- quota ------------------------------------------------------------------

    def test_assistant_quota_is_a_hard_limit_for_create_and_copy(self) -> None:
        headers = self.login("admin@example.com")
        with self.session_factory.begin() as db:
            tenant = db.get(Tenant, UUID(self.ids["tenant"]))
            tenant.plan = "standard"
        # The enterprise snapshot is not created until first use, so the standard limit (5) applies.
        for index in range(5):
            self.assertEqual(self.create(headers, name=f"A{index}").status_code, 201)
        blocked = self.create(headers, name="A5")
        self.assertEqual((blocked.status_code, blocked.json()["code"]), (409, "plan.quota_exceeded"))
        first = self.client.get("/api/v1/assistants", headers=headers).json()["items"][0]
        copied = self.client.post(f"/api/v1/assistants/{first['id']}/copy", headers=headers)
        self.assertEqual((copied.status_code, copied.json()["code"]), (409, "plan.quota_exceeded"))
        summary = self.client.get("/api/v1/assistants/summary", headers=headers).json()
        self.assertEqual((summary["quota"]["used"], summary["quota"]["limit"], summary["total"]), (5, 5, 5))

    # -- update / copy / lifecycle ----------------------------------------------

    def test_update_checks_row_version_and_bumps_it(self) -> None:
        headers = self.login("admin@example.com")
        created = self.create(headers).json()
        update = self.body("Renamed")
        stale = self.client.put(
            f"/api/v1/assistants/{created['id']}", json={**update, "row_version": 99}, headers=headers
        )
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "conflict.stale"))
        missing = self.client.put(f"/api/v1/assistants/{created['id']}", json=update, headers=headers)
        self.assertEqual(missing.status_code, 422)
        saved = self.client.put(
            f"/api/v1/assistants/{created['id']}",
            json={**update, "row_version": created["row_version"]}, headers=headers,
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual((saved.json()["name"], saved.json()["row_version"]), ("Renamed", created["row_version"] + 1))
        self.assertEqual(saved.json()["draft_version"], "0.1")

    def test_only_tenant_admin_can_change_organization(self) -> None:
        admin = self.login("admin@example.com")
        created = self.create(admin).json()
        payload = {**self.body("Helper"), "row_version": created["row_version"], "organization_id": self.ids["sales"]}
        builder = self.login("builder@example.com")
        self.assertEqual(
            self.client.put(f"/api/v1/assistants/{created['id']}", json=payload, headers=builder).status_code, 403
        )
        admin = self.login("admin@example.com")
        moved = self.client.put(f"/api/v1/assistants/{created['id']}", json=payload, headers=admin)
        self.assertEqual((moved.status_code, moved.json()["organization_id"]), (200, self.ids["sales"]))

    def test_copy_creates_independent_draft(self) -> None:
        headers = self.login("admin@example.com")
        source = self.create(headers, name="Helper").json()
        self.set_status(source["id"], "published")
        copy = self.client.post(f"/api/v1/assistants/{source['id']}/copy", headers=headers)
        self.assertEqual(copy.status_code, 201, copy.text)
        data = copy.json()
        self.assertEqual((data["name"], data["status"], data["draft_version"]), ("Helper（副本）", "draft", "0.1"))
        self.assertEqual(data["config"]["prompt"], source["config"]["prompt"])
        self.assertIsNone(data["live_version_id"])
        again = self.client.post(f"/api/v1/assistants/{source['id']}/copy", headers=headers).json()
        self.assertEqual(again["name"], "Helper（副本） 2")

    def test_status_machine_disable_enable_delete(self) -> None:
        headers = self.login("admin@example.com")
        item = self.create(headers).json()
        path = f"/api/v1/assistants/{item['id']}"
        self.assertEqual(self.client.post(f"{path}/disable", headers=headers).status_code, 409)
        self.assertEqual(self.client.post(f"{path}/enable", headers=headers).status_code, 409)
        self.set_status(item["id"], "published")
        self.assertEqual(self.client.delete(path, headers=headers).status_code, 409)
        self.assertEqual(self.client.post(f"{path}/disable", headers=headers).json()["status"], "disabled")
        self.assertEqual(self.client.post(f"{path}/enable", headers=headers).json()["status"], "published")
        self.client.post(f"{path}/disable", headers=headers)
        assistants_api.DELETE_BLOCKERS.append(lambda _db, _assistant: "仍被渠道绑定，请先解绑")
        blocked = self.client.delete(path, headers=headers)
        self.assertEqual((blocked.status_code, blocked.json()["code"]), (409, "assistant.delete_blocked"))
        assistants_api.DELETE_BLOCKERS.clear()
        self.assertEqual(self.client.delete(path, headers=headers).status_code, 204)
        self.assertEqual(self.client.get(path, headers=headers).status_code, 404)
        with self.session_factory() as db:
            actions = set(db.scalars(select(AuditEvent.action).where(AuditEvent.action.like("assistant.%"))))
        self.assertTrue({"assistant.created", "assistant.disabled", "assistant.enabled", "assistant.deleted"} <= actions)

    # -- list / summary / model options -----------------------------------------

    def test_list_filters_search_sort_and_pagination(self) -> None:
        headers = self.login("admin@example.com")
        a = self.create(headers, name="Alpha", description="billing questions").json()
        self.create(headers, name="Beta", description="shipping", organization_id=self.ids["sales"])
        self.create(headers, name="Gamma", description="refunds", organization_id=self.ids["support"])
        self.set_status(a["id"], "published")

        def names(query: str) -> list[str]:
            return [item["name"] for item in self.client.get(f"/api/v1/assistants?{query}", headers=headers).json()["items"]]

        self.assertEqual(names("q=BILLING"), ["Alpha"])
        self.assertEqual(names("status=published"), ["Alpha"])
        self.assertEqual(sorted(names("status=draft")), ["Beta", "Gamma"])
        self.assertEqual(names(f"organization_id={self.ids['sales']}"), ["Beta"])
        self.assertEqual(sorted(names(f"organization_id={self.ids['sales']}&organization_id={self.ids['support']}")), ["Beta", "Gamma"])
        self.assertEqual(names("sort=name_asc"), ["Alpha", "Beta", "Gamma"])
        page = self.client.get("/api/v1/assistants?sort=name_asc&limit=2&offset=2", headers=headers).json()
        self.assertEqual(([i["name"] for i in page["items"]], page["total"]), (["Gamma"], 3))
        self.assertEqual(self.client.get("/api/v1/assistants?status=bogus", headers=headers).status_code, 422)
        summary = self.client.get("/api/v1/assistants/summary", headers=headers).json()
        self.assertEqual((summary["total"], summary["published"], summary["draft"]), (3, 1, 2))
        self.assertIsNone(summary["monthly_conversations"])

    def test_model_options_only_list_enabled_chat_models(self) -> None:
        headers = self.login("admin@example.com")
        options = self.client.get("/api/v1/assistants/model-options", headers=headers).json()
        self.assertEqual([m["id"] for m in options["models"]], [self.ids["chat"]])
        self.assertEqual(options["default_model_id"], self.ids["chat"])

    def test_assistant_scope_role_validation(self) -> None:
        admin = self.login("admin@example.com")
        allowed = self.create(admin, name="Allowed").json()

        def role_payload(**overrides) -> dict:
            data = {
                "name": "One assistant", "scope": "assistant", "scope_assistant_ids": [allowed["id"]],
                "permissions": {"assistants": ["view", "edit", "delete"]},
            }
            data.update(overrides)
            return data

        def post(**overrides):
            return self.client.post("/api/v1/roles", json=role_payload(**overrides), headers=admin)

        self.assertEqual(post(permissions={"assistants": ["view"], "audit": ["view"]}).status_code, 422)
        self.assertEqual(post(scope_assistant_ids=[]).status_code, 422)
        self.assertEqual(post(scope_assistant_ids=[self.ids["chat"]]).status_code, 404)
        self.assertEqual(post(scope="tenant").status_code, 422)
        created = post()
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["scope_assistant_ids"], [allowed["id"]])
        listed = self.client.get("/api/v1/roles", headers=admin).json()["roles"]
        self.assertIn([allowed["id"]], [r["scope_assistant_ids"] for r in listed if r["scope"] == "assistant"])

    # -- organization / assistant scope -----------------------------------------

    def test_organization_scoped_role_visibility_and_write_scope(self) -> None:
        admin = self.login("admin@example.com")
        own = self.create(admin, name="Own", organization_id=self.ids["sales"]).json()
        private = self.create(admin, name="Private", organization_id=self.ids["support"]).json()
        shared_all = self.create(admin, name="SharedAll", organization_id=self.ids["support"], visibility="all").json()
        shared_some = self.create(
            admin, name="SharedSome", organization_id=self.ids["support"],
            visibility="specified", visible_org_ids=[self.ids["sales"]],
        ).json()
        other_specified = self.create(
            admin, name="SharedElsewhere", organization_id=self.ids["support"],
            visibility="specified", visible_org_ids=[self.ids["root"]],
        ).json()

        sales = self.login("sales@example.com")
        seen = {i["name"]: i for i in self.client.get("/api/v1/assistants", headers=sales).json()["items"]}
        self.assertEqual(set(seen), {"Own", "SharedAll", "SharedSome"})
        self.assertTrue(seen["Own"]["can_edit"])
        self.assertFalse(seen["SharedAll"]["can_edit"])
        self.assertEqual(self.client.get(f"/api/v1/assistants/{private['id']}", headers=sales).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/assistants/{other_specified['id']}", headers=sales).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/assistants/{shared_all['id']}", headers=sales).status_code, 200)
        # Visible but owned by another organization: read only.
        self.assertEqual(
            self.client.delete(f"/api/v1/assistants/{shared_some['id']}", headers=sales).status_code, 403
        )
        self.assertEqual(
            self.client.put(
                f"/api/v1/assistants/{shared_all['id']}",
                json={**self.body("SharedAll"), "row_version": shared_all["row_version"]}, headers=sales,
            ).status_code, 403,
        )
        # Creation is limited to the role's organizations.
        self.assertEqual(self.create(sales, name="Mine", organization_id=self.ids["sales"]).status_code, 201)
        self.assertEqual(self.create(sales, name="Elsewhere", organization_id=self.ids["support"]).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/v1/assistants/{own['id']}", headers=sales).status_code, 204)

    def test_assistant_scope_end_to_end(self) -> None:
        admin = self.login("admin@example.com")
        allowed = self.create(admin, name="Allowed").json()
        hidden = self.create(admin, name="Hidden").json()
        created = self.client.post(
            "/api/v1/roles",
            json={
                "name": "One assistant", "scope": "assistant", "scope_assistant_ids": [allowed["id"]],
                "permissions": {"dashboard": ["view"], "assistants": ["view", "edit", "delete"]},
            },
            headers=admin,
        )
        self.assertEqual(created.status_code, 201, created.text)
        with self.session_factory.begin() as db:
            db.add(Membership(
                tenant_id=UUID(self.ids["tenant"]), user_id=self.ids["scoped_user"],
                organization_id=UUID(self.ids["root"]), role="custom", custom_role_id=UUID(created.json()["id"]),
            ))
        scoped = self.login("scoped@example.com")
        items = self.client.get("/api/v1/assistants", headers=scoped).json()["items"]
        self.assertEqual([i["name"] for i in items], ["Allowed"])
        self.assertEqual(self.client.get(f"/api/v1/assistants/{hidden['id']}", headers=scoped).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/v1/assistants/{hidden['id']}", headers=scoped).status_code, 404)
        self.assertEqual(self.create(scoped, name="New").status_code, 403)
        self.assertEqual(self.client.post(f"/api/v1/assistants/{allowed['id']}/copy", headers=scoped).status_code, 403)
        # Other modules stay closed to an assistant-scoped role even if a permission were stored.
        self.assertEqual(self.client.get("/api/v1/organizations", headers=scoped).status_code, 403)
        # Deleting the assistant removes it from the role's scope.
        admin = self.login("admin@example.com")
        self.assertEqual(self.client.delete(f"/api/v1/assistants/{allowed['id']}", headers=admin).status_code, 204)
        with self.session_factory() as db:
            role = db.get(TenantRole, UUID(created.json()["id"]))
            self.assertEqual(role.scope_assistant_ids, [])


if __name__ == "__main__":
    unittest.main()
