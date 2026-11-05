import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from beework import entitlements
from beework.db import get_db
from beework.main import app
from beework.models import AuditEvent, Base, Membership, Organization, Tenant, User
from beework.models.plans import Plan, PlanVersion, TenantSubscription
from beework.security import hash_password
from isolation import disable_external_integrations, stub_public_dns

PASSWORD = "correct horse battery"


class PlanManagementTests(unittest.TestCase):
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
        raw = self.engine.raw_connection()
        raw.cursor().execute("PRAGMA foreign_keys=OFF")
        raw.close()
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        self.client = TestClient(app)
        with self.session_factory.begin() as db:
            tenant = Tenant(name="Acme", slug="acme", plan="standard", seat_limit=3)
            platform = User(email="platform@example.com", display_name="Platform", password_hash=hash_password(PASSWORD), is_platform_admin=True)
            admin = User(email="admin@example.com", display_name="Admin", password_hash=hash_password(PASSWORD))
            db.add_all([tenant, platform, admin])
            db.flush()
            org = Organization(tenant_id=tenant.id, name=tenant.name)
            db.add(org)
            db.flush()
            db.add(Membership(tenant_id=tenant.id, user_id=admin.id, organization_id=org.id, role="tenant_admin"))
            entitlements.create_subscription(db, tenant, seats=3)
            self.tenant_id = tenant.id

    def login(self, email: str) -> dict[str, str]:
        response = self.client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return {"X-CSRF-Token": self.client.cookies.get("beework_csrf"), "X-Tenant-ID": str(self.tenant_id)}

    def plan(self, headers, code):
        plans = self.client.get("/api/v1/platform/plans", headers=headers).json()
        return next(plan for plan in plans if plan["code"] == code)

    def test_plan_list_is_platform_only_and_counts_tenants_per_version(self) -> None:
        admin = self.login("admin@example.com")
        self.assertEqual(self.client.get("/api/v1/platform/plans", headers=admin).status_code, 403)
        self.client.post("/api/v1/auth/logout", headers=admin)
        headers = self.login("platform@example.com")
        plans = self.client.get("/api/v1/platform/plans", headers=headers).json()
        self.assertEqual([plan["code"] for plan in plans], ["standard", "professional", "enterprise"])
        standard = plans[0]
        self.assertEqual((standard["tenant_count"], standard["latest_version"]), (1, 1))
        self.assertEqual(standard["tenants"], [{"id": str(self.tenant_id), "name": "Acme"}])
        self.assertEqual(standard["versions"][0]["tenant_count"], 1)
        self.assertEqual(plans[2]["tenant_count"], 0)

    def test_new_version_leaves_existing_snapshot_until_the_change_is_applied(self) -> None:
        headers = self.login("platform@example.com")
        response = self.client.post(
            "/api/v1/platform/plans/standard/versions", headers=headers,
            json={"price_text": "$599 / 月", "quotas": {"assistants": 8}, "entitlements": {"mcp": True}},
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual((body["latest_version"], body["price_text"]), (2, "$599 / 月"))
        self.assertEqual(body["versions"][0]["quotas"]["assistants"], 8)
        self.assertEqual(body["versions"][0]["quotas"]["seats"], 10)  # untouched values carry over
        self.assertEqual(body["versions"][1]["quotas"]["assistants"], 5)  # version 1 is immutable
        self.assertEqual([v["tenant_count"] for v in body["versions"]], [0, 1])
        with self.session_factory() as db:
            subscription = entitlements.get_subscription(db, self.tenant_id)
            self.assertEqual(subscription.quotas["assistants"], 5)
            self.assertFalse(entitlements.has_entitlement(db, self.tenant_id, "mcp"))
        applied = self.client.post(
            f"/api/v1/platform/tenants/{self.tenant_id}/subscription", headers=headers, json={"plan_code": "standard"},
        )
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertEqual((applied.json()["plan_version"], applied.json()["quotas"]["assistants"]), (2, 8))
        with self.session_factory() as db:
            self.assertTrue(entitlements.has_entitlement(db, self.tenant_id, "mcp"))
            subscriptions = list(db.scalars(select(TenantSubscription).where(TenantSubscription.tenant_id == self.tenant_id)))
            self.assertEqual(sorted(s.is_current for s in subscriptions), [False, True])  # history is kept
            self.assertEqual(db.scalar(select(AuditEvent.action).where(AuditEvent.action == "plan.version_published")), "plan.version_published")

    def test_invalid_definitions_are_rejected(self) -> None:
        headers = self.login("platform@example.com")
        for bad in (
            {"entitlements": {"teleport": True}},
            {"entitlements": {"environments": "some"}},
            {"entitlements": {"approval_levels": 3}},
            {"quotas": {"assistants": -1}},
            {"quotas": {"assistants": "many"}},
            {"quotas": {"gold": 1}},
            {"overage_policy": {"assistants": "explode"}},
        ):
            response = self.client.post("/api/v1/platform/plans/standard/versions", headers=headers, json=bad)
            self.assertEqual(response.status_code, 422, (bad, response.text))
        self.assertEqual(self.client.post("/api/v1/platform/plans/nope/versions", headers=headers, json={}).status_code, 404)
        self.assertEqual(self.plan(headers, "standard")["latest_version"], 1)
        unlimited = self.client.post("/api/v1/platform/plans/standard/versions", headers=headers, json={"quotas": {"assistants": None}})
        self.assertEqual(unlimited.status_code, 201)

    def test_plan_in_use_cannot_be_deleted_but_an_unused_one_can(self) -> None:
        headers = self.login("platform@example.com")
        refused = self.client.delete("/api/v1/platform/plans/standard", headers=headers)
        self.assertEqual((refused.status_code, refused.json()["code"]), (409, "plan.in_use"))
        # Even after the tenant moves on, its history still points at the old plan version.
        self.client.post(f"/api/v1/platform/tenants/{self.tenant_id}/subscription", headers=headers, json={"plan_code": "professional"})
        self.assertEqual(self.client.delete("/api/v1/platform/plans/standard", headers=headers).status_code, 409)
        with self.session_factory.begin() as db:
            plan = Plan(code="legacy", name="Legacy")
            db.add(plan)
            db.flush()
            db.add(PlanVersion(plan_id=plan.id, version=1, entitlements={}, quotas={}, overage_policy={}))
        self.assertEqual(self.client.delete("/api/v1/platform/plans/legacy", headers=headers).status_code, 204)
        self.assertEqual(self.client.delete("/api/v1/platform/plans/legacy", headers=headers).status_code, 404)
        with self.session_factory() as db:
            self.assertIsNone(db.scalar(select(Plan).where(Plan.code == "legacy")))

    def test_applying_a_plan_drives_environments_models_seats_and_audit(self) -> None:
        tenant_headers = self.login("admin@example.com")
        me = self.client.get("/api/v1/auth/me", headers=tenant_headers).json()
        tenant_entry = next(t for t in me["memberships"] if t["id"] == str(self.tenant_id))
        self.assertEqual(tenant_entry["environments"], ["production"])
        offered = self.client.get("/api/v1/tenant/environments", headers=tenant_headers).json()["environments"]
        self.assertEqual([(e["environment"], e["available"]) for e in offered], [("development", False), ("staging", False), ("production", True)])
        self.client.post("/api/v1/auth/logout", headers=tenant_headers)

        headers = self.login("platform@example.com")
        applied = self.client.post(
            f"/api/v1/platform/tenants/{self.tenant_id}/subscription", headers=headers,
            json={"plan_code": "enterprise", "seats": 25, "billing_cycle": "yearly"},
        )
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertEqual((applied.json()["billing_cycle"], applied.json()["quotas"]["seats"]), ("yearly", 25))
        with self.session_factory() as db:
            tenant = db.get(Tenant, self.tenant_id)
            self.assertEqual((tenant.plan, tenant.seat_limit), ("enterprise", 25))  # denormalised copies follow
            self.assertEqual(entitlements.environments_for(db, self.tenant_id), ("development", "staging", "production"))
            actions = list(db.scalars(select(AuditEvent.action).where(AuditEvent.action == "tenant.subscription_changed")))
            self.assertEqual(len(actions), 2)  # tenant chain and platform chain
        self.client.post("/api/v1/auth/logout", headers=headers)
        tenant_headers = self.login("admin@example.com")
        me = self.client.get("/api/v1/auth/me", headers=tenant_headers).json()
        self.assertEqual(next(t for t in me["memberships"] if t["id"] == str(self.tenant_id))["environments"], ["development", "staging", "production"])
        offered = self.client.get("/api/v1/tenant/environments", headers={**tenant_headers, "X-Environment": "development"}).json()
        self.assertEqual((offered["current"], [e["available"] for e in offered["environments"]]), ("development", [True, True, True]))

    def test_reapplying_the_current_plan_keeps_negotiated_seats(self) -> None:
        headers = self.login("platform@example.com")
        url = f"/api/v1/platform/tenants/{self.tenant_id}/subscription"
        self.client.post("/api/v1/platform/plans/standard/versions", headers=headers, json={"quotas": {"assistants": 8}})
        again = self.client.post(url, headers=headers, json={"plan_code": "standard"})
        self.assertEqual((again.json()["plan_version"], again.json()["quotas"]["seats"]), (2, 3))  # created with 3 seats
        moved = self.client.post(url, headers=headers, json={"plan_code": "professional"})
        self.assertEqual(moved.json()["quotas"]["seats"], 50)  # a different plan brings its own seat quota

    def test_downgrade_keeps_existing_objects_but_blocks_new_ones(self) -> None:
        headers = self.login("platform@example.com")
        self.client.post(f"/api/v1/platform/tenants/{self.tenant_id}/subscription", headers=headers, json={"plan_code": "enterprise", "seats": 5})
        original = entitlements.get_usage_counter("assistants")
        entitlements.register_usage_counter("assistants", lambda _db, _tenant: 9)  # more than standard allows
        try:
            self.client.post(f"/api/v1/platform/tenants/{self.tenant_id}/subscription", headers=headers, json={"plan_code": "standard"})
            with self.session_factory() as db:
                decision = entitlements.check_quota(db, self.tenant_id, "assistants")
                self.assertEqual((decision.allowed, decision.used, decision.limit), (False, 9, 5))
                self.assertEqual(entitlements.environments_for(db, self.tenant_id), ("production",))
        finally:
            if original is None:
                entitlements._usage_counters.pop("assistants", None)
            else:
                entitlements.register_usage_counter("assistants", original)

    def test_unknown_targets_and_versions_are_404_and_csrf_is_required(self) -> None:
        headers = self.login("platform@example.com")
        url = f"/api/v1/platform/tenants/{self.tenant_id}/subscription"
        self.assertEqual(self.client.post(f"/api/v1/platform/tenants/{uuid4()}/subscription", headers=headers, json={"plan_code": "standard"}).status_code, 404)
        self.assertEqual(self.client.post(url, headers=headers, json={"plan_code": "gold"}).status_code, 404)
        self.assertEqual(self.client.post(url, headers=headers, json={"plan_code": "standard", "version": 9}).status_code, 404)
        no_csrf = {key: value for key, value in headers.items() if key != "X-CSRF-Token"}
        self.assertEqual(self.client.post(url, headers=no_csrf, json={"plan_code": "standard"}).status_code, 403)
        self.assertEqual(self.client.post(url, headers=headers, json={"plan_code": "standard", "seats": 0}).status_code, 422)
        current = self.client.get(url, headers=headers)
        self.assertEqual((current.status_code, current.json()["plan_code"]), (200, "standard"))


if __name__ == "__main__":
    unittest.main()
