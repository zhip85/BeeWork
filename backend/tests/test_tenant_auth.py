import unittest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from beework.auth import ACCESS_COOKIE
from beework.db import get_db
from beework.main import app
from beework.models import Base, Membership, Tenant, User
from beework.security import hash_password


class TenantAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        cls.session_factory = sessionmaker(
            bind=cls.engine,
            expire_on_commit=False,
        )

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
            tenant = Tenant(name="Acme", slug="acme")
            other_tenant = Tenant(name="Other", slug="other")
            admin = User(
                email="admin@example.com",
                display_name="Admin",
                password_hash=hash_password("correct horse battery"),
                is_platform_admin=True,
            )
            member = User(
                email="member@example.com",
                display_name="Member",
                password_hash=hash_password("another correct battery"),
            )
            db.add_all([tenant, other_tenant, admin, member])
            db.flush()
            db.add_all(
                [
                    Membership(tenant_id=tenant.id, user_id=admin.id, role="tenant_admin"),
                    Membership(tenant_id=tenant.id, user_id=member.id, role="member"),
                ]
            )
            self.tenant_id = str(tenant.id)
            self.other_tenant_id = str(other_tenant.id)
            self.admin_id = str(admin.id)
            self.member_id = str(member.id)

    def login(
        self,
        email: str = "admin@example.com",
        password: str = "correct horse battery",
    ) -> None:
        response = self.client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_login_sets_http_only_session_and_returns_tenants(self) -> None:
        response = self.client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@example.com",
                "password": "correct horse battery",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("httponly", response.headers["set-cookie"].lower())
        self.assertEqual(response.json()["memberships"][0]["slug"], "acme")
        self.assertIn(ACCESS_COOKIE, self.client.cookies)

    def test_invalid_password_is_rejected(self) -> None:
        response = self.client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": "wrong password"},
        )

        self.assertEqual(response.status_code, 401)

    def test_member_list_is_scoped_and_role_protected(self) -> None:
        self.login(
            email="member@example.com",
            password="another correct battery",
        )
        denied = self.client.get(
            "/api/v1/members",
            headers={"X-Tenant-ID": self.tenant_id},
        )
        wrong_tenant = self.client.get(
            "/api/v1/members",
            headers={"X-Tenant-ID": self.other_tenant_id},
        )
        platform_list = self.client.get("/api/v1/platform/tenants")

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(wrong_tenant.status_code, 403)
        self.assertEqual(platform_list.status_code, 403)

        self.client.post("/api/v1/auth/logout", headers=self.csrf_headers())
        self.login()
        allowed = self.client.get(
            "/api/v1/members",
            headers={"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(len(allowed.json()["members"]), 2)

    def test_csrf_and_last_tenant_admin_protection(self) -> None:
        self.login()
        denied = self.client.patch(
            f"/api/v1/members/{self.admin_id}/role",
            json={"role": "member"},
            headers={"X-Tenant-ID": self.tenant_id},
        )
        protected = self.client.patch(
            f"/api/v1/members/{self.admin_id}/role",
            json={"role": "member"},
            headers={
                **self.csrf_headers(),
                "X-Tenant-ID": self.tenant_id,
            },
        )

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(protected.status_code, 409)
        self.assertEqual(
            self.client.get("/api/v1/auth/me").status_code,
            200,
        )

    def test_platform_admin_can_create_tenant(self) -> None:
        self.login()
        rejected = self.client.post(
            "/api/v1/platform/tenants",
            json={"name": "Rejected", "slug": "rejected-tenant"},
            headers={
                **self.csrf_headers(),
                "Origin": "https://attacker.invalid",
            },
        )
        response = self.client.post(
            "/api/v1/platform/tenants",
            json={"name": "New Customer", "slug": "new-customer"},
            headers=self.csrf_headers(),
        )

        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["role"], "tenant_admin")
        tenants = self.client.get("/api/v1/platform/tenants")
        self.assertEqual(tenants.status_code, 200)
        self.assertEqual(
            {tenant["slug"] for tenant in tenants.json()},
            {"acme", "new-customer", "other"},
        )

    def csrf_headers(self) -> dict[str, str]:
        csrf_token = self.client.cookies.get("beework_csrf")
        self.assertIsNotNone(csrf_token)
        return {"X-CSRF-Token": csrf_token}


if __name__ == "__main__":
    unittest.main()
