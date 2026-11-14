"""T-014: catalog degradation validation, deletion guards, runtime resolution and gateway projection."""

import json
import unittest
from unittest import mock
from uuid import UUID, uuid4

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from beework import gateway_sync, model_catalog
from beework.config import settings
from beework.db import get_db
from beework.errors import ApiError
from beework.integrations.litellm import GatewayError, LiteLLMClient, model_alias
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
    User,
)
from beework.models.assistants import Assistant
from beework.models.jobs import Job
from beework.runtime.dsl import AssistantDSLv1
from beework.security import hash_password
from isolation import disable_external_integrations, stub_public_dns


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __enter__(self) -> "FakeGateway":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def delete_model(self, model_id: UUID) -> None:
        self.calls.append(("delete", model_id))

    def upsert_model(self, **kwargs: object) -> None:
        self.calls.append(("upsert", kwargs))

    def set_fallback(self, name: str, models: list[str]) -> None:
        self.calls.append(("set_fallback", name, models))

    def clear_fallback(self, name: str) -> None:
        self.calls.append(("clear_fallback", name))

    def ensure_team(self, *, team_id: str | None, alias: str, models: list[str]) -> str:
        self.calls.append(("ensure_team", {"team_id": team_id, "alias": alias, "models": models}))
        return team_id or "team-t014"

    def generate_key(self, *, team_id: str, alias: str) -> str:
        self.calls.append(("generate_key", team_id, alias))
        return "sk-tenant-test"


class ModelCatalogCompletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        disable_external_integrations()
        stub_public_dns()
        cls.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )

        @event.listens_for(cls.engine, "connect")
        def enable_foreign_keys(connection, _record) -> None:
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
            tenant = Tenant(name="Acme", slug="acme")
            platform_admin = User(
                email="platform@example.com", display_name="Platform Admin",
                password_hash=hash_password("platform admin safe password"), is_platform_admin=True,
            )
            admin = User(
                email="admin@example.com", display_name="Admin",
                password_hash=hash_password("correct horse battery"), is_platform_admin=False,
            )
            db.add_all([tenant, platform_admin, admin])
            db.flush()
            organization = Organization(tenant_id=tenant.id, name="Acme")
            db.add(organization)
            db.flush()
            db.add(Membership(tenant_id=tenant.id, user_id=admin.id, organization_id=organization.id, role="tenant_admin"))
            self.tenant_id = str(tenant.id)
        self.login_platform()

    def tearDown(self) -> None:
        settings.litellm_base_url = ""
        settings.litellm_master_key = ""

    # ------------------------------------------------------------------
    # Fixtures.
    # ------------------------------------------------------------------

    def login_platform(self) -> None:
        response = self.client.post(
            "/api/v1/auth/login",
            json={"email": "platform@example.com", "password": "platform admin safe password"},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def csrf(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.client.cookies.get("beework_csrf") or ""}

    def payload(self, name: str, **overrides: object) -> dict:
        values: dict = {
            "display_name": name,
            "provider": "openai",
            "model_type": "chat",
            "availability": "all_plans",
            "input_price_per_million": "0",
            "output_price_per_million": "0",
            "input_points_per_thousand": "1",
            "output_points_per_thousand": "2",
            "status": "healthy",
            "provider_model_id": name.lower().replace(" ", "-"),
            "endpoint_url": "https://models.example.test/v1",
            "api_key": "test-provider-secret",
            "region": "ap_singapore",
            "allowlist_tenant_ids": [],
        }
        values.update(overrides)
        return values

    def create_model(self, name: str, **overrides: object) -> dict:
        response = self.client.post(
            "/api/v1/platform/models", json=self.payload(name, **overrides), headers=self.csrf()
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def update_model(self, model: dict, **overrides: object):
        payload = {key: model[key] for key in (
            "display_name", "provider", "model_type", "availability",
            "input_price_per_million", "output_price_per_million",
            "input_points_per_thousand", "output_points_per_thousand",
            "provider_model_id", "endpoint_url", "api_version", "region",
            "context_window", "max_output_tokens", "default_temperature", "rpm_limit",
            "fallback_model_id", "allowlist_tenant_ids", "status",
        )}
        payload.update(overrides)
        return self.client.put(f"/api/v1/platform/models/{model['id']}", json=payload, headers=self.csrf())

    def enable_for_tenant(self, model_id: str) -> None:
        with self.session_factory.begin() as db:
            environment = TenantModelEnvironment(tenant_id=UUID(self.tenant_id), environment="production")
            db.add(environment)
            db.flush()
            db.add(TenantModelSelection(
                environment_id=environment.id, model_id=UUID(model_id), model_type="chat", is_default=True
            ))

    # ------------------------------------------------------------------
    # Validation (PLT-090 / 3.14.2).
    # ------------------------------------------------------------------

    def test_degradation_target_must_match_type_and_region_and_be_serving(self) -> None:
        primary = self.create_model("Primary")
        other_region = self.create_model("Other Region", region="us_east")
        other_type = self.create_model("Other Type", model_type="embedding")

        cross_region = self.update_model(primary, fallback_model_id=other_region["id"])
        self.assertEqual(cross_region.status_code, 422, cross_region.text)
        self.assertIn("区域", cross_region.json()["detail"])
        cross_type = self.update_model(primary, fallback_model_id=other_type["id"])
        self.assertEqual(cross_type.status_code, 422, cross_type.text)

        disabled = self.create_model("Disabled Target", status="disabled")
        to_disabled = self.update_model(primary, fallback_model_id=disabled["id"])
        self.assertEqual(to_disabled.status_code, 422, to_disabled.text)
        self.assertIn("停用", to_disabled.json()["detail"])

        self.assertEqual(self.update_model(primary, fallback_model_id=primary["id"]).status_code, 422)
        self.assertEqual(self.update_model(primary, fallback_model_id=str(uuid4())).status_code, 422)

        fallback = self.create_model("Fallback")
        saved = self.update_model(primary, fallback_model_id=fallback["id"])
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["fallback_model_id"], fallback["id"])

    def test_cyclic_degradation_is_rejected(self) -> None:
        first = self.create_model("Cycle A")
        second = self.create_model("Cycle B")
        self.assertEqual(self.update_model(first, fallback_model_id=second["id"]).status_code, 200)
        cycle = self.update_model(second, fallback_model_id=first["id"])
        self.assertEqual(cycle.status_code, 422, cycle.text)
        self.assertIn("循环", cycle.json()["detail"])
        with self.session_factory() as db:
            self.assertIsNone(db.get(PlatformModel, UUID(second["id"])).fallback_model_id)

    def test_legacy_cycle_in_existing_data_is_reported_as_conflict(self) -> None:
        columns = {
            k: v for k, v in self.payload("Legacy A").items()
            if k not in ("api_key", "allowlist_tenant_ids")
        }
        with self.session_factory.begin() as db:
            a = PlatformModel(**columns, encrypted_api_key="encrypted", api_key_last_four="cret")
            b = PlatformModel(**{**columns, "display_name": "Legacy B", "provider_model_id": "legacy-b"},
                              encrypted_api_key="encrypted", api_key_last_four="cret")
            db.add_all([a, b])
            db.flush()
            a.fallback_model_id, b.fallback_model_id = b.id, a.id
            ids = (a.id, b.id)
        primary = self.create_model("New Primary")
        with self.session_factory() as db:
            with self.assertRaises(ApiError) as raised:
                model_catalog.validate_fallback(
                    db, model_id=UUID(primary["id"]), fallback_model_id=ids[0],
                    model_type="chat", region="ap_singapore",
                )
            self.assertEqual(getattr(raised.exception, "status_code", None), 409)

    # ------------------------------------------------------------------
    # Deletion (PLT-093).
    # ------------------------------------------------------------------

    def test_delete_rejects_enabled_models_and_fallback_targets(self) -> None:
        primary = self.create_model("In Use")
        fallback = self.create_model("Degradation Target")
        self.enable_for_tenant(primary["id"])

        blocked = self.client.delete(f"/api/v1/platform/models/{primary['id']}", headers=self.csrf())
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(blocked.json()["code"], "model_catalog.in_use")
        self.assertIn("停用", blocked.json()["detail"])

        referenced = self.create_model("Referring", fallback_model_id=fallback["id"])
        self.assertIn("id", referenced)
        blocked_reference = self.client.delete(f"/api/v1/platform/models/{fallback['id']}", headers=self.csrf())
        self.assertEqual(blocked_reference.status_code, 409, blocked_reference.text)
        self.assertIn("降级", blocked_reference.json()["detail"])

    def test_delete_succeeds_audits_and_queues_gateway_cleanup(self) -> None:
        model = self.create_model("Disposable")
        unused = str(uuid4())
        self.assertEqual(self.client.delete(f"/api/v1/platform/models/{unused}", headers=self.csrf()).status_code, 404)
        settings.litellm_base_url = "http://gateway"
        settings.litellm_master_key = "sk-test"

        deleted = self.client.delete(f"/api/v1/platform/models/{model['id']}", headers=self.csrf())
        self.assertEqual(deleted.status_code, 204, deleted.text)
        with self.session_factory() as db:
            self.assertIsNone(db.get(PlatformModel, UUID(model["id"])))
            actions = list(db.scalars(select(AuditEvent.action)))
            self.assertIn("model_catalog.deleted", actions)
            jobs = list(db.scalars(select(Job.kind)))
            self.assertIn("gateway.delete_model", jobs)
        remaining = self.client.get("/api/v1/platform/models").json()["models"]
        self.assertNotIn(model["id"], [item["id"] for item in remaining])

    def test_delete_requires_platform_admin_and_csrf(self) -> None:
        model = self.create_model("Protected")
        url = f"/api/v1/platform/models/{model['id']}"
        self.assertEqual(self.client.delete(url).status_code, 403)  # no CSRF
        self.client.post("/api/v1/auth/logout", headers=self.csrf())
        response = self.client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": "correct horse battery"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        headers = {**self.csrf(), "X-Tenant-ID": self.tenant_id}
        denied = self.client.delete(url, headers=headers)
        self.assertEqual(denied.status_code, 403, denied.text)
        # A tenant account cannot even probe whether a platform-only model id exists.
        self.assertEqual(self.client.delete(
            f"/api/v1/platform/models/{uuid4()}", headers=headers
        ).status_code, 403)

    # ------------------------------------------------------------------
    # Runtime + configuration impact (PLT-090 / PLT-092).
    # ------------------------------------------------------------------

    def test_runtime_resolution_switches_disabled_model_and_carries_one_fallback(self) -> None:
        primary = self.create_model("Runtime Primary")
        fallback = self.create_model("Runtime Fallback")
        saved = self.update_model(primary, fallback_model_id=fallback["id"])
        self.assertEqual(saved.status_code, 200, saved.text)
        primary = saved.json()
        with self.session_factory() as db:
            healthy = model_catalog.resolve_runtime_model(db, model_alias(UUID(primary["id"])))
            self.assertEqual(healthy.alias, model_alias(UUID(primary["id"])))
            self.assertEqual(healthy.fallbacks, [model_alias(UUID(fallback["id"]))])
            self.assertIsNone(healthy.switched_from)
            self.assertEqual(model_catalog.resolve_runtime_model(db, "some/external-model").alias, "some/external-model")

        self.assertEqual(self.update_model(primary, status="disabled").status_code, 200)
        with self.session_factory() as db:
            switched = model_catalog.resolve_runtime_model(db, model_alias(UUID(primary["id"])))
            self.assertEqual(switched.alias, model_alias(UUID(fallback["id"])))
            self.assertEqual(switched.switched_from, "Runtime Primary")
            self.assertEqual(switched.effective_name, "Runtime Fallback")
            state = model_catalog.model_state(db, primary["id"])
            self.assertTrue(state.unavailable)
            self.assertEqual(state.fallback_name, "Runtime Fallback")

        with self.session_factory.begin() as db:
            db.get(PlatformModel, UUID(primary["id"])).fallback_model_id = None
        with self.session_factory() as db:
            without = model_catalog.resolve_runtime_model(db, model_alias(UUID(primary["id"])))
            self.assertEqual(without.alias, model_alias(UUID(primary["id"])))
            self.assertEqual(without.fallbacks, [])
            self.assertTrue(model_catalog.model_state(db, primary["id"]).unavailable)
            self.assertIsNone(model_catalog.model_state(db, primary["id"]).fallback_name)

    def test_configuration_check_warns_or_blocks_for_disabled_models(self) -> None:
        from beework.assistant_checks import (
            check_assistant,  # lazy: schemas <-> api circular import
        )

        primary = self.create_model("Check Primary")
        fallback = self.create_model("Check Fallback")
        self.update_model(primary, fallback_model_id=fallback["id"], status="disabled")
        assistant = Assistant(
            id=uuid4(), tenant_id=UUID(self.tenant_id), name="Check Agent", description="d",
            draft_config={"model_id": primary["id"], "prompt": "answer"},
        )
        with self.session_factory() as db:
            issues = check_assistant(db, assistant, enabled_model_ids={primary["id"]}, environment="production")
        warned = [item for item in issues if item.code == "runtime.model_disabled_fallback"]
        self.assertEqual(len(warned), 1)
        self.assertEqual(warned[0].severity, "high")
        self.assertIn("Check Fallback", warned[0].message)

        with self.session_factory.begin() as db:
            db.get(PlatformModel, UUID(primary["id"])).fallback_model_id = None
        with self.session_factory() as db:
            issues = check_assistant(db, assistant, enabled_model_ids={primary["id"]}, environment="production")
        blocked = [item for item in issues if item.code == "runtime.model_disabled"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0].severity, "blocker")

    def test_model_options_expose_disabled_state_and_degradation_name(self) -> None:
        primary = self.create_model("Options Primary")
        fallback = self.create_model("Options Fallback")
        self.update_model(primary, fallback_model_id=fallback["id"], status="disabled")
        # Model options are tenant-scoped; enable the model directly in the tenant environment.
        self.enable_for_tenant(primary["id"])
        self.client.post("/api/v1/auth/logout", headers=self.csrf())
        self.client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "correct horse battery"})
        response = self.client.get("/api/v1/assistants/model-options", headers={"X-Tenant-ID": self.tenant_id})
        self.assertEqual(response.status_code, 200, response.text)
        option = next(item for item in response.json()["models"] if item["id"] == primary["id"])
        self.assertEqual(option["status"], "disabled")
        self.assertEqual(option["fallback_display_name"], "Options Fallback")

    # ------------------------------------------------------------------
    # Gateway projection (contract tests, no real gateway needed).
    # ------------------------------------------------------------------

    def test_litellm_client_manages_fallbacks_over_the_api(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "DELETE":
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(200, json={})

        with LiteLLMClient("http://gateway", "sk-master", transport=httpx.MockTransport(handler)) as client:
            client.set_fallback("bw-primary", ["bw-fallback"])
            client.clear_fallback("bw-primary")  # a rule that was never created is not an error
        self.assertEqual(requests[0].url.path, "/fallback")
        self.assertEqual(json.loads(requests[0].content), {
            "model": "bw-primary", "fallback_models": ["bw-fallback"], "fallback_type": "general"
        })
        self.assertEqual(requests[1].method, "DELETE")
        self.assertEqual(requests[1].url.path, "/fallback/bw-primary")
        self.assertEqual(requests[1].url.params.get("fallback_type"), "general")

    def test_gateway_sync_projects_the_catalog_fallback_and_cleans_it_up(self) -> None:
        primary = self.create_model("Sync Primary")
        fallback = self.create_model("Sync Fallback")
        self.update_model(primary, fallback_model_id=fallback["id"])
        fake = FakeGateway()
        with mock.patch.object(gateway_sync, "gateway_configured", return_value=True), \
             mock.patch.object(gateway_sync, "LiteLLMClient", return_value=fake), \
             mock.patch.object(gateway_sync, "decrypt_model_api_key", return_value="sk-test"):
            with self.session_factory() as db:
                model = db.get(PlatformModel, UUID(primary["id"]))
                gateway_sync.sync_platform_model(db, model)
                self.assertIn(
                    ("set_fallback", model_alias(model.id), [model_alias(UUID(fallback["id"]))]), fake.calls
                )
                model.status = "disabled"
                db.commit()
                gateway_sync.sync_platform_model(db, model)
                self.assertIn(("clear_fallback", model_alias(model.id)), fake.calls)
                self.assertIn(("delete", model.id), fake.calls)
            fake.calls.clear()
            gateway_sync.delete_model_job(
                self.session_factory(), {"model_id": primary["id"], "alias": model_alias(UUID(primary["id"]))}
            )
        self.assertEqual(fake.calls, [
            ("delete", UUID(primary["id"])),
            ("clear_fallback", model_alias(UUID(primary["id"]))),
        ])


    # ------------------------------------------------------------------
    # Review fixes.
    # ------------------------------------------------------------------

    def test_long_chain_is_not_treated_as_a_cycle(self) -> None:
        """A chain longer than the old depth cap is legitimate; only a repeated id is a cycle."""
        chain = [self.create_model(f"Long {index}") for index in range(9)]
        for current, nxt in zip(chain, chain[1:], strict=False):
            self.assertEqual(self.update_model(current, fallback_model_id=nxt["id"]).status_code, 200)
        extended = self.create_model("Long Head", fallback_model_id=chain[0]["id"])
        self.assertIn("id", extended)  # used to fail: the 9-hop chain tripped the depth cap

    def test_disabled_degradation_target_does_not_block_unrelated_edits(self) -> None:
        primary = self.create_model("Edit Primary")
        target = self.create_model("Edit Target")
        saved = self.update_model(primary, fallback_model_id=target["id"])
        self.assertEqual(saved.status_code, 200, saved.text)
        # Disable the degradation target, then edit an unrelated field of the referrer: the stale
        # (now disabled) target must not turn the edit into a 422 (review fix 2).
        self.assertEqual(self.update_model(target, status="disabled").status_code, 200)
        edited = self.update_model(primary, rpm_limit=99)
        self.assertEqual(edited.status_code, 200, edited.text)

    def test_region_change_is_rejected_while_referenced_as_fallback(self) -> None:
        primary = self.create_model("Ref Primary")
        target = self.create_model("Ref Target")
        saved = self.update_model(primary, fallback_model_id=target["id"])
        self.assertEqual(saved.status_code, 200, saved.text)
        moved = self.update_model(target, region="us_east")
        self.assertEqual(moved.status_code, 422, moved.text)
        self.assertIn("Ref Primary", moved.json()["detail"])
        self.assertEqual(self.update_model(saved.json(), fallback_model_id=None).status_code, 200)
        self.assertEqual(self.update_model(target, region="us_east").status_code, 200)

    def test_disabling_a_chain_target_resyncs_the_whole_chain(self) -> None:
        settings.litellm_base_url = "http://gateway"
        settings.litellm_master_key = "sk-test"
        a = self.create_model("Chain A")
        b = self.create_model("Chain B")
        c = self.create_model("Chain C")
        self.assertEqual(self.update_model(a, fallback_model_id=b["id"]).status_code, 200)
        self.assertEqual(self.update_model(b, fallback_model_id=c["id"]).status_code, 200)
        self.assertEqual(self.update_model(c, status="disabled").status_code, 200)
        with self.session_factory() as db:
            synced = {
                job.payload.get("model_id")
                for job in db.scalars(select(Job).where(Job.kind == "gateway.sync_model"))
            }
        self.assertIn(a["id"], synced)  # transitive: A→B→C, so A's projection depends on C too
        self.assertIn(b["id"], synced)

    def test_deleted_model_resolution_records_no_fallback(self) -> None:
        model = self.create_model("Vanishing")
        alias = model_alias(UUID(model["id"]))
        deleted = self.client.delete(f"/api/v1/platform/models/{model['id']}", headers=self.csrf())
        self.assertEqual(deleted.status_code, 204, deleted.text)
        with self.session_factory() as db:
            resolved = model_catalog.resolve_runtime_model(db, alias)
            self.assertEqual(resolved.alias, alias)
            self.assertIsNone(resolved.switched_from)
            self.assertEqual(resolved.fallbacks, [])

    def test_configuration_check_reports_one_issue_for_a_deleted_model(self) -> None:
        from beework.assistant_checks import (
            check_assistant,  # lazy: schemas <-> api circular import
        )

        model = self.create_model("Doomed")
        deleted = self.client.delete(f"/api/v1/platform/models/{model['id']}", headers=self.csrf())
        self.assertEqual(deleted.status_code, 204, deleted.text)
        assistant = Assistant(
            id=uuid4(), tenant_id=UUID(self.tenant_id), name="Deleted Model Agent", description="d",
            draft_config={"model_id": model["id"], "prompt": "answer"},
        )
        with self.session_factory() as db:
            issues = check_assistant(db, assistant, enabled_model_ids={model["id"]}, environment="production")
        faults = [item for item in issues if item.code.startswith("runtime.model")]
        self.assertEqual(len(faults), 1, [item.code for item in faults])
        self.assertEqual(faults[0].code, "runtime.model_disabled")
        self.assertEqual(faults[0].severity, "blocker")

    def test_configuration_check_flags_disabled_rewrite_and_intent_models(self) -> None:
        from beework.assistant_checks import (
            check_assistant,  # lazy: schemas <-> api circular import
        )

        rewrite_target = self.create_model("Rewrite Fallback")
        rewrite = self.create_model("Rewrite Model", fallback_model_id=rewrite_target["id"], status="disabled")
        intent = self.create_model("Intent Model")
        deleted = self.client.delete(f"/api/v1/platform/models/{intent['id']}", headers=self.csrf())
        self.assertEqual(deleted.status_code, 204, deleted.text)
        main = self.create_model("OC Main")
        assistant = Assistant(
            id=uuid4(), tenant_id=UUID(self.tenant_id), name="OC Model Agent", description="d",
            draft_config={
                "model_id": main["id"], "prompt": "answer",
                "oc": {
                    "rewrite": {"enabled": True, "model_id": rewrite["id"]},
                    "intent": {"enabled": True, "model_id": intent["id"]},
                },
            },
        )
        with self.session_factory() as db:
            issues = check_assistant(db, assistant, enabled_model_ids={main["id"]}, environment="production")
        rewrite_issue = next(item for item in issues if item.code == "runtime.rewrite_model_disabled")
        self.assertEqual(rewrite_issue.severity, "high")
        self.assertIn("Rewrite Fallback", rewrite_issue.message)
        intent_issue = next(item for item in issues if item.code == "runtime.intent_model_disabled")
        self.assertEqual(intent_issue.severity, "blocker")
        # Off by default: a switched-off section must not produce issues.
        quiet = Assistant(
            id=uuid4(), tenant_id=UUID(self.tenant_id), name="OC Quiet Agent", description="d",
            draft_config={
                "model_id": main["id"], "prompt": "answer",
                "oc": {"rewrite": {"enabled": False, "model_id": rewrite["id"]}},
            },
        )
        with self.session_factory() as db:
            issues = check_assistant(db, quiet, enabled_model_ids={main["id"]}, environment="production")
        self.assertFalse([item for item in issues if item.code.startswith("runtime.rewrite")])

    def test_tenant_team_allowlist_includes_resolved_degradation_alias(self) -> None:
        primary = self.create_model("Team Primary")
        fallback = self.create_model("Team Fallback")
        saved = self.update_model(primary, fallback_model_id=fallback["id"])
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(self.update_model(saved.json(), status="disabled").status_code, 200)
        self.enable_for_tenant(primary["id"])
        fake = FakeGateway()
        with mock.patch.object(gateway_sync, "gateway_configured", return_value=True), \
             mock.patch.object(gateway_sync, "LiteLLMClient", return_value=fake), \
             mock.patch.object(gateway_sync, "encrypt_model_api_key", return_value="encrypted"):
            with self.session_factory() as db:
                tenant = db.get(Tenant, UUID(self.tenant_id))
                config = db.scalar(select(TenantModelEnvironment).where(
                    TenantModelEnvironment.tenant_id == UUID(self.tenant_id)
                ))
                gateway_sync.sync_tenant_environment(db, tenant, config)
        ensure = next(call for call in fake.calls if call[0] == "ensure_team")[1]
        self.assertIn(model_alias(UUID(fallback["id"])), ensure["models"])
        self.assertNotIn(model_alias(UUID(primary["id"])), ensure["models"])

    def test_changing_region_or_type_revalidates_an_unchanged_fallback(self) -> None:
        primary = self.create_model("Shape Primary")
        target = self.create_model("Shape Target")
        saved = self.update_model(primary, fallback_model_id=target["id"])
        self.assertEqual(saved.status_code, 200, saved.text)
        moved = self.update_model(saved.json(), region="us_east")  # same fallback, other region
        self.assertEqual(moved.status_code, 422, moved.text)

    def test_a_server_error_whose_alias_contains_404_is_not_swallowed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "boom"})

        with LiteLLMClient("http://gateway", "sk-master", transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(GatewayError):
                client.clear_fallback("bw-12340404-0000-0000-0000-000000000000")
            with self.assertRaises(GatewayError):
                client.delete_model(UUID("12340404-0000-0000-0000-000000000000"))

    def test_deleting_a_deployment_that_is_already_gone_is_fine_in_either_gateway_dialect(self) -> None:
        model_id = UUID("12340404-0000-0000-0000-000000000000")
        for response in (httpx.Response(404, json={"detail": "gone"}), httpx.Response(400, json={"error": f"Model with id={model_id} not found in db"})):
            with LiteLLMClient("http://gateway", "sk-master", transport=httpx.MockTransport(lambda _request, r=response: r)) as client:
                client.delete_model(model_id)  # does not raise
        with LiteLLMClient("http://gateway", "sk-master", transport=httpx.MockTransport(lambda _request: httpx.Response(400, json={"error": "bad request"}))) as client:
            with self.assertRaises(GatewayError):
                client.delete_model(model_id)  # any other 400 is still a failure

    def test_a_degradation_target_outside_the_tenants_entitlement_stays_out_of_its_scope(self) -> None:
        primary = self.create_model("Entitled Primary")
        restricted = self.create_model("Restricted Target", availability="enterprise")
        saved = self.update_model(primary, fallback_model_id=restricted["id"])
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(self.update_model(saved.json(), status="disabled").status_code, 200)
        self.enable_for_tenant(primary["id"])
        fake = FakeGateway()
        with mock.patch.object(gateway_sync, "gateway_configured", return_value=True), \
             mock.patch.object(gateway_sync, "LiteLLMClient", return_value=fake), \
             mock.patch.object(gateway_sync, "encrypt_model_api_key", return_value="encrypted"):
            with self.session_factory() as db:
                tenant = db.get(Tenant, UUID(self.tenant_id))
                config = db.scalar(select(TenantModelEnvironment).where(TenantModelEnvironment.tenant_id == UUID(self.tenant_id)))
                gateway_sync.sync_tenant_environment(db, tenant, config)
        ensure = next(call for call in fake.calls if call[0] == "ensure_team")[1]
        self.assertNotIn(model_alias(UUID(restricted["id"])), ensure["models"])
        with self.session_factory() as db:
            state = model_catalog.model_states(db, [primary["id"]])[UUID(primary["id"])]
            target = db.get(PlatformModel, state.fallback_id)
            self.assertFalse(model_catalog.tenant_may_use(db, UUID(self.tenant_id), target))

    def test_a_disabled_model_without_a_serving_target_records_no_switch(self) -> None:
        lonely = self.create_model("Lonely")
        self.assertEqual(self.update_model(lonely, status="disabled").status_code, 200)
        with self.session_factory() as db:
            resolved = model_catalog.resolve_runtime_model(db, model_alias(UUID(lonely["id"])))
        self.assertIsNone(resolved.switched_from)

    def test_rewrite_model_resolves_through_the_catalog(self) -> None:
        from beework.models.runtime import Run
        from beework.runtime import executor

        rewrite_target = self.create_model("Runtime Rewrite Fallback")
        rewrite = self.create_model("Runtime Rewrite Model", fallback_model_id=rewrite_target["id"])
        saved = self.update_model(rewrite, status="disabled")
        self.assertEqual(saved.status_code, 200, saved.text)
        self.enable_for_tenant(rewrite_target["id"])  # the environment the rewrite model is built from
        dsl = AssistantDSLv1(
            prompt="answer", model_alias=model_alias(UUID(rewrite_target["id"])),
            oc={"rewrite": {"enabled": True, "model_id": rewrite["id"]}},
        )
        run = Run(tenant_id=UUID(self.tenant_id), assistant_id=uuid4(), environment="production", source="api")
        with mock.patch.object(executor, "tenant_gateway_key", return_value="sk-tenant"):
            with self.session_factory() as db:
                llm = executor.gateway_rewrite_llm(db, run, dsl)
        self.assertEqual(llm.model_name, model_alias(UUID(rewrite_target["id"])))
        self.assertEqual(llm.extra_body["metadata"]["model_fallback_from"], "Runtime Rewrite Model")

    def test_short_models_carry_request_level_fallbacks(self) -> None:
        from beework.runtime.llm import build_intent_model, build_rewrite_model

        rewrite = build_rewrite_model("bw-a", api_key="sk", metadata={}, fallbacks=["bw-b"])
        self.assertEqual(rewrite.extra_body["fallbacks"], ["bw-b"])
        intent = build_intent_model("bw-a", api_key="sk", metadata={})
        self.assertNotIn("fallbacks", intent.extra_body)


if __name__ == "__main__":
    unittest.main()
