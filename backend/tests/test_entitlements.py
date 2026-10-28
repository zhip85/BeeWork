import unittest
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from beework import entitlements
from beework.api.conventions import ApiError
from beework.db import get_db
from beework.main import app
from beework.models import AuditEvent, Base, Membership, Organization, Tenant, User
from beework.models.plans import Plan, PlanVersion, TenantSubscription
from beework.security import hash_password
from isolation import disable_external_integrations, stub_public_dns


class EntitlementTests(unittest.TestCase):
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
        self._counter_backup = dict(entitlements._usage_counters)
        with self.session_factory.begin() as db:
            tenant = Tenant(name="Acme", slug="acme", plan="standard", seat_limit=3)
            other = Tenant(name="Other", slug="other", plan="enterprise", seat_limit=9)
            platform = User(
                email="platform@example.com", display_name="Platform",
                password_hash=hash_password("platform admin safe password"), is_platform_admin=True,
            )
            admin = User(
                email="admin@example.com", display_name="Admin",
                password_hash=hash_password("correct horse battery"),
            )
            viewer = User(
                email="viewer@example.com", display_name="Viewer",
                password_hash=hash_password("another correct battery"),
            )
            db.add_all([tenant, other, platform, admin, viewer])
            db.flush()
            org = Organization(tenant_id=tenant.id, name=tenant.name)
            other_org = Organization(tenant_id=other.id, name=other.name)
            db.add_all([org, other_org])
            db.flush()
            db.add_all([
                Membership(tenant_id=tenant.id, user_id=admin.id, organization_id=org.id, role="tenant_admin"),
                Membership(tenant_id=tenant.id, user_id=viewer.id, organization_id=org.id, role="viewer"),
            ])
            self.tenant_id = tenant.id
            self.other_tenant_id = other.id
            self.organization_id = str(org.id)

    def tearDown(self) -> None:
        entitlements._usage_counters.clear()
        entitlements._usage_counters.update(self._counter_backup)

    def login(self, email: str, password: str) -> dict[str, str]:
        response = self.client.post("/api/v1/auth/login", json={"email": email, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return {"X-CSRF-Token": self.client.cookies.get("beework_csrf")}

    def test_snapshot_is_created_from_tenant_plan_and_seat_limit(self) -> None:
        with self.session_factory.begin() as db:
            subscription = entitlements.get_subscription(db, self.tenant_id)
            self.assertEqual(subscription.quotas["seats"], 3)
            self.assertEqual(subscription.quotas["assistants"], 5)
            self.assertFalse(entitlements.has_entitlement(db, self.tenant_id, "mcp"))
            self.assertTrue(entitlements.has_entitlement(db, self.other_tenant_id, "mcp"))
            self.assertIsNone(entitlements.quota_limit(db, self.other_tenant_id, "assistants"))
            # A second lookup reuses the same current row.
            self.assertEqual(entitlements.get_subscription(db, self.tenant_id).id, subscription.id)

    def test_locked_feature_raises_403_with_plan_code(self) -> None:
        with self.session_factory() as db:
            with self.assertRaises(ApiError) as caught:
                entitlements.ensure_entitlement(db, self.tenant_id, "mcp")
            self.assertEqual(caught.exception.status_code, 403)
            self.assertEqual(caught.exception.code, "plan.feature_locked")
            self.assertEqual(caught.exception.detail, "当前套餐未包含，请联系平台升级")
            entitlements.ensure_entitlement(db, self.other_tenant_id, "mcp")

    def test_require_entitlement_dependency_locks_route(self) -> None:
        path = "/api/v1/_test/needs-mcp"
        from fastapi import Depends

        @app.get(path)
        def needs_mcp(_=Depends(entitlements.require_entitlement("mcp"))) -> dict:
            return {"ok": True}

        try:
            self.login("admin@example.com", "correct horse battery")
            denied = self.client.get(path, headers={"X-Tenant-ID": str(self.tenant_id)})
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(denied.json()["code"], "plan.feature_locked")
            with self.session_factory.begin() as db:
                db.get(Tenant, self.tenant_id).plan = "professional"
                entitlements.create_subscription(db, db.get(Tenant, self.tenant_id))
            allowed = self.client.get(path, headers={"X-Tenant-ID": str(self.tenant_id)})
            self.assertEqual(allowed.status_code, 200)
        finally:
            app.router.routes = [r for r in app.router.routes if getattr(r, "path", "") != path]

    def test_check_quota_hard_soft_unlimited_and_downgrade(self) -> None:
        used = {"value": 4}
        entitlements.register_usage_counter("assistants", lambda _db, _tenant: used["value"])
        with self.session_factory.begin() as db:
            self.assertTrue(entitlements.check_quota(db, self.tenant_id, "assistants").allowed)
            used["value"] = 5
            decision = entitlements.check_quota(db, self.tenant_id, "assistants")
            self.assertEqual((decision.allowed, decision.used, decision.limit, decision.policy), (False, 5, 5, "hard"))
            with self.assertRaises(ApiError) as caught:
                entitlements.enforce_quota(db, self.tenant_id, "assistants")
            self.assertEqual((caught.exception.status_code, caught.exception.code), (409, "plan.quota_exceeded"))
            # Downgrade: existing objects above the new limit are kept but nothing more can be added.
            used["value"] = 7
            self.assertFalse(entitlements.check_quota(db, self.tenant_id, "assistants").allowed)
            # Enterprise has no assistant limit.
            self.assertTrue(entitlements.check_quota(db, self.other_tenant_id, "assistants", 1000).allowed)
            # Soft policy lets usage exceed the limit.
            db.get(TenantSubscription, entitlements.get_subscription(db, self.tenant_id).id).overage_policy = {
                "assistants": "soft"
            }
            db.flush()
            self.assertTrue(entitlements.check_quota(db, self.tenant_id, "assistants").allowed)

    def test_quota_without_counter_is_an_error_not_zero(self) -> None:
        entitlements._usage_counters.pop("storage_gb", None)
        with self.session_factory() as db, self.assertRaises(ValueError):
            entitlements.check_quota(db, self.tenant_id, "storage_gb")

    def test_published_plan_version_does_not_change_existing_snapshot(self) -> None:
        with self.session_factory.begin() as db:
            existing = entitlements.get_subscription(db, self.tenant_id)
            plan = db.scalar(select(Plan).where(Plan.code == "standard"))
            db.add(PlanVersion(
                plan_id=plan.id, version=2, entitlements={"mcp": True},
                quotas={"assistants": 99, "seats": 99}, overage_policy={},
            ))
            db.flush()
            self.assertEqual(entitlements.get_subscription(db, self.tenant_id).quotas["assistants"], 5)
            replaced = entitlements.create_subscription(db, db.get(Tenant, self.tenant_id))
            self.assertEqual(replaced.quotas["assistants"], 99)
            current = db.scalars(select(TenantSubscription).where(
                TenantSubscription.tenant_id == self.tenant_id, TenantSubscription.is_current.is_(True)
            )).all()
            self.assertEqual([c.id for c in current], [replaced.id])
            self.assertIsNotNone(db.get(TenantSubscription, existing.id))  # history is kept

    def test_subscription_endpoint_permissions_and_tenant_isolation(self) -> None:
        self.assertEqual(self.client.get("/api/v1/tenant/subscription").status_code, 401)
        self.login("viewer@example.com", "another correct battery")
        denied = self.client.get(
            "/api/v1/tenant/subscription", headers={"X-Tenant-ID": str(self.tenant_id)}
        )
        self.assertEqual(denied.status_code, 403)
        foreign = self.client.get(
            "/api/v1/tenant/subscription", headers={"X-Tenant-ID": str(self.other_tenant_id)}
        )
        self.assertEqual(foreign.status_code, 403)

        self.client.cookies.clear()
        self.login("admin@example.com", "correct horse battery")
        response = self.client.get(
            "/api/v1/tenant/subscription", headers={"X-Tenant-ID": str(self.tenant_id)}
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["plan_code"], body["plan_version"]), ("standard", 1))
        quotas = {item["key"]: item for item in body["quotas"]}
        self.assertEqual((quotas["seats"]["limit"], quotas["seats"]["used"]), (3, 2))
        self.assertIsNone(quotas["storage_gb"]["used"])  # no counter registered yet
        foreign_header = self.client.get(
            "/api/v1/tenant/subscription", headers={"X-Tenant-ID": str(self.other_tenant_id)}
        )
        self.assertEqual(foreign_header.status_code, 403)

    def test_seat_limit_comes_from_subscription_and_returns_quota_code(self) -> None:
        headers = self.login("admin@example.com", "correct horse battery") | {
            "X-Tenant-ID": str(self.tenant_id)
        }
        with self.session_factory.begin() as db:
            subscription = entitlements.get_subscription(db, self.tenant_id)
            subscription.quotas = {**subscription.quotas, "seats": 2}
            # The legacy column no longer drives the limit.
            db.get(Tenant, self.tenant_id).seat_limit = 100
        response = self.client.post(
            "/api/v1/members/invitations",
            json={"emails": ["new@example.com"], "role": "viewer", "organization_id": self.organization_id},
            headers=headers,
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["code"], "plan.quota_exceeded")
        self.assertIn("席位已满", response.json()["detail"])

    def test_platform_tenant_creation_writes_subscription_snapshot(self) -> None:
        headers = self.login("platform@example.com", "platform admin safe password")
        response = self.client.post(
            "/api/v1/platform/tenants",
            json={
                "name": "Fresh Co", "slug": "fresh-co", "plan": "professional", "region": "ap_singapore",
                "seat_limit": 12, "admin_name": "Fresh Admin", "admin_email": "fresh@example.com",
                "admin_password": "fresh admin safe password",
            },
            headers=headers,
        )
        self.assertEqual(response.status_code, 201, response.text)
        tenant_id = UUID(response.json()["id"])
        with self.session_factory() as db:
            subscription = db.scalar(select(TenantSubscription).where(TenantSubscription.tenant_id == tenant_id))
            self.assertEqual(subscription.quotas["seats"], 12)
            self.assertEqual(subscription.quotas["assistants"], 30)
            self.assertTrue(subscription.entitlements["mcp"])
            self.assertEqual(
                db.scalar(select(AuditEvent.action).where(AuditEvent.action == "tenant.created")),
                "tenant.created",
            )


if __name__ == "__main__":
    unittest.main()
