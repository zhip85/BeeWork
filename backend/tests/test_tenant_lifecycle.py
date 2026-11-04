import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from beework import gateway_sync
from beework.db import get_db
from beework.integrations.litellm import GatewayError
from beework.main import app
from beework.model_secrets import decrypt_model_api_key, encrypt_model_api_key
from beework.models import (
    AuditEvent,
    AuthSession,
    Base,
    Membership,
    Organization,
    Tenant,
    TenantModelEnvironment,
    TenantModelSelection,
    User,
)
from beework.models.jobs import Job
from beework.models.notifications import InAppNotification
from beework.security import hash_password
from beework.tenant_lifecycle import DATA_NOT_PURGED_NOTE, apply_transition, run_auto_lifecycle
from beework.worker.runner import run_once
from isolation import disable_external_integrations, stub_public_dns
from test_runtime import make_model

PASSWORD = "correct horse battery"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


class FakeLiteLLM:
    """Stands in for the gateway: records what BeeWork asks of it."""

    calls: list[tuple] = []
    fail_access = False
    models_seen: list = []

    def __init__(self, *_a, **_k) -> None: ...

    def __enter__(self) -> "FakeLiteLLM":
        return self

    def __exit__(self, *_exc) -> None: ...

    def ensure_team(self, *, team_id, alias, models):
        self.calls.append(("ensure_team", alias, tuple(models)))
        return team_id or "team-1"

    def generate_key(self, *, team_id, alias):
        self.calls.append(("generate_key", team_id))
        return "sk-virtual-key"

    def block_key(self, key):
        if self.fail_access:
            raise GatewayError("gateway down")
        self.calls.append(("block", key))

    def unblock_key(self, key):
        if self.fail_access:
            raise GatewayError("gateway down")
        self.calls.append(("unblock", key))

    def upsert_model(self, **kw):
        self.calls.append(("upsert_model", str(kw["model_id"])))

    def delete_model(self, model_id):
        self.calls.append(("delete_model", str(model_id)))


