import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from beework.audit_maintenance import prune_expired_audit_events, verify_audit_chain
from beework.auth import ACCESS_COOKIE
from beework.db import get_db
from beework.main import app
from beework.model_secrets import decrypt_model_api_key
from beework.models import (
    AuditEvent,
    Base,
    MemberInvitation,
    Membership,
    Organization,
    PlatformModel,
    PlatformModelTenant,
    Tenant,
    TenantModelEnvironment,
    User,
)
from beework.models.assistants import Assistant
from beework.models.jobs import Job, RunEvent
from beework.models.runtime import Run
from beework.security import hash_password
from isolation import disable_external_integrations, stub_public_dns


class TenantAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        disable_external_integrations()
        stub_public_dns()
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(cls.engine, "connect")
        def enable_sqlite_foreign_keys(connection, _record) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

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
        connection = self.engine.raw_connection()
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        connection.commit()
        cursor.close()
        connection.close()
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        connection = self.engine.raw_connection()
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        connection.commit()
        cursor.close()
        connection.close()
        self.client = TestClient(app)
        with self.session_factory.begin() as db:
            tenant = Tenant(name="Acme", slug="acme")
            other_tenant = Tenant(name="Other", slug="other")
            admin = User(
                email="admin@example.com",
                display_name="Admin",
                password_hash=hash_password("correct horse battery"),
                is_platform_admin=False,
            )
            platform_admin = User(
                email="platform@example.com",
                display_name="Platform Admin",
                password_hash=hash_password("platform admin safe password"),
                is_platform_admin=True,
            )
            member = User(
                email="member@example.com",
                display_name="Member",
                password_hash=hash_password("another correct battery"),
            )
            db.add_all([tenant, other_tenant, admin, platform_admin, member])
            db.flush()
            organization = Organization(tenant_id=tenant.id, name=tenant.name)
            other_organization = Organization(
                tenant_id=other_tenant.id,
                name=other_tenant.name,
            )
            db.add_all([organization, other_organization])
            db.flush()
            db.add_all(
                [
                    Membership(
                        tenant_id=tenant.id,
                        user_id=admin.id,
                        organization_id=organization.id,
                        role="tenant_admin",
                    ),
                    Membership(
                        tenant_id=tenant.id,
                        user_id=member.id,
                        organization_id=organization.id,
                        role="viewer",
                    ),
                ]
            )
            self.tenant_id = str(tenant.id)
            self.other_tenant_id = str(other_tenant.id)
            self.organization_id = str(organization.id)
            self.other_organization_id = str(other_organization.id)
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

    def test_locale_preference_is_saved_audited_and_validated(self) -> None:
        anonymous = self.client.patch("/api/v1/auth/me/preferences", json={"locale": "en"})
        self.assertEqual(anonymous.status_code, 403)
        self.login()
        self.assertIsNone(self.client.get("/api/v1/auth/me").json()["locale"])
        csrf = {"X-CSRF-Token": self.client.cookies.get("beework_csrf")}

        missing_csrf = self.client.patch("/api/v1/auth/me/preferences", json={"locale": "en"})
        self.assertEqual(missing_csrf.status_code, 403)
        invalid = self.client.patch(
            "/api/v1/auth/me/preferences", json={"locale": "fr"}, headers=csrf
        )
        self.assertEqual(invalid.status_code, 422)

        saved = self.client.patch(
            "/api/v1/auth/me/preferences", json={"locale": "en"}, headers=csrf
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["locale"], "en")
        self.assertEqual(self.client.get("/api/v1/auth/me").json()["locale"], "en")
        with self.session_factory() as db:
            events = db.scalars(
                select(AuditEvent).where(AuditEvent.action == "auth.preferences_updated")
            ).all()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].details, {"locale": "en"})

    def test_runtime_demo_requires_login(self) -> None:
        denied = self.client.post("/api/v1/runtime/demo", json={"message": "hello"})
        self.assertEqual(denied.status_code, 401)

        self.login()
        allowed = self.client.post(
            "/api/v1/runtime/demo",
            json={"message": "hello"},
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(allowed.json()["result"], "LangGraph is ready: hello")

    def test_run_event_stream_is_tenant_scoped(self) -> None:
        own_run, foreign_run = uuid4(), uuid4()
        with self.session_factory.begin() as db:
            for run_id, tenant_id, organization_id in (
                (own_run, self.tenant_id, self.organization_id),
                (foreign_run, self.other_tenant_id, self.other_organization_id),
            ):
                assistant = Assistant(
                    tenant_id=UUID(tenant_id), organization_id=UUID(organization_id), name="Runner"
                )
                db.add(assistant)
                db.flush()
                db.add(Run(id=run_id, tenant_id=UUID(tenant_id), assistant_id=assistant.id))
                for seq, kind in enumerate(("queued", "token", "done"), start=1):
                    db.add(
                        RunEvent(
                            run_id=run_id,
                            tenant_id=UUID(tenant_id),
                            seq=seq,
                            type=kind,
                            data={"seq": seq},
                        )
                    )
        headers = {"X-Tenant-ID": self.tenant_id}
        anonymous = self.client.get(f"/api/v1/runs/{own_run}/events", headers=headers)
        self.assertEqual(anonymous.status_code, 401)

        self.login()
        streamed = self.client.get(f"/api/v1/runs/{own_run}/events", headers=headers)
        self.assertEqual(streamed.status_code, 200, streamed.text)
        self.assertEqual(streamed.headers["content-type"].split(";")[0], "text/event-stream")
        self.assertEqual(
            [line for line in streamed.text.splitlines() if line.startswith("event:")],
            ["event: queued", "event: token", "event: done"],
        )
        resumed = self.client.get(f"/api/v1/runs/{own_run}/events?after_seq=2", headers=headers)
        self.assertEqual([line for line in resumed.text.splitlines() if line.startswith("id:")], ["id: 3"])

        # Another tenant's run, and a run that does not exist, look identical: 404.
        foreign = self.client.get(f"/api/v1/runs/{foreign_run}/events", headers=headers)
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign.json()["code"], "not_found")
        unknown = self.client.get(f"/api/v1/runs/{uuid4()}/events", headers=headers)
        self.assertEqual(unknown.status_code, 404)

    def test_platform_jobs_are_platform_only_and_retry_is_audited(self) -> None:
        with self.session_factory.begin() as db:
            job = Job(kind="noop.echo", payload={}, status="dead", attempts=5)
            db.add(job)
            db.flush()
            job_id = str(job.id)

        self.login()
        denied = self.client.get("/api/v1/platform/jobs")
        self.assertEqual(denied.status_code, 403, denied.text)

        self.client.post("/api/v1/auth/logout", headers=self.csrf_headers())
        self.login(email="platform@example.com", password="platform admin safe password")
        listed = self.client.get("/api/v1/platform/jobs")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["total"], 1)
        retried = self.client.post(
            f"/api/v1/platform/jobs/{job_id}/retry", headers=self.csrf_headers()
        )
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(retried.json()["status"], "queued")
        with self.session_factory() as db:
            self.assertEqual(db.get(Job, UUID(job_id)).status, "queued")
            self.assertEqual(
                db.scalar(select(AuditEvent.action).where(AuditEvent.action == "job.retried")),
                "job.retried",
            )

    def test_audit_list_filters_and_export_permissions(self) -> None:
        self.login()
        created = self.client.post(
            "/api/v1/organizations",
            json={"name": "审计测试部门"},
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(created.status_code, 201, created.text)

        events = self.client.get(
            "/api/v1/audit/events",
            params={"category": "members_permissions", "search": "审计测试部门"},
            headers={"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(events.status_code, 200, events.text)
        payload = events.json()
        self.assertEqual(payload["total"], 1, payload)
        self.assertEqual(payload["events"][0]["category"], "members_permissions")
        self.assertEqual(payload["events"][0]["description"], "创建组织：审计测试部门")
        self.assertEqual(payload["events"][0]["actor_email"], "admin@example.com")
        self.assertIsNotNone(payload["events"][0]["source_ip"])
        integrity = self.client.get(
            "/api/v1/audit/integrity",
            headers={"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(integrity.status_code, 200, integrity.text)
        self.assertTrue(integrity.json()["is_valid"])

        export = self.client.get(
            "/api/v1/audit/events/export",
            params={"category": "members_permissions", "search": "审计测试部门"},
            headers={"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(export.status_code, 200, export.text)
        self.assertIn("text/csv", export.headers["content-type"])
        self.assertIn("创建组织", export.content.decode("utf-8-sig"))

        self.client.post("/api/v1/auth/logout", headers=self.csrf_headers())
        self.login(email="member@example.com", password="another correct battery")
        denied = self.client.get(
            "/api/v1/audit/events",
            headers={"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(denied.status_code, 403)

        self.client.post("/api/v1/auth/logout", headers=self.csrf_headers())
        with self.session_factory.begin() as db:
            auditor = User(
                email="auditor@example.com",
                display_name="Auditor",
                password_hash=hash_password("auditor correct battery"),
            )
            db.add(auditor)
            db.flush()
            db.add(
                Membership(
                    tenant_id=UUID(self.tenant_id),
                    user_id=auditor.id,
                    organization_id=UUID(self.organization_id),
                    role="auditor",
                )
            )
        self.login(email="auditor@example.com", password="auditor correct battery")
        allowed = self.client.get(
            "/api/v1/audit/events",
            headers={"X-Tenant-ID": self.tenant_id},
        )
        organizations = self.client.get(
            "/api/v1/organizations",
            headers={"X-Tenant-ID": self.tenant_id},
        )
        wrong_tenant = self.client.get(
            "/api/v1/audit/events",
            headers={"X-Tenant-ID": self.other_tenant_id},
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(organizations.status_code, 403)
        self.assertEqual(wrong_tenant.status_code, 403)

    def test_audit_events_cannot_be_updated_or_deleted_through_orm(self) -> None:
        with self.session_factory() as db:
            entry = AuditEvent(
                tenant_id=UUID(self.tenant_id),
                actor_user_id=UUID(self.admin_id),
                action="organization.created",
                details={"name": "部门"},
            )
            db.add(entry)
            db.commit()
            entry.description = "被篡改"
            with self.assertRaisesRegex(ValueError, "append-only"):
                db.commit()
            db.rollback()
            db.delete(entry)
            with self.assertRaisesRegex(ValueError, "append-only"):
                db.commit()

    def test_audit_chain_detects_tampering_and_retention_keeps_checkpoint(self) -> None:
        chain_scope = f"tenant:{self.tenant_id}"
        now = datetime.now(UTC)
        with self.session_factory() as db:
            db.add_all(
                [
                    AuditEvent(
                        tenant_id=UUID(self.tenant_id),
                        actor_user_id=UUID(self.admin_id),
                        action="organization.created",
                        details={"name": "過期 1"},
                        created_at=now - timedelta(days=400),
                    ),
                    AuditEvent(
                        tenant_id=UUID(self.tenant_id),
                        actor_user_id=UUID(self.admin_id),
                        action="organization.created",
                        details={"name": "過期 2"},
                        created_at=now - timedelta(days=390),
                    ),
                ]
            )
            db.commit()
            db.add(
                AuditEvent(
                    tenant_id=UUID(self.tenant_id),
                    actor_user_id=UUID(self.admin_id),
                    action="organization.created",
                    details={"name": "近期"},
                    created_at=now - timedelta(days=2),
                )
            )
            db.commit()
            self.assertTrue(verify_audit_chain(db, chain_scope)["is_valid"])
            pruned = prune_expired_audit_events(db, now=now)
            self.assertEqual(pruned, 2)
            valid = verify_audit_chain(db, chain_scope)
            self.assertTrue(valid["is_valid"], valid)
            self.assertEqual(valid["checked_events"], 1)
            self.assertEqual(valid["through_sequence"], 3)
            db.execute(
                update(AuditEvent)
                .where(AuditEvent.chain_scope == chain_scope)
                .values(description="伪造变更")
            )
            db.commit()
            tampered = verify_audit_chain(db, chain_scope)
            self.assertFalse(tampered["is_valid"], tampered)

    def test_platform_audit_is_separate_and_platform_only(self) -> None:
        self.login(
            email="platform@example.com",
            password="platform admin safe password",
        )
        events = self.client.get("/api/v1/platform/audit/events")
        export = self.client.get("/api/v1/platform/audit/events/export")
        tenant_audit = self.client.get(
            "/api/v1/audit/events",
            headers={"X-Tenant-ID": self.tenant_id},
        )
        integrity = self.client.get("/api/v1/platform/audit/integrity")
        self.assertEqual(events.status_code, 200, events.text)
        self.assertGreaterEqual(events.json()["total"], 1)
        self.assertEqual(events.json()["events"][0]["description"], "登录成功")
        self.assertEqual(export.status_code, 200, export.text)
        self.assertIn("text/csv", export.headers["content-type"])
        self.assertEqual(tenant_audit.status_code, 403)
        self.assertEqual(integrity.status_code, 200, integrity.text)
        self.assertEqual(integrity.json()["chain_scope"], "platform")

        self.client.post("/api/v1/auth/logout", headers=self.csrf_headers())
        self.login()
        denied = self.client.get("/api/v1/platform/audit/events")
        self.assertEqual(denied.status_code, 403)

    def test_platform_model_catalog_encrypts_credentials_and_is_platform_only(self) -> None:
        payload = {
            "display_name": "Qwen Test",
            "provider": "alibaba_qwen",
            "model_type": "chat",
            "availability": "allowlist",
            "input_price_per_million": "0.500000",
            "output_price_per_million": "1.000000",
            "input_points_per_thousand": "1.000000",
            "output_points_per_thousand": "2.000000",
            "status": "healthy",
            "provider_model_id": "qwen-test-model",
            "endpoint_url": "https://models.example.test/v1",
            "api_key": "test-provider-secret",
            "region": "ap_singapore",
            "context_window": 32768,
            "max_output_tokens": 4096,
            "default_temperature": "0.70",
            "rpm_limit": 120,
            "allowlist_tenant_ids": [self.tenant_id],
        }
        self.login()
        denied = self.client.get("/api/v1/platform/models")
        self.assertEqual(denied.status_code, 403)
        self.client.post("/api/v1/auth/logout", headers=self.csrf_headers())

        self.login(
            email="platform@example.com",
            password="platform admin safe password",
        )
        created = self.client.post(
            "/api/v1/platform/models",
            json=payload,
            headers=self.csrf_headers(),
        )
        self.assertEqual(created.status_code, 201, created.text)
        model_response = created.json()
        model_id = model_response["id"]
        self.assertTrue(model_response["credential_configured"])
        self.assertTrue(model_response["api_key_masked"].endswith("cret"))
        self.assertNotIn("api_key", model_response)
        self.assertNotIn("encrypted_api_key", model_response)
        self.assertEqual(model_response["allowlist_tenant_ids"], [self.tenant_id])
        # No gateway is configured in unit tests: the catalog still saves, flagged as unsynced.
        self.assertEqual(model_response["gateway_sync_status"], "not_configured")
        test_without_gateway = self.client.post(
            f"/api/v1/platform/models/{model_id}/test",
            json={"access_only": True},
            headers=self.csrf_headers(),
        )
        self.assertEqual(test_without_gateway.status_code, 409, test_without_gateway.text)
        with self.session_factory() as db:
            stored = db.get(PlatformModel, UUID(model_id))
            self.assertIsNotNone(stored)
            self.assertNotEqual(stored.encrypted_api_key, payload["api_key"])
            encrypted_key = stored.encrypted_api_key
            self.assertEqual(decrypt_model_api_key(encrypted_key), payload["api_key"])

        updated_payload = {key: value for key, value in payload.items() if key != "api_key"}
        updated_payload["status"] = "disabled"
        updated = self.client.put(
            f"/api/v1/platform/models/{model_id}",
            json=updated_payload,
            headers=self.csrf_headers(),
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["status"], "disabled")
        with self.session_factory() as db:
            stored = db.get(PlatformModel, UUID(model_id))
            self.assertEqual(stored.encrypted_api_key, encrypted_key)
            self.assertEqual(decrypt_model_api_key(stored.encrypted_api_key), payload["api_key"])
        self.assertEqual(
            self.client.get(
                "/api/v1/platform/models",
                params={"status": "disabled"},
            ).json()["models"][0]["id"],
            model_id,
        )
        invalid_endpoint = self.client.post(
            "/api/v1/platform/models",
            json={**payload, "display_name": "Unsafe Endpoint", "endpoint_url": "http://127.0.0.1"},
            headers=self.csrf_headers(),
        )
        self.assertEqual(invalid_endpoint.status_code, 422)

    def test_tenant_model_environment_filters_and_saves_defaults(self) -> None:
        def make_model(name: str, model_type: str, **overrides) -> PlatformModel:
            values = {
                "display_name": name,
                "provider": "private",
                "model_type": model_type,
                "availability": "all_plans",
                "input_price_per_million": "1",
                "output_price_per_million": "2",
                "input_points_per_thousand": "1",
                "output_points_per_thousand": "2",
                "status": "healthy",
                "provider_model_id": name.lower().replace(" ", "-"),
                "endpoint_url": "https://models.example.test/v1",
                "encrypted_api_key": "encrypted-provider-secret",
                "api_key_last_four": "cret",
                "region": "ap_singapore",
            }
            values.update(overrides)
            return PlatformModel(**values)

        with self.session_factory.begin() as db:
            chat = make_model("Available Chat", "chat")
            embedding = make_model("Available Embedding", "embedding")
            rerank = make_model("Available Rerank", "rerank")
            wrong_region = make_model("Wrong Region", "chat", region="eu_west")
            unavailable_plan = make_model(
                "Professional Only", "chat", availability="professional_plus"
            )
            canary = make_model("Canary Model", "chat", status="canary")
            db.add_all([chat, embedding, rerank, wrong_region, unavailable_plan, canary])
            db.flush()
            ids = {
                "chat": str(chat.id),
                "embedding": str(embedding.id),
                "rerank": str(rerank.id),
                "wrong_region": str(wrong_region.id),
                "unavailable_plan": str(unavailable_plan.id),
                "canary": str(canary.id),
            }

        self.login()
        headers = {"X-Tenant-ID": self.tenant_id}
        initial = self.client.get(
            "/api/v1/tenant/model-environments/production",
            headers=headers,
        )
        self.assertEqual(initial.status_code, 200, initial.text)
        initial_data = initial.json()
        self.assertEqual(initial_data["plan"], "standard")
        self.assertEqual(initial_data["region"], "ap_singapore")
        self.assertEqual(initial_data["version"], 0)
        available_ids = {model["id"] for model in initial_data["models"]}
        self.assertEqual(
            available_ids,
            {ids["chat"], ids["embedding"], ids["rerank"]},
        )
        self.assertNotIn("encrypted_api_key", initial.text)
        self.assertNotIn("endpoint_url", initial.text)

        saved = self.client.put(
            "/api/v1/tenant/model-environments/production",
            json={
                "expected_version": 0,
                "enabled_model_ids": [ids["chat"], ids["embedding"], ids["rerank"]],
                "default_model_ids": {
                    "chat": ids["chat"],
                    "embedding": ids["embedding"],
                    "rerank": ids["rerank"],
                },
            },
            headers=self.csrf_headers() | headers,
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["version"], 1)
        self.assertEqual(saved.json()["default_model_ids"]["chat"], ids["chat"])
        self.assertEqual(saved.json()["available_environments"], ["production"])

        standard_staging = self.client.get(
            "/api/v1/tenant/model-environments/staging",
            headers=headers,
        )
        self.assertEqual(standard_staging.status_code, 403, standard_staging.text)
        with self.session_factory.begin() as db:
            db.get(Tenant, UUID(self.tenant_id)).plan = "enterprise"
        enterprise_staging = self.client.get(
            "/api/v1/tenant/model-environments/staging",
            headers=headers,
        )
        self.assertEqual(enterprise_staging.status_code, 200, enterprise_staging.text)
        self.assertEqual(
            enterprise_staging.json()["available_environments"],
            ["development", "staging", "production"],
        )

        stale_update = self.client.put(
            "/api/v1/tenant/model-environments/production",
            json={
                "expected_version": 0,
                "enabled_model_ids": [ids["chat"]],
                "default_model_ids": {
                    "chat": ids["chat"],
                    "embedding": None,
                    "rerank": None,
                },
            },
            headers=self.csrf_headers() | headers,
        )
        wrong_default_type = self.client.put(
            "/api/v1/tenant/model-environments/staging",
            json={
                "expected_version": 0,
                "enabled_model_ids": [ids["chat"], ids["embedding"]],
                "default_model_ids": {
                    "chat": ids["chat"],
                    "embedding": ids["chat"],
                    "rerank": None,
                },
            },
            headers=self.csrf_headers() | headers,
        )
        other_tenant_model = make_model(
            "Other Tenant Model", "chat", availability="allowlist"
        )
        with self.session_factory.begin() as db:
            db.add(other_tenant_model)
            db.flush()
            db.add(
                PlatformModelTenant(
                    model_id=other_tenant_model.id,
                    tenant_id=UUID(self.other_tenant_id),
                )
            )
            foreign_id = str(other_tenant_model.id)
        foreign_selection = self.client.put(
            "/api/v1/tenant/model-environments/staging",
            json={
                "expected_version": 0,
                "enabled_model_ids": [foreign_id],
                "default_model_ids": {
                    "chat": foreign_id,
                    "embedding": None,
                    "rerank": None,
                },
            },
            headers=self.csrf_headers() | headers,
        )
        self.assertEqual(stale_update.status_code, 409)
        self.assertEqual(wrong_default_type.status_code, 422)
        self.assertEqual(foreign_selection.status_code, 422)

        with self.session_factory.begin() as db:
            environment = db.scalar(
                select(TenantModelEnvironment).where(
                    TenantModelEnvironment.tenant_id == UUID(self.tenant_id),
                    TenantModelEnvironment.environment == "production",
                )
            )
            self.assertEqual(environment.version, 1)

        self.client.post("/api/v1/auth/logout", headers=self.csrf_headers())
        self.login(email="member@example.com", password="another correct battery")
        denied = self.client.put(
            "/api/v1/tenant/model-environments/staging",
            json={
                "expected_version": 0,
                "enabled_model_ids": [],
                "default_model_ids": {
                    "chat": None,
                    "embedding": None,
                    "rerank": None,
                },
            },
            headers=self.csrf_headers() | headers,
        )
        self.assertEqual(denied.status_code, 403)

    def test_auditor_cannot_read_model_settings(self) -> None:
        # PRD 6.1: auditors see audit and governance settings, not models / BYOK.
        with self.session_factory.begin() as db:
            membership = db.scalar(
                select(Membership).where(Membership.user_id == UUID(self.member_id))
            )
            membership.role = "auditor"
        self.login(email="member@example.com", password="another correct battery")
        headers = {"X-Tenant-ID": self.tenant_id}
        me = self.client.get("/api/v1/auth/me").json()
        self.assertNotIn("models:view", me["memberships"][0]["permissions"])
        self.assertEqual(
            self.client.get("/api/v1/tenant/model-environments/production", headers=headers).status_code,
            403,
        )
        self.assertEqual(self.client.get("/api/v1/audit/events", headers=headers).status_code, 200)

    def test_invalid_password_is_rejected(self) -> None:
        response = self.client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": "wrong password"},
        )

        self.assertEqual(response.status_code, 401)
        self.login()
        failed_logins = self.client.get(
            "/api/v1/audit/events",
            params={"category": "security", "result": "failure"},
            headers={"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(failed_logins.status_code, 200, failed_logins.text)
        self.assertEqual(failed_logins.json()["total"], 1)
        self.assertEqual(failed_logins.json()["events"][0]["description"], "登录失败：admin@example.com")

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
        platform_overview = self.client.get("/api/v1/platform/overview")
        platform_audit = self.client.get("/api/v1/platform/audit/events")
        platform_audit_export = self.client.get("/api/v1/platform/audit/events/export")

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(wrong_tenant.status_code, 403)
        self.assertEqual(platform_list.status_code, 403)
        self.assertEqual(platform_overview.status_code, 403)
        self.assertEqual(platform_audit.status_code, 403)
        self.assertEqual(platform_audit_export.status_code, 403)
        organizations = self.client.get(
            "/api/v1/organizations",
            headers={"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(organizations.status_code, 403)

        self.client.post("/api/v1/auth/logout", headers=self.csrf_headers())
        self.login()
        allowed = self.client.get(
            "/api/v1/members",
            headers={"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(len(allowed.json()["members"]), 2)
        role_updated = self.client.patch(
            f"/api/v1/members/{self.member_id}/role",
            json={"role": "auditor"},
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        invalid_role = self.client.patch(
            f"/api/v1/members/{self.member_id}/role",
            json={"role": "approver"},
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(role_updated.status_code, 200, role_updated.text)
        self.assertEqual(role_updated.json()["role"], "auditor")
        self.assertEqual(role_updated.json()["organization_id"], self.organization_id)
        self.assertEqual(invalid_role.status_code, 422)

    def test_csrf_and_self_role_change_protection(self) -> None:
        self.login()
        denied = self.client.patch(
            f"/api/v1/members/{self.admin_id}/role",
            json={"role": "member"},
            headers={"X-Tenant-ID": self.tenant_id},
        )
        protected = self.client.patch(
            f"/api/v1/members/{self.admin_id}/role",
            json={"role": "viewer"},
            headers={
                **self.csrf_headers(),
                "X-Tenant-ID": self.tenant_id,
            },
        )

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(protected.status_code, 409)
        self.assertEqual(protected.json()["detail"], "不能修改自己的角色")
        self.assertEqual(
            self.client.get("/api/v1/auth/me").status_code,
            200,
        )

    def test_platform_admin_can_create_tenant(self) -> None:
        self.login(
            email="platform@example.com",
            password="platform admin safe password",
        )
        rejected = self.client.post(
            "/api/v1/platform/tenants",
            json={
                "name": "Rejected",
                "slug": "rejected-tenant",
                "seat_limit": 8,
                "admin_email": "rejected-admin@example.com",
                "admin_name": "Rejected Admin",
                "admin_password": "a-long-safe-password",
            },
            headers={
                **self.csrf_headers(),
                "Origin": "https://attacker.invalid",
            },
        )
        response = self.client.post(
            "/api/v1/platform/tenants",
            json={
                "name": "New Customer",
                "slug": "new-customer",
                "seat_limit": 12,
                "admin_email": "new-admin@example.com",
                "admin_name": "New Admin",
                "admin_password": "a-long-safe-password",
            },
            headers=self.csrf_headers(),
        )

        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["admin_email"], "new-admin@example.com")
        self.assertEqual(response.json()["member_count"], 1)
        self.assertEqual(response.json()["seat_limit"], 12)
        tenants = self.client.get("/api/v1/platform/tenants")
        self.assertEqual(tenants.status_code, 200)
        self.assertEqual(
            {tenant["slug"] for tenant in tenants.json()},
            {"acme", "new-customer", "other"},
        )
        new_tenant = next(
            item for item in tenants.json() if item["slug"] == "new-customer"
        )
        self.assertEqual(new_tenant["admin_name"], "New Admin")
        self.assertEqual(new_tenant["member_count"], 1)
        self.assertEqual(new_tenant["seat_limit"], 12)
        overview = self.client.get("/api/v1/platform/overview")
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.json()["tenant_count"], 3)

        with self.session_factory.begin() as db:
            db.add(
                User(
                    email="reviewer@example.com",
                    display_name="Platform Reviewer",
                    password_hash=hash_password("platform reviewer safe password"),
                    is_platform_admin=True,
                )
            )
        lifecycle_url = f"/api/v1/platform/tenants/{new_tenant['id']}/lifecycle"
        missing_reason = self.client.post(
            lifecycle_url, json={"action": "suspend"}, headers=self.csrf_headers()
        )
        self.assertEqual(missing_reason.status_code, 422, missing_reason.text)
        requested = self.client.post(
            lifecycle_url,
            json={"action": "suspend", "reason": "合同到期"},
            headers=self.csrf_headers(),
        )
        self.assertEqual(requested.status_code, 202, requested.text)
        self.assertEqual(requested.json()["tenant"]["status"], "active")
        operation_id = requested.json()["operation"]["id"]
        self_review = self.client.post(
            f"/api/v1/platform/operations/{operation_id}/approve",
            json={},
            headers=self.csrf_headers(),
        )
        self.assertEqual(self_review.status_code, 403, self_review.text)
        invalid_action = self.client.post(
            lifecycle_url, json={"action": "record_payment"}, headers=self.csrf_headers()
        )
        self.assertEqual(invalid_action.status_code, 409, invalid_action.text)

        self.client.post("/api/v1/auth/logout", headers=self.csrf_headers())
        self.login(email="reviewer@example.com", password="platform reviewer safe password")
        pending = self.client.get("/api/v1/platform/operations", params={"status": "pending"})
        self.assertEqual([item["id"] for item in pending.json()], [operation_id])
        approved = self.client.post(
            f"/api/v1/platform/operations/{operation_id}/approve",
            json={"comment": "已核实"},
            headers=self.csrf_headers(),
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["status"], "succeeded")
        tenants_after = {item["slug"]: item for item in self.client.get("/api/v1/platform/tenants").json()}
        self.assertEqual(tenants_after["new-customer"]["status"], "suspended")
        self.assertEqual(
            {item["action"] for item in tenants_after["new-customer"]["available_actions"]},
            {"restore", "schedule_deletion"},
        )

        # PER-004: a suspended tenant's administrator may sign in to view and export only.
        self.client.post("/api/v1/auth/logout", headers=self.csrf_headers())
        self.login(email="new-admin@example.com", password="a-long-safe-password")
        suspended_me = self.client.get("/api/v1/auth/me").json()
        self.assertEqual(suspended_me["memberships"][0]["access_mode"], "read_only")
        new_tenant_headers = {"X-Tenant-ID": new_tenant["id"]}
        self.assertEqual(
            self.client.get("/api/v1/audit/events", headers=new_tenant_headers).status_code,
            200,
        )
        blocked_write = self.client.post(
            "/api/v1/organizations",
            json={"name": "新部门"},
            headers=self.csrf_headers() | new_tenant_headers,
        )
        self.assertEqual(blocked_write.status_code, 403, blocked_write.text)

        self.client.post("/api/v1/auth/logout", headers=self.csrf_headers())
        self.login(email="platform@example.com", password="platform admin safe password")
        reactivated = self.client.post(
            lifecycle_url, json={"action": "restore"}, headers=self.csrf_headers()
        )
        self.assertEqual(reactivated.status_code, 200, reactivated.text)
        self.assertEqual(reactivated.json()["tenant"]["status"], "active")
        platform_audit = self.client.get(
            "/api/v1/platform/audit/events",
            params={"search": "New Customer"},
        )
        self.assertEqual(platform_audit.status_code, 200, platform_audit.text)
        descriptions = [item["description"] for item in platform_audit.json()["events"]]
        self.assertIn("创建企业租户：new-customer", descriptions)
        self.assertIn("变更企业状态", descriptions)
        self.assertIn("发起租户生命周期复核", descriptions)

        self.client.post("/api/v1/auth/logout", headers=self.csrf_headers())
        self.login(
            email="new-admin@example.com",
            password="a-long-safe-password",
        )
        enterprise_user = self.client.get("/api/v1/auth/me")
        self.assertEqual(enterprise_user.status_code, 200)
        self.assertFalse(enterprise_user.json()["is_platform_admin"])
        self.assertEqual(
            enterprise_user.json()["memberships"][0]["slug"],
            "new-customer",
        )
        self.assertEqual(
            self.client.get("/api/v1/platform/tenants").status_code,
            403,
        )

    def test_platform_and_enterprise_admin_identities_are_separate(self) -> None:
        self.login()
        enterprise_user = self.client.get("/api/v1/auth/me").json()
        self.assertFalse(enterprise_user["is_platform_admin"])
        self.assertEqual(
            self.client.get("/api/v1/platform/overview").status_code,
            403,
        )
        self.client.post("/api/v1/auth/logout", headers=self.csrf_headers())

        self.login(
            email="platform@example.com",
            password="platform admin safe password",
        )
        platform_user = self.client.get("/api/v1/auth/me").json()
        self.assertTrue(platform_user["is_platform_admin"])
        self.assertEqual(platform_user["memberships"], [])
        self.assertEqual(
            self.client.get("/api/v1/platform/overview").status_code,
            200,
        )

    def test_organization_tree_and_member_scope_are_tenant_isolated(self) -> None:
        self.login()
        created = self.client.post(
            "/api/v1/organizations",
            json={"name": "工程部", "parent_id": self.organization_id},
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )

        self.assertEqual(created.status_code, 201, created.text)
        department_id = created.json()["id"]
        self.assertEqual(created.json()["depth"], 1)
        self.assertEqual(created.json()["member_count"], 0)

        moved = self.client.patch(
            f"/api/v1/members/{self.member_id}/organization",
            json={"organization_id": department_id},
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(moved.json()["organization_id"], department_id)

        organizations = self.client.get(
            "/api/v1/organizations",
            headers={"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(organizations.status_code, 200)
        department = next(
            item
            for item in organizations.json()["organizations"]
            if item["id"] == department_id
        )
        self.assertEqual(department["member_count"], 1)

        foreign_move = self.client.patch(
            f"/api/v1/members/{self.member_id}/organization",
            json={"organization_id": self.other_organization_id},
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        wrong_tenant_list = self.client.get(
            "/api/v1/organizations",
            headers={"X-Tenant-ID": self.other_tenant_id},
        )
        self.assertEqual(foreign_move.status_code, 404)
        self.assertEqual(wrong_tenant_list.status_code, 403)

    def test_member_invitation_seats_acceptance_and_one_time_token(self) -> None:
        with self.session_factory.begin() as db:
            db.get(Tenant, UUID(self.tenant_id)).seat_limit = 3
        self.login()
        created = self.client.post(
            "/api/v1/members/invitations",
            json={
                "emails": ["invitee@example.com"],
                "role": "builder",
                "organization_id": self.organization_id,
            },
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(created.status_code, 201, created.text)
        invitation = created.json()["invitations"][0]
        self.assertEqual(created.json()["seats_used"], 3)
        self.assertIn("token=", invitation["invitation_url"])
        token = invitation["invitation_url"].split("token=", 1)[1]
        with self.session_factory() as db:
            stored_invite = db.get(MemberInvitation, UUID(invitation["id"]))
            self.assertNotEqual(stored_invite.token_hash, token)
            self.assertNotIn(token, stored_invite.token_hash)

        at_limit = self.client.post(
            "/api/v1/members/invitations",
            json={
                "emails": ["another@example.com"],
                "role": "viewer",
                "organization_id": self.organization_id,
            },
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(at_limit.status_code, 409)
        self.assertIn("席位已满", at_limit.json()["detail"])

        accepted_client = TestClient(app)
        accepted = accepted_client.post(
            "/api/v1/members/invitations/accept",
            json={
                "token": token,
                "display_name": "Invitee",
                "password": "a-strong-invite-password",
            },
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["memberships"][0]["slug"], "acme")
        self.assertEqual(accepted_client.get("/api/v1/auth/me").status_code, 200)
        replayed = accepted_client.post(
            "/api/v1/members/invitations/accept",
            json={
                "token": token,
                "display_name": "Invitee",
                "password": "a-strong-invite-password",
            },
        )
        self.assertEqual(replayed.status_code, 410)
        member_list = self.client.get(
            "/api/v1/members",
            headers={"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(member_list.status_code, 200)
        self.assertEqual(member_list.json()["seats_used"], 3)
        self.assertEqual(
            next(
                item for item in member_list.json()["members"]
                if item["email"] == "invitee@example.com"
            )["status"],
            "active",
        )

    def test_invitation_resend_revokes_previous_link_and_invite_can_be_revoked(self) -> None:
        self.login()
        created = self.client.post(
            "/api/v1/members/invitations",
            json={
                "emails": ["invitee@example.com"],
                "role": "viewer",
                "organization_id": self.organization_id,
            },
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(created.status_code, 201, created.text)
        original = created.json()["invitations"][0]
        original_token = original["invitation_url"].split("token=", 1)[1]
        resent = self.client.post(
            f"/api/v1/members/invitations/{original['id']}/resend",
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(resent.status_code, 200, resent.text)
        new_token = resent.json()["invitation_url"].split("token=", 1)[1]
        self.assertNotEqual(original_token, new_token)
        invalidated = TestClient(app).post(
            "/api/v1/members/invitations/accept",
            json={
                "token": original_token,
                "display_name": "Invitee",
                "password": "a-strong-invite-password",
            },
        )
        self.assertEqual(invalidated.status_code, 410)
        revoked = self.client.delete(
            f"/api/v1/members/invitations/{original['id']}",
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(revoked.status_code, 204)
        denied = TestClient(app).post(
            "/api/v1/members/invitations/accept",
            json={
                "token": new_token,
                "display_name": "Invitee",
                "password": "a-strong-invite-password",
            },
        )
        self.assertEqual(denied.status_code, 410)

    def test_expired_invitation_is_rejected_and_can_be_resent(self) -> None:
        self.login()
        created = self.client.post(
            "/api/v1/members/invitations",
            json={
                "emails": ["expired@example.com"],
                "role": "viewer",
                "organization_id": self.organization_id,
            },
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(created.status_code, 201, created.text)
        invitation = created.json()["invitations"][0]
        token = invitation["invitation_url"].split("token=", 1)[1]
        with self.session_factory.begin() as db:
            stored_invite = db.get(MemberInvitation, UUID(invitation["id"]))
            stored_invite.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        listed = self.client.get(
            "/api/v1/members",
            headers={"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(listed.json()["invitations"][0]["status"], "expired")
        expired = TestClient(app).post(
            "/api/v1/members/invitations/accept",
            json={
                "token": token,
                "display_name": "Expired Invitee",
                "password": "a-strong-invite-password",
            },
        )
        self.assertEqual(expired.status_code, 410)
        resent = self.client.post(
            f"/api/v1/members/invitations/{invitation['id']}/resend",
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(resent.status_code, 200, resent.text)
        self.assertEqual(resent.json()["status"], "pending")

    def test_suspend_restore_remove_members_and_revoke_sessions(self) -> None:
        member_client = TestClient(app)
        member_login = member_client.post(
            "/api/v1/auth/login",
            json={
                "email": "member@example.com",
                "password": "another correct battery",
            },
        )
        self.assertEqual(member_login.status_code, 200, member_login.text)
        self.login()

        suspended = self.client.patch(
            f"/api/v1/members/{self.member_id}/status",
            json={"status": "suspended"},
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(suspended.status_code, 200, suspended.text)
        self.assertEqual(suspended.json()["status"], "suspended")
        self.assertEqual(member_client.get("/api/v1/auth/me").status_code, 401)
        self.assertEqual(
            self.client.get(
                "/api/v1/members",
                headers={"X-Tenant-ID": self.tenant_id},
            ).json()["seats_used"],
            1,
        )

        restored = self.client.patch(
            f"/api/v1/members/{self.member_id}/status",
            json={"status": "active"},
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        member_client.post(
            "/api/v1/auth/login",
            json={
                "email": "member@example.com",
                "password": "another correct battery",
            },
        )
        removed = self.client.delete(
            f"/api/v1/members/{self.member_id}",
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(removed.status_code, 204)
        self.assertEqual(member_client.get("/api/v1/auth/me").status_code, 401)

    def test_last_active_tenant_admin_cannot_be_demoted_suspended_or_removed(self) -> None:
        self.login()
        demoted = self.client.patch(
            f"/api/v1/members/{self.admin_id}/role",
            json={"role": "viewer"},
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        suspended = self.client.patch(
            f"/api/v1/members/{self.admin_id}/status",
            json={"status": "suspended"},
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        removed = self.client.delete(
            f"/api/v1/members/{self.admin_id}",
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(demoted.status_code, 409)
        self.assertEqual(suspended.status_code, 409)
        self.assertEqual(removed.status_code, 409)

    def test_custom_role_permissions_scope_and_assignment_lifecycle(self) -> None:
        self.login()
        department = self.client.post(
            "/api/v1/organizations",
            json={"name": "产品部", "parent_id": self.organization_id},
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(department.status_code, 201, department.text)
        department_id = department.json()["id"]
        invalid_scope = self.client.post(
            "/api/v1/roles",
            json={
                "name": "外租户角色",
                "scope": "organization",
                "scope_organization_id": self.other_organization_id,
                "permissions": {
                    "knowledge": ["view"],
                    "settings": ["view"],
                    "organizations": ["view"],
                },
            },
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        created = self.client.post(
            "/api/v1/roles",
            json={
                "name": "知识审阅员",
                "description": "仅查看产品部知识库",
                "scope": "organization",
                "scope_organization_id": department_id,
                "permissions": {
                    "knowledge": ["view"],
                    "settings": ["view"],
                    "organizations": ["view"],
                },
            },
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(invalid_scope.status_code, 404)
        self.assertEqual(created.status_code, 201, created.text)
        role_id = created.json()["id"]
        self.assertEqual(created.json()["key"], f"custom:{role_id}")

        out_of_scope_assignment = self.client.patch(
            f"/api/v1/members/{self.member_id}/role",
            json={"role": "custom", "custom_role_id": role_id},
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(out_of_scope_assignment.status_code, 409)

        moved = self.client.patch(
            f"/api/v1/members/{self.member_id}/organization",
            json={"organization_id": department_id},
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        assigned = self.client.patch(
            f"/api/v1/members/{self.member_id}/role",
            json={"role": "custom", "custom_role_id": role_id},
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(assigned.status_code, 200, assigned.text)
        self.assertEqual(assigned.json()["role"], "custom")
        self.assertEqual(assigned.json()["custom_role_name"], "知识审阅员")

        delete_assigned = self.client.delete(
            f"/api/v1/roles/{role_id}",
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        disable_assigned = self.client.put(
            f"/api/v1/roles/{role_id}",
            json={
                "name": "知识审阅员",
                "description": "仅查看产品部知识库",
                "scope": "organization",
                "scope_organization_id": department_id,
                "permissions": {
                    "knowledge": ["view"],
                    "settings": ["view"],
                    "organizations": ["view"],
                },
                "is_active": False,
            },
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        move_out_of_scope = self.client.patch(
            f"/api/v1/members/{self.member_id}/organization",
            json={"organization_id": self.organization_id},
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(delete_assigned.status_code, 409)
        self.assertEqual(disable_assigned.status_code, 409)
        self.assertEqual(move_out_of_scope.status_code, 409)

        self.client.post("/api/v1/auth/logout", headers=self.csrf_headers())
        self.login(email="member@example.com", password="another correct battery")
        current_user = self.client.get("/api/v1/auth/me").json()
        member_tenant = current_user["memberships"][0]
        self.assertEqual(member_tenant["role"], "custom")
        self.assertEqual(member_tenant["custom_role_name"], "知识审阅员")
        self.assertEqual(
            member_tenant["permissions"],
            ["knowledge:view", "organizations:view", "settings:view"],
        )
        scoped_organizations = self.client.get(
            "/api/v1/organizations",
            headers={"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(scoped_organizations.status_code, 200, scoped_organizations.text)
        self.assertEqual(
            [item["id"] for item in scoped_organizations.json()["organizations"]],
            [department_id],
        )
        self.assertEqual(
            self.client.get(
                "/api/v1/members",
                headers={"X-Tenant-ID": self.tenant_id},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                "/api/v1/roles",
                headers={"X-Tenant-ID": self.tenant_id},
            ).status_code,
            403,
        )

        self.client.post("/api/v1/auth/logout", headers=self.csrf_headers())
        self.login()
        reassigned = self.client.patch(
            f"/api/v1/members/{self.member_id}/role",
            json={"role": "viewer"},
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        deleted = self.client.delete(
            f"/api/v1/roles/{role_id}",
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(reassigned.status_code, 200, reassigned.text)
        self.assertEqual(deleted.status_code, 204)

    def test_root_and_nonempty_organizations_cannot_be_deleted(self) -> None:
        self.login()
        root_delete = self.client.delete(
            f"/api/v1/organizations/{self.organization_id}",
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        created = self.client.post(
            "/api/v1/organizations",
            json={"name": "客服部"},
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(root_delete.status_code, 409)
        department_id = created.json()["id"]
        moved = self.client.patch(
            f"/api/v1/members/{self.member_id}/organization",
            json={"organization_id": department_id},
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(moved.status_code, 200, moved.text)
        occupied_delete = self.client.delete(
            f"/api/v1/organizations/{department_id}",
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        empty_department = self.client.post(
            "/api/v1/organizations",
            json={"name": "暂存部"},
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        empty_delete = self.client.delete(
            f"/api/v1/organizations/{empty_department.json()['id']}",
            headers=self.csrf_headers() | {"X-Tenant-ID": self.tenant_id},
        )
        self.assertEqual(occupied_delete.status_code, 409)
        self.assertEqual(empty_delete.status_code, 204)

    def test_database_rejects_cross_tenant_organization_references(self) -> None:
        with self.assertRaises(IntegrityError):
            with self.session_factory.begin() as db:
                user = User(
                    email="foreign@example.com",
                    display_name="Foreign",
                    password_hash=hash_password("correct horse battery"),
                )
                db.add(user)
                db.flush()
                db.add(
                    Membership(
                        tenant_id=UUID(self.tenant_id),
                        user_id=user.id,
                        organization_id=UUID(self.other_organization_id),
                        role="viewer",
                    )
                )
                db.flush()

    def csrf_headers(self) -> dict[str, str]:
        csrf_token = self.client.cookies.get("beework_csrf")
        self.assertIsNotNone(csrf_token)
        return {"X-CSRF-Token": csrf_token}


if __name__ == "__main__":
    unittest.main()
