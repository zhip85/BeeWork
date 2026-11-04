import shutil
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from beework.db import get_db
from beework.main import app
from beework.models import Base, Membership, Organization, Tenant, TenantModelEnvironment, User
from beework.security import hash_password
from isolation import disable_external_integrations, stub_public_dns

PASSWORD = "correct horse battery"


class EnvironmentContextTests(unittest.TestCase):
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
            enterprise = Tenant(name="Big Co", slug="bigco", plan="enterprise", seat_limit=20)
            standard = Tenant(name="Small Co", slug="smallco", plan="standard", seat_limit=5)
            db.add_all([enterprise, standard])
            db.flush()
            roots = {t.id: Organization(tenant_id=t.id, name=t.name) for t in (enterprise, standard)}
            db.add_all(roots.values())
            db.flush()
            admin = User(email="admin@example.com", display_name="Admin", password_hash=hash_password(PASSWORD))
            small = User(email="small@example.com", display_name="Small", password_hash=hash_password(PASSWORD))
            db.add_all([admin, small])
            db.flush()
            db.add_all([
                Membership(tenant_id=enterprise.id, user_id=admin.id, organization_id=roots[enterprise.id].id, role="tenant_admin"),
                Membership(tenant_id=standard.id, user_id=small.id, organization_id=roots[standard.id].id, role="tenant_admin"),
            ])
            db.add_all([
                TenantModelEnvironment(tenant_id=enterprise.id, environment="production", gateway_sync_status="synced"),
                TenantModelEnvironment(tenant_id=enterprise.id, environment="staging", gateway_sync_status="failed"),
                TenantModelEnvironment(tenant_id=standard.id, environment="production", gateway_sync_status="pending"),
            ])
            self.ids = {"enterprise": enterprise.id, "standard": standard.id}

    def login(self, email: str, tenant) -> dict[str, str]:
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).status_code, 200)
        return {"X-Tenant-ID": str(tenant)}

    def environments(self, headers: dict[str, str]):
        return self.client.get("/api/v1/tenant/environments", headers=headers)

    def test_membership_carries_the_plan_and_the_environments_it_offers(self) -> None:
        self.login("admin@example.com", self.ids["enterprise"])
        enterprise = self.client.get("/api/v1/auth/me").json()["memberships"][0]
        self.assertEqual((enterprise["plan"], enterprise["environments"]), ("enterprise", ["development", "staging", "production"]))
        self.login("small@example.com", self.ids["standard"])
        standard = self.client.get("/api/v1/auth/me").json()["memberships"][0]
        self.assertEqual((standard["plan"], standard["environments"]), ("standard", ["production"]))

    def test_enterprise_can_select_any_environment_and_defaults_to_production(self) -> None:
        headers = self.login("admin@example.com", self.ids["enterprise"])
        body = self.environments(headers).json()
        self.assertEqual(body["current"], "production")
        self.assertEqual([(e["environment"], e["available"]) for e in body["environments"]], [("development", True), ("staging", True), ("production", True)])
        for chosen in ("development", "staging", "production", "Staging"):
            self.assertEqual(self.environments({**headers, "X-Environment": chosen}).json()["current"], chosen.lower())

    def test_health_follows_the_gateway_sync_state(self) -> None:
        headers = self.login("admin@example.com", self.ids["enterprise"])
        status = {e["environment"]: e["gateway_sync_status"] for e in self.environments(headers).json()["environments"]}
        self.assertEqual(status, {"development": "not_configured", "staging": "failed", "production": "synced"})

    def test_a_plan_without_the_environment_gets_403_for_staging_and_development(self) -> None:
        headers = self.login("small@example.com", self.ids["standard"])
        for chosen in ("staging", "development"):
            response = self.environments({**headers, "X-Environment": chosen})
            self.assertEqual((response.status_code, response.json()["code"]), (403, "environment.unavailable"), chosen)
        ok = self.environments({**headers, "X-Environment": "production"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual([(e["environment"], e["available"]) for e in ok.json()["environments"]], [("development", False), ("staging", False), ("production", True)])
        self.assertEqual(ok.json()["environments"][2]["gateway_sync_status"], "pending")

    def test_an_unknown_environment_is_a_validation_error(self) -> None:
        headers = self.login("admin@example.com", self.ids["enterprise"])
        response = self.environments({**headers, "X-Environment": "prod"})
        self.assertEqual((response.status_code, response.json()["code"]), (422, "environment.invalid"))

    def test_access_rules_still_apply(self) -> None:
        headers = self.login("small@example.com", self.ids["standard"])
        other = self.environments({"X-Tenant-ID": str(self.ids["enterprise"])})  # not a member of that workspace
        self.assertEqual(other.status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.environments(headers).status_code, 401)


if __name__ == "__main__":
    unittest.main()