class TenantLifecycleTests(unittest.TestCase):
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
        FakeLiteLLM.calls = []
        FakeLiteLLM.fail_access = False
        self.client = TestClient(app)
        self.count = 0
        with self.session_factory.begin() as db:
            platform = User(email="platform@example.com", display_name="Platform", password_hash=hash_password(PASSWORD), is_platform_admin=True)
            db.add(platform)
        self.chat_id: UUID | None = None

    # -- helpers ------------------------------------------------------------------------------

    def make_tenant(self, status: str = "active", **fields) -> UUID:
        self.count += 1
        with self.session_factory.begin() as db:
            tenant = Tenant(name=f"Tenant {self.count}", slug=f"tenant-{self.count}", plan="enterprise", seat_limit=10, status=status, **fields)
            db.add(tenant)
            db.flush()
            org = Organization(tenant_id=tenant.id, name=tenant.name)
            admin = User(email=f"admin{self.count}@example.com", display_name=f"Admin {self.count}", password_hash=hash_password(PASSWORD))
            builder = User(email=f"builder{self.count}@example.com", display_name=f"Builder {self.count}", password_hash=hash_password(PASSWORD))
            db.add_all([org, admin, builder])
            db.flush()
            db.add_all([
                Membership(tenant_id=tenant.id, user_id=admin.id, organization_id=org.id, role="tenant_admin"),
                Membership(tenant_id=tenant.id, user_id=builder.id, organization_id=org.id, role="builder"),
            ])
            return tenant.id

    def login(self, email: str) -> dict[str, str]:
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).status_code, 200)
        return {"X-CSRF-Token": self.client.cookies.get("beework_csrf")}

    def tenant(self, tenant_id: UUID) -> Tenant:
        with self.session_factory() as db:
            return db.get(Tenant, tenant_id)

    def auto(self, now: datetime = NOW) -> dict[str, int]:
        with self.session_factory.begin() as db:
            return run_auto_lifecycle(db, now=now)

    def audits(self, tenant_id: UUID | None, action: str = "tenant.status_changed") -> list[AuditEvent]:
        with self.session_factory() as db:
            return list(db.scalars(select(AuditEvent).where(AuditEvent.action == action, AuditEvent.tenant_id == tenant_id)))

    def worker(self) -> int:
        return run_once("test-worker", self.session_factory, schedule=False)

    # -- automatic transitions ------------------------------------------------------------------------

    def test_an_expired_trial_is_suspended_and_audited_by_the_system(self) -> None:
        expired = self.make_tenant("trial", trial_ends_at=NOW - timedelta(minutes=1))
        running = self.make_tenant("trial", trial_ends_at=NOW + timedelta(days=3))
        open_ended = self.make_tenant("trial", trial_ends_at=None)
        self.assertEqual(self.auto()["trial_expired"], 1)
        self.assertEqual((self.tenant(expired).status, self.tenant(running).status, self.tenant(open_ended).status), ("suspended", "trial", "trial"))
        tenant_chain, platform_chain = self.audits(expired), [a for a in self.audits(None) if a.details.get("target_tenant_id") == str(expired)]
        for entry in (tenant_chain[0], platform_chain[0]):
            self.assertIsNone(entry.actor_user_id)
            self.assertEqual((entry.details["operator"], entry.details["from"], entry.details["to"], entry.details["reason"]), ("system", "trial", "suspended", "试用期已到期"))
        self.assertEqual((len(tenant_chain), len(platform_chain)), (1, 1))
        with self.session_factory() as db:
            note = db.scalar(select(InAppNotification).where(InAppNotification.tenant_id == expired, InAppNotification.type == "tenant.status_changed"))
            self.assertEqual(note.params, {"status": "suspended"})

    def test_grace_longer_than_seven_days_turns_read_only(self) -> None:
        over = self.make_tenant("grace", status_changed_at=NOW - timedelta(days=7, seconds=1))
        exactly = self.make_tenant("grace", status_changed_at=NOW - timedelta(days=7))
        recent = self.make_tenant("grace", status_changed_at=NOW - timedelta(days=2))
        self.assertEqual(self.auto()["grace_to_read_only"], 1)
        self.assertEqual([self.tenant(t).status for t in (over, exactly, recent)], ["read_only", "grace", "grace"])
        self.assertEqual(self.audits(over)[0].details["reason"], "欠费宽限期超过 7 天")

    def test_pending_deletion_after_thirty_days_is_deleted_without_claiming_data_was_purged(self) -> None:
        old = self.make_tenant("pending_deletion", status_changed_at=NOW - timedelta(days=31))
        fresh = self.make_tenant("pending_deletion", status_changed_at=NOW - timedelta(days=29))
        with self.session_factory.begin() as db:
            member = db.scalar(select(User).where(User.email == f"admin{self.count - 1}@example.com"))
            db.add(AuthSession(user_id=member.id, token_id="tok", expires_at=NOW + timedelta(days=1)))
        self.assertEqual(self.auto()["pending_deletion_deleted"], 1)
        self.assertEqual((self.tenant(old).status, self.tenant(fresh).status), ("deleted", "pending_deletion"))
        entry = self.audits(old)[0]
        self.assertEqual((entry.details["data_purged"], entry.details["note"]), ("false", DATA_NOT_PURGED_NOTE))
        with self.session_factory() as db:
            self.assertIsNotNone(db.scalar(select(AuthSession.revoked_at)))
        # The platform console shows the caveat next to the status.
        self.login("platform@example.com")
        tenants = {t["id"]: t for t in self.client.get("/api/v1/platform/tenants").json()}
        self.assertEqual(tenants[str(old)]["status_note"], DATA_NOT_PURGED_NOTE)
        self.assertIsNone(tenants[str(fresh)]["status_note"])

    def test_running_twice_does_not_repeat_transitions_or_audits(self) -> None:
        tenant = self.make_tenant("trial", trial_ends_at=NOW - timedelta(days=1))
        self.assertEqual(sum(self.auto().values()), 1)
        self.assertEqual(sum(self.auto().values()), 0)
        self.assertEqual(sum(self.auto(NOW + timedelta(days=40)).values()), 0)  # suspended tenants are not touched
        self.assertEqual(len(self.audits(tenant)), 1)
        with self.session_factory() as db:
            self.assertEqual(len(list(db.scalars(select(InAppNotification)))), 1)

    def test_chained_progress_needs_the_next_run(self) -> None:
        tenant = self.make_tenant("grace", status_changed_at=NOW - timedelta(days=9))
        self.auto()
        self.assertEqual(self.tenant(tenant).status, "read_only")
        self.assertEqual(self.tenant(tenant).status_changed_at.replace(tzinfo=UTC), NOW)  # the clock restarts at each change

    def test_the_hourly_job_runs_through_the_worker(self) -> None:
        tenant = self.make_tenant("trial", trial_ends_at=datetime.now(UTC) - timedelta(hours=1))
        with self.session_factory.begin() as db:
            from beework.worker.queue import enqueue

            enqueue(db, "tenants.auto_lifecycle", {})
        self.worker()
        self.assertEqual(self.tenant(tenant).status, "suspended")

    # -- trial at creation ------------------------------------------------------------------------------

    def create_payload(self, **extra) -> dict:
        return {
            "name": "Fresh Co", "slug": "fresh-co", "seat_limit": 5, "plan": "standard", "region": "ap_singapore",
            "admin_email": "owner@fresh.example", "admin_name": "Owner", "admin_password": "a-strong-password-1", **extra,
        }

    def test_a_tenant_can_be_created_as_a_trial_with_an_end_date(self) -> None:
        headers = self.login("platform@example.com")
        response = self.client.post("/api/v1/platform/tenants", json=self.create_payload(provisioning="trial", trial_days=21), headers=headers)
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["status"], "trial")
        ends = datetime.fromisoformat(body["trial_ends_at"].replace("Z", "+00:00"))
        ends = ends if ends.tzinfo else ends.replace(tzinfo=UTC)
        self.assertAlmostEqual((ends - datetime.now(UTC)).total_seconds(), 21 * 86400, delta=60)
        created = self.audits(None, "tenant.created")[0].details
        self.assertEqual((created["provisioning"], created["trial_days"]), ("trial", "21"))

    def test_trial_days_default_to_fourteen_and_formal_is_the_default(self) -> None:
        headers = self.login("platform@example.com")
        trial = self.client.post("/api/v1/platform/tenants", json=self.create_payload(provisioning="trial"), headers=headers).json()
        ends = datetime.fromisoformat(trial["trial_ends_at"].replace("Z", "+00:00"))
        ends = ends if ends.tzinfo else ends.replace(tzinfo=UTC)
        self.assertAlmostEqual((ends - datetime.now(UTC)).total_seconds(), 14 * 86400, delta=60)
        formal = self.client.post("/api/v1/platform/tenants", json=self.create_payload(slug="formal-co", admin_email="o2@fresh.example"), headers=headers).json()
        self.assertEqual((formal["status"], formal["trial_ends_at"]), ("active", None))
        for bad in (0, 91):
            response = self.client.post("/api/v1/platform/tenants", json=self.create_payload(slug="bad-co", admin_email="o3@fresh.example", provisioning="trial", trial_days=bad), headers=headers)
            self.assertEqual(response.status_code, 422, bad)

    # -- gateway sync is queued -------------------------------------------------------------------------------

    def gateway(self):
        stack = mock.patch.object(gateway_sync, "LiteLLMClient", FakeLiteLLM), mock.patch.object(gateway_sync, "gateway_configured", return_value=True)
        for patcher in stack:
            patcher.start()
            self.addCleanup(patcher.stop)

    def enable_model(self, tenant_id: UUID) -> TenantModelEnvironment:
        with self.session_factory.begin() as db:
            model = make_model(f"Chat {tenant_id.hex[:6]}")
            db.add(model)
            db.flush()
            config = TenantModelEnvironment(tenant_id=tenant_id, environment="production", gateway_sync_status="not_configured")
            db.add(config)
            db.flush()
            db.add(TenantModelSelection(environment_id=config.id, model_id=model.id, model_type="chat", is_default=True))
            self.chat_id = model.id
            return config

    def env(self, tenant_id: UUID) -> TenantModelEnvironment:
        with self.session_factory() as db:
            return db.scalar(select(TenantModelEnvironment).where(TenantModelEnvironment.tenant_id == tenant_id))

    def test_saving_a_model_environment_returns_pending_and_the_worker_finishes_the_sync(self) -> None:
        self.gateway()
        tenant_id = self.make_tenant()
        self.enable_model(tenant_id)
        headers = self.login("admin1@example.com")
        base = {"X-Tenant-ID": str(tenant_id), **headers}
        body = {"enabled_model_ids": [str(self.chat_id)], "default_model_ids": {"chat": str(self.chat_id), "embedding": None, "rerank": None}, "expected_version": self.env(tenant_id).version}
        saved = self.client.put("/api/v1/tenant/model-environments/production", json=body, headers=base)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["gateway_sync_status"], "pending")
        self.assertEqual(FakeLiteLLM.calls, [])  # nothing talked to the gateway during the request
        with self.session_factory() as db:
            self.assertEqual([j.kind for j in db.scalars(select(Job))], ["gateway.sync_environment"])
        self.worker()
        done = self.client.get("/api/v1/tenant/model-environments/production", headers=base).json()
        self.assertEqual(done["gateway_sync_status"], "synced")
        self.assertEqual([c[0] for c in FakeLiteLLM.calls], ["ensure_team", "generate_key"])

    def test_retry_sync_queues_again_and_a_gateway_failure_is_shown_on_the_row(self) -> None:
        self.gateway()
        tenant_id = self.make_tenant()
        self.enable_model(tenant_id)
        base = {"X-Tenant-ID": str(tenant_id), **self.login("admin1@example.com")}
        with mock.patch.object(FakeLiteLLM, "ensure_team", side_effect=GatewayError("gateway down")):
            retried = self.client.post("/api/v1/tenant/model-environments/production/sync", headers=base)
            self.assertEqual(retried.json()["gateway_sync_status"], "pending")
            self.worker()
        failed = self.env(tenant_id)
        self.assertEqual((failed.gateway_sync_status, failed.gateway_error), ("failed", "gateway down"))
        again = self.client.post("/api/v1/tenant/model-environments/production/sync", headers=base)
        self.assertEqual(again.json()["gateway_sync_status"], "pending")
        self.worker()
        self.assertEqual(self.env(tenant_id).gateway_sync_status, "synced")

    def test_without_a_configured_gateway_nothing_is_queued(self) -> None:
        tenant_id = self.make_tenant()
        self.enable_model(tenant_id)
        base = {"X-Tenant-ID": str(tenant_id), **self.login("admin1@example.com")}
        response = self.client.post("/api/v1/tenant/model-environments/production/sync", headers=base)
        self.assertEqual(response.json()["gateway_sync_status"], "not_configured")
        with self.session_factory() as db:
            self.assertEqual(list(db.scalars(select(Job))), [])

    def test_catalog_changes_are_synced_by_the_worker_too(self) -> None:
        self.gateway()
        headers = self.login("platform@example.com")
        payload = {
            "display_name": "Queued Model", "provider": "private", "model_type": "chat", "availability": "all_plans",
            "input_price_per_million": "1", "output_price_per_million": "2", "input_points_per_thousand": "1",
            "output_points_per_thousand": "2", "status": "healthy", "provider_model_id": "queued-model",
            "endpoint_url": "https://models.example.test/v1", "api_key": "sk-provider-key", "region": "ap_singapore",
        }
        created = self.client.post("/api/v1/platform/models", json=payload, headers=headers)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["gateway_sync_status"], "pending")
        self.assertEqual(FakeLiteLLM.calls, [])
        self.worker()
        listing = self.client.get("/api/v1/platform/models").json()
        rows = listing["models"] if isinstance(listing, dict) and "models" in listing else listing
        listed = {m["id"]: m for m in rows}
        self.assertEqual(listed[created.json()["id"]]["gateway_sync_status"], "synced")
        self.assertEqual([c[0] for c in FakeLiteLLM.calls], ["upsert_model"])

    def test_the_hourly_reconcile_requeues_keys_that_disagree_with_the_tenant_status(self) -> None:
        self.gateway()
        should_block = self.keyed_tenant("suspended")  # a block that never made it (e.g. a long outage)
        should_unblock = self.keyed_tenant("active")
        in_step = self.keyed_tenant("active")
        with self.session_factory.begin() as db:
            db.query(TenantModelEnvironment).filter(TenantModelEnvironment.tenant_id == should_unblock).update({"gateway_key_blocked": True})
        with self.session_factory.begin() as db:
            self.assertEqual(gateway_sync.reconcile_tenant_access(db), 2)
            self.assertEqual(gateway_sync.reconcile_tenant_access(db), 2)  # same hour: the queue deduplicates
        with self.session_factory() as db:
            jobs = list(db.scalars(select(Job).where(Job.kind == "gateway.sync_tenant_access")))
        self.assertEqual({j.tenant_id for j in jobs}, {should_block, should_unblock})
        self.assertEqual({j.max_attempts for j in jobs}, {gateway_sync.ACCESS_JOB_ATTEMPTS})
        self.assertNotIn(in_step, {j.tenant_id for j in jobs})
        self.worker()
        self.assertEqual(sorted(c[0] for c in FakeLiteLLM.calls), ["block", "unblock"])
        self.assertTrue(self.env(should_block).gateway_key_blocked)
        self.assertFalse(self.env(should_unblock).gateway_key_blocked)

    def test_blocking_after_a_status_change_survives_a_long_gateway_outage(self) -> None:
        self.gateway()
        tenant_id = self.keyed_tenant()
        self.transition(tenant_id, "suspended")
        with self.session_factory() as db:
            job = db.scalar(select(Job).where(Job.kind == "gateway.sync_tenant_access"))
            self.assertGreaterEqual(job.max_attempts, 30)

    def test_only_due_tenants_are_selected_for_automatic_transitions(self) -> None:
        # Not-yet-due tenants stay untouched even when many exist (and are not locked by the hourly job).
        for days in range(1, 6):
            self.make_tenant("trial", trial_ends_at=NOW + timedelta(days=days))
            self.make_tenant("grace", status_changed_at=NOW - timedelta(days=days))
            self.make_tenant("pending_deletion", status_changed_at=NOW - timedelta(days=days))
        self.assertEqual(self.auto(), {"trial_expired": 0, "grace_to_read_only": 0, "pending_deletion_deleted": 0})
        self.assertEqual(self.auto(NOW + timedelta(days=3, hours=1))["trial_expired"], 3)

    # -- a re-sync must not take a working environment offline ------------------------------------------------

    def test_an_environment_with_a_key_stays_usable_while_a_re_sync_is_pending(self) -> None:
        from beework.assistant_checks import check_assistant
        from beework.models.assistants import Assistant

        tenant_id = self.keyed_tenant()
        with self.session_factory.begin() as db:
            row = db.scalar(select(TenantModelEnvironment).where(TenantModelEnvironment.tenant_id == tenant_id))
            row.gateway_sync_status = "pending"  # a model-list save has just queued a re-sync
            org = db.scalar(select(Organization).where(Organization.tenant_id == tenant_id))
            assistant = Assistant(tenant_id=tenant_id, organization_id=org.id, name="A", description="d", draft_config={"prompt": "p", "model_id": str(self.chat_id)})
            db.add(assistant)
            db.flush()
            assistant_id = assistant.id
        with self.session_factory() as db:
            row = db.scalar(select(TenantModelEnvironment).where(TenantModelEnvironment.tenant_id == tenant_id))
            self.assertEqual(gateway_sync.tenant_gateway_key(row), "sk-virtual-key")
            issues = check_assistant(db, db.get(Assistant, assistant_id), enabled_model_ids={str(self.chat_id)}, environment="production")
            self.assertNotIn("runtime.gateway_not_ready", [i.code for i in issues])
        # A first-time environment (no key yet) and a failed one are still not ready.
        with self.session_factory.begin() as db:
            row = db.scalar(select(TenantModelEnvironment).where(TenantModelEnvironment.tenant_id == tenant_id))
            row.gateway_sync_status = "failed"
        with self.session_factory() as db:
            row = db.scalar(select(TenantModelEnvironment).where(TenantModelEnvironment.tenant_id == tenant_id))
            with self.assertRaises(GatewayError):
                gateway_sync.tenant_gateway_key(row)
        fresh = self.make_tenant()
        config = self.enable_model(fresh)
        with self.session_factory.begin() as db:
            db.get(TenantModelEnvironment, config.id).gateway_sync_status = "pending"
        with self.session_factory() as db:
            with self.assertRaises(GatewayError):
                gateway_sync.tenant_gateway_key(db.get(TenantModelEnvironment, config.id))

    # -- blocking keys of tenants that must not consume ---------------------------------------------------------

    def keyed_tenant(self, status: str = "active") -> UUID:
        tenant_id = self.make_tenant(status)
        config = self.enable_model(tenant_id)
        with self.session_factory.begin() as db:
            row = db.get(TenantModelEnvironment, config.id)
            row.gateway_key_encrypted = encrypt_model_api_key("sk-virtual-key")
            row.gateway_sync_status = "synced"
        return tenant_id

    def transition(self, tenant_id: UUID, to_status: str) -> None:
        with self.session_factory.begin() as db:
            apply_transition(db, db.get(Tenant, tenant_id), "test", to_status, now=NOW)

    def test_a_suspended_tenant_gets_its_virtual_key_blocked_and_restoring_unblocks_it(self) -> None:
        self.gateway()
        tenant_id = self.keyed_tenant()
        self.transition(tenant_id, "suspended")
        with self.session_factory() as db:
            self.assertEqual([(j.kind, j.tenant_id) for j in db.scalars(select(Job))], [("gateway.sync_tenant_access", tenant_id)])
        self.worker()
        self.assertEqual(FakeLiteLLM.calls, [("block", "sk-virtual-key")])
        self.assertTrue(self.env(tenant_id).gateway_key_blocked)
        self.transition(tenant_id, "active")
        self.worker()
        self.assertEqual(FakeLiteLLM.calls[-1], ("unblock", "sk-virtual-key"))
        self.assertFalse(self.env(tenant_id).gateway_key_blocked)

    def test_every_non_serving_status_blocks_and_serving_ones_do_not(self) -> None:
        self.gateway()
        for source, target, queued in (
            ("active", "grace", False), ("trial", "active", False), ("grace", "read_only", True),
            ("read_only", "suspended", False), ("suspended", "pending_deletion", False),
            ("pending_deletion", "deleted", False), ("read_only", "active", True),
        ):
            tenant_id = self.make_tenant(source)
            self.transition(tenant_id, target)
            with self.session_factory() as db:
                jobs = list(db.scalars(select(Job).where(Job.tenant_id == tenant_id, Job.kind == "gateway.sync_tenant_access")))
            self.assertEqual(bool(jobs), queued, f"{source} -> {target}")

    def test_access_sync_is_idempotent_and_follows_the_status_at_run_time(self) -> None:
        self.gateway()
        tenant_id = self.keyed_tenant()
        self.transition(tenant_id, "suspended")
        self.transition(tenant_id, "active")  # restored again before the worker got to it
        self.worker()
        self.assertEqual(FakeLiteLLM.calls, [])  # converges on "active": nothing to block
        with self.session_factory.begin() as db:
            gateway_sync.apply_tenant_access(db, db.get(Tenant, tenant_id))
        self.assertEqual(FakeLiteLLM.calls, [])

    def test_a_gateway_outage_retries_the_block_instead_of_dropping_it(self) -> None:
        self.gateway()
        tenant_id = self.keyed_tenant()
        self.transition(tenant_id, "suspended")
        FakeLiteLLM.fail_access = True
        self.worker()
        self.assertFalse(self.env(tenant_id).gateway_key_blocked)
        with self.session_factory() as db:
            job = db.scalar(select(Job).where(Job.kind == "gateway.sync_tenant_access"))
            self.assertEqual((job.status, job.last_error), ("queued", "gateway down"))
            job.run_after = datetime.now(UTC) - timedelta(seconds=1)
            db.commit()
        FakeLiteLLM.fail_access = False
        self.worker()
        self.assertTrue(self.env(tenant_id).gateway_key_blocked)

    def test_a_key_created_for_a_suspended_workspace_starts_blocked(self) -> None:
        self.gateway()
        tenant_id = self.make_tenant("suspended")
        config = self.enable_model(tenant_id)
        with self.session_factory.begin() as db:
            gateway_sync.sync_tenant_environment(db, db.get(Tenant, tenant_id), db.get(TenantModelEnvironment, config.id))
        self.assertEqual([c[0] for c in FakeLiteLLM.calls], ["ensure_team", "generate_key", "block"])
        row = self.env(tenant_id)
        self.assertTrue(row.gateway_key_blocked)
        self.assertEqual(decrypt_model_api_key(row.gateway_key_encrypted), "sk-virtual-key")

    def test_platform_lifecycle_actions_use_the_same_transition(self) -> None:
        self.gateway()
        tenant_id = self.keyed_tenant("trial")
        headers = self.login("platform@example.com")
        # Convert to active (no review): not a blocking change, nothing queued.
        converted = self.client.post(f"/api/v1/platform/tenants/{tenant_id}/lifecycle", json={"action": "convert_to_active"}, headers=headers)
        self.assertEqual(converted.status_code, 200, converted.text)
        with self.session_factory() as db:
            self.assertEqual(list(db.scalars(select(Job).where(Job.kind == "gateway.sync_tenant_access"))), [])
        self.assertEqual(self.tenant(tenant_id).status, "active")
        self.assertIsNotNone(self.tenant(tenant_id).status_changed_at)


if __name__ == "__main__":
    unittest.main()
