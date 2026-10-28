"""PostgreSQL-only behaviour that the SQLite suite cannot prove.

Runs only when TEST_DATABASE_URL points at a disposable, migrated PostgreSQL
database (see scripts/test-postgres.sh). It never touches the development DB.
"""

import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from beework.api.common import lock_tenant
from beework.audit_maintenance import verify_audit_chain
from beework.db import get_db
from beework.main import app
from beework.models import (
    AuditChainCheckpoint,
    AuditEvent,
    AuthSession,
    MemberInvitation,
    Membership,
    Organization,
    PlatformOperationRequest,
    Tenant,
    TenantModelEnvironment,
    TenantRole,
    User,
)
from beework.models.assistants import Assistant, AssistantVersion
from beework.models.jobs import Job
from beework.security import hash_password
from beework.worker.events import publish_run_event, stream_run_events
from beework.worker.queue import claim_jobs, enqueue
from isolation import disable_external_integrations

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL is not set")
class PostgresIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        disable_external_integrations()
        cls.engine = create_engine(TEST_DATABASE_URL, pool_size=20, max_overflow=10)
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
        with self.engine.begin() as connection:
            # Audit tables are append-only via triggers; truncate is the reset path.
            connection.execute(
                text("TRUNCATE audit_events, audit_chain_checkpoints, jobs, run_events RESTART IDENTITY CASCADE")
            )
        with self.session_factory.begin() as db:
            for model in (
                Assistant,
                PlatformOperationRequest,
                MemberInvitation,
                AuthSession,
                Membership,
                TenantRole,
                TenantModelEnvironment,
                Organization,
                Tenant,
                User,
            ):
                db.execute(delete(model))
            tenant = Tenant(name="Concurrency", slug="concurrency", seat_limit=3)
            admin = User(
                email="pg-admin@example.com",
                display_name="PG Admin",
                password_hash=hash_password("postgres admin password"),
            )
            db.add_all([tenant, admin])
            db.flush()
            organization = Organization(tenant_id=tenant.id, name=tenant.name)
            db.add(organization)
            db.flush()
            db.add(
                Membership(
                    tenant_id=tenant.id,
                    user_id=admin.id,
                    organization_id=organization.id,
                    role="tenant_admin",
                )
            )
            self.tenant_id = tenant.id
            self.organization_id = organization.id

    def test_concurrent_audit_writes_keep_a_single_valid_chain(self) -> None:
        writers, events_per_writer = 8, 5

        def write(index: int) -> None:
            for event_index in range(events_per_writer):
                with self.session_factory.begin() as db:
                    db.add(
                        AuditEvent(
                            tenant_id=self.tenant_id,
                            action="organization.created",
                            details={"name": f"writer-{index}-{event_index}"},
                        )
                    )

        with ThreadPoolExecutor(max_workers=writers) as pool:
            list(pool.map(write, range(writers)))

        scope = f"tenant:{self.tenant_id}"
        with self.session_factory() as db:
            sequences = list(
                db.scalars(
                    select(AuditEvent.chain_sequence)
                    .where(AuditEvent.chain_scope == scope)
                    .order_by(AuditEvent.chain_sequence)
                )
            )
            result = verify_audit_chain(db, scope)
        self.assertEqual(sequences, list(range(1, writers * events_per_writer + 1)))
        self.assertTrue(result["is_valid"], result)

    def test_database_trigger_rejects_audit_update_and_delete(self) -> None:
        with self.session_factory.begin() as db:
            db.add(AuditEvent(tenant_id=self.tenant_id, action="auth.login"))
        for statement in (
            "UPDATE audit_events SET action = 'tampered'",
            "DELETE FROM audit_events",
        ):
            with self.subTest(statement=statement):
                with self.assertRaises(DBAPIError), self.engine.begin() as connection:
                    connection.execute(text(statement))
        with self.session_factory() as db:
            self.assertIsNone(db.get(AuditChainCheckpoint, f"tenant:{self.tenant_id}"))
            self.assertEqual(
                db.scalar(select(AuditEvent.action).where(AuditEvent.tenant_id == self.tenant_id)),
                "auth.login",
            )

    def test_tenant_row_lock_does_not_block_tenant_audit_writes(self) -> None:
        # A tenant lock held by one request must not block another request's audit
        # insert (FK key-share lock); otherwise lock order inversion deadlocks.
        holder = self.session_factory()
        try:
            lock_tenant(holder, self.tenant_id)
            with self.engine.connect() as connection:
                connection.execute(text("SET lock_timeout = '2s'"))
                with Session(bind=connection) as writer:
                    writer.add(AuditEvent(tenant_id=self.tenant_id, action="auth.login"))
                    writer.commit()
        finally:
            holder.rollback()
            holder.close()

    def test_concurrent_invitations_cannot_exceed_seat_limit(self) -> None:
        def invite(index: int) -> int:
            client = TestClient(app)
            login = client.post(
                "/api/v1/auth/login",
                json={"email": "pg-admin@example.com", "password": "postgres admin password"},
            )
            self.assertEqual(login.status_code, 200, login.text)
            response = client.post(
                "/api/v1/members/invitations",
                json={
                    "emails": [f"invitee-{index}@example.com"],
                    "role": "viewer",
                    "organization_id": str(self.organization_id),
                },
                headers={
                    "X-Tenant-ID": str(self.tenant_id),
                    "X-CSRF-Token": client.cookies.get("beework_csrf"),
                },
            )
            return response.status_code

        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(invite, range(6)))

        # seat_limit 3 with one active admin leaves exactly two seats.
        self.assertEqual(sorted(statuses), [201, 201, 409, 409, 409, 409])
        with self.session_factory() as db:
            pending = db.scalars(
                select(MemberInvitation).where(
                    MemberInvitation.tenant_id == UUID(str(self.tenant_id))
                )
            ).all()
        self.assertEqual(len(pending), 2)

    def test_concurrent_assistant_creation_cannot_exceed_the_plan_quota(self) -> None:
        def create(index: int) -> int:
            client = TestClient(app)
            login = client.post(
                "/api/v1/auth/login",
                json={"email": "pg-admin@example.com", "password": "postgres admin password"},
            )
            self.assertEqual(login.status_code, 200, login.text)
            response = client.post(
                "/api/v1/assistants",
                json={"name": f"Concurrent {index}", "draft": True},
                headers={
                    "X-Tenant-ID": str(self.tenant_id),
                    "X-CSRF-Token": client.cookies.get("beework_csrf"),
                },
            )
            return response.status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(create, range(8)))

        # The standard plan allows 5 assistants; the tenant lock serialises the count-then-insert.
        self.assertEqual(sorted(statuses), [201] * 5 + [409] * 3)
        with self.session_factory() as db:
            count = db.scalar(
                select(text("count(*)")).select_from(Assistant).where(Assistant.tenant_id == self.tenant_id)
            )
        self.assertEqual(count, 5)

    def test_database_trigger_rejects_assistant_version_updates(self) -> None:
        from sqlalchemy.exc import DBAPIError

        with self.session_factory.begin() as db:
            assistant = Assistant(tenant_id=self.tenant_id, organization_id=self.organization_id, name="Versioned", draft_config={})
            db.add(assistant)
            db.flush()
            version = AssistantVersion(
                tenant_id=self.tenant_id, assistant_id=assistant.id, version_seq=1, version="1.0", snapshot={"k": "v"},
            )
            db.add(version)
            db.flush()
            version_id = version.id
        with self.engine.begin() as connection:
            with self.assertRaises(DBAPIError) as caught:
                connection.execute(
                    text("UPDATE assistant_versions SET change_note = 'tampered' WHERE id = :id"), {"id": version_id}
                )
            self.assertIn("immutable", str(caught.exception))
        with self.session_factory() as db:
            self.assertEqual(db.get(AssistantVersion, version_id).change_note, "")

    def test_concurrent_workers_claim_each_job_once(self) -> None:
        with self.session_factory.begin() as db:
            for index in range(8):
                enqueue(db, "noop.echo", {"index": index})

        def claim(worker_index: int) -> list[UUID]:
            with self.session_factory.begin() as db:
                return [job.id for job in claim_jobs(db, f"worker-{worker_index}", limit=8)]

        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(claim, range(2)))
        claimed_ids = [job_id for group in claimed for job_id in group]
        self.assertEqual(len(claimed_ids), 8)
        self.assertEqual(len(set(claimed_ids)), 8)
        with self.session_factory() as db:
            self.assertEqual(db.scalar(select(text("count(*)")).select_from(Job)), 8)

    def test_concurrent_enqueue_with_same_key_yields_one_job(self) -> None:
        """The loser blocks on the unique index, then gets the winner's job instead of an error."""
        winner_inserted = threading.Event()
        release_winner = threading.Event()

        def winner() -> UUID:
            with self.session_factory.begin() as db:
                job = enqueue(db, "audit.retention", {}, idempotency_key="pg-race")
                winner_inserted.set()
                release_winner.wait(10)
                return job.id

        def loser() -> UUID:
            winner_inserted.wait(10)
            with self.session_factory.begin() as db:
                return enqueue(db, "audit.retention", {}, idempotency_key="pg-race").id

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(winner)
            second = pool.submit(loser)
            winner_inserted.wait(10)
            time.sleep(0.5)  # let the loser reach the blocked INSERT
            release_winner.set()
            self.assertEqual(first.result(15), second.result(15))
        with self.session_factory() as db:
            self.assertEqual(db.scalar(select(text("count(*)")).select_from(Job)), 1)

    def _tenant_id(self) -> UUID:
        with self.session_factory.begin() as db:
            tenant = Tenant(name="Events", slug=f"events-{uuid4().hex[:8]}")
            db.add(tenant)
            db.flush()
            return tenant.id

    def test_concurrent_run_event_publish_assigns_unique_sequence(self) -> None:
        tenant_id = self._tenant_id()
        run_id = uuid4()

        class _NoValkey:
            def publish(self, *_args):
                return 0

        def publish(index: int) -> int:
            with self.session_factory() as db:
                return publish_run_event(
                    db, run_id, "token", {"i": index}, tenant_id=tenant_id, redis_client=_NoValkey()
                ).seq

        with ThreadPoolExecutor(max_workers=8) as pool:
            seqs = sorted(pool.map(publish, range(24)))
        self.assertEqual(seqs, list(range(1, 25)))

    def test_stream_receives_live_events_through_real_valkey(self) -> None:
        import redis

        from beework.config import settings

        try:
            probe = redis.Redis.from_url(settings.valkey_url)
            probe.ping()
            probe.close()
        except redis.RedisError:
            self.skipTest("Valkey is not reachable")
        tenant_id = self._tenant_id()
        run_id = uuid4()
        with self.session_factory() as db:
            publish_run_event(db, run_id, "queued", {}, tenant_id=tenant_id)
        received: list[str] = []
        started = time.monotonic()

        def consume() -> None:
            with self.session_factory() as db:
                # A long poll proves delivery comes from Valkey wake-ups, not polling.
                for item in stream_run_events(db, run_id, poll_seconds=30):
                    received.append(item["type"])

        consumer = threading.Thread(target=consume, daemon=True)
        consumer.start()
        time.sleep(0.5)
        for event_type in ("token", "token", "done"):
            with self.session_factory() as db:
                publish_run_event(db, run_id, event_type, {}, tenant_id=tenant_id)
        consumer.join(10)
        self.assertFalse(consumer.is_alive(), "stream did not finish")
        self.assertEqual(received, ["queued", "token", "token", "done"])
        self.assertLess(time.monotonic() - started, 10)


if __name__ == "__main__":
    unittest.main()
