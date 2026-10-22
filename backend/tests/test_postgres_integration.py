"""PostgreSQL-only behaviour that the SQLite suite cannot prove.

Runs only when TEST_DATABASE_URL points at a disposable, migrated PostgreSQL
database (see scripts/test-postgres.sh). It never touches the development DB.
"""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

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
from beework.security import hash_password
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
                text("TRUNCATE audit_events, audit_chain_checkpoints RESTART IDENTITY CASCADE")
            )
        with self.session_factory.begin() as db:
            for model in (
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


if __name__ == "__main__":
    unittest.main()
