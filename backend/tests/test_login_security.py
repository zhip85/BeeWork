import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from beework import login_security
from beework.config import Settings, settings
from beework.db import get_db
from beework.main import app
from beework.models import (
    AuditEvent,
    AuthSession,
    Base,
    LoginThrottle,
    Membership,
    Organization,
    Tenant,
    User,
)
from beework.models.notifications import InAppNotification
from beework.security import hash_password
from beework.worker.scheduler import auth_release_locks
from isolation import disable_external_integrations, stub_public_dns

PASSWORD = "correct horse battery"
START = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.current = START

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **kwargs) -> None:
        self.current += timedelta(**kwargs)


class LoginSecurityTests(unittest.TestCase):
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
        self.clock = Clock()
        patcher = mock.patch.object(login_security, "now", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(app)
        with self.session_factory.begin() as db:
            tenant = Tenant(name="Acme", slug="acme", plan="enterprise", seat_limit=20)
            db.add(tenant)
            db.flush()
            root = Organization(tenant_id=tenant.id, name="Acme")
            db.add(root)
            db.flush()
            admin = User(email="admin@example.com", display_name="Admin", password_hash=hash_password(PASSWORD))
            member = User(email="member@example.com", display_name="Member", password_hash=hash_password(PASSWORD))
            db.add_all([admin, member])
            db.flush()
            db.add_all([
                Membership(tenant_id=tenant.id, user_id=admin.id, organization_id=root.id, role="tenant_admin"),
                Membership(tenant_id=tenant.id, user_id=member.id, organization_id=root.id, role="builder"),
            ])
            self.ids = {"tenant": tenant.id, "admin": admin.id, "member": member.id}

    # -- helpers ---------------------------------------------------------------------

    def attempt(self, email: str = "member@example.com", password: str = "wrong password!", client: TestClient | None = None):
        return (client or self.client).post("/api/v1/auth/login", json={"email": email, "password": password})

    def fail(self, times: int, email: str = "member@example.com") -> list:
        return [self.attempt(email) for _ in range(times)]

    # -- account lock-out ---------------------------------------------------------------

    def test_the_fifth_failure_locks_the_account_even_for_the_right_password(self) -> None:
        first_four = self.fail(4)
        self.assertEqual({r.status_code for r in first_four}, {401})
        fifth = self.attempt()
        self.assertEqual((fifth.status_code, fifth.json()["code"]), (423, "auth.locked"))
        self.assertIn("15 分钟", fifth.json()["detail"])
        correct = self.attempt(password=PASSWORD)
        self.assertEqual((correct.status_code, correct.json()["code"]), (423, "auth.locked"))
        self.assertEqual(correct.headers["Retry-After"], str(15 * 60))
        self.clock.advance(minutes=6)
        later = self.attempt(password=PASSWORD)
        self.assertEqual(later.status_code, 423)
        self.assertIn("9 分钟", later.json()["detail"])
        self.assertEqual(later.headers["Retry-After"], str(9 * 60))

    def test_the_lock_expires_after_fifteen_minutes_and_the_unlock_is_audited(self) -> None:
        self.fail(5)
        self.clock.advance(minutes=15, seconds=1)
        ok = self.attempt(password=PASSWORD)
        self.assertEqual(ok.status_code, 200, ok.text)
        with self.session_factory() as db:
            actions = [e.action for e in db.scalars(select(AuditEvent).where(AuditEvent.action.like("auth.account_%")).order_by(AuditEvent.created_at))]
            self.assertEqual(actions, ["auth.account_locked", "auth.account_unlocked"])
            locked = db.scalar(select(AuditEvent).where(AuditEvent.action == "auth.account_locked"))
            self.assertEqual((locked.category, locked.result, locked.details["email"]), ("security", "warning", "member@example.com"))
            self.assertIsNone(db.scalar(select(LoginThrottle).where(LoginThrottle.scope == "account")))  # success clears it

    def test_a_successful_login_resets_the_counter(self) -> None:
        self.fail(4)
        self.assertEqual(self.attempt(password=PASSWORD).status_code, 200)
        self.client.cookies.clear()
        self.assertEqual({r.status_code for r in self.fail(4)}, {401})  # four more failures do not lock

    def test_old_failures_do_not_count_as_consecutive(self) -> None:
        self.fail(4)
        self.clock.advance(minutes=16)
        self.assertEqual(self.attempt().status_code, 401)  # the fifth failure overall, but the first of a new run
        self.assertEqual(self.attempt(password=PASSWORD).status_code, 200)

    def test_the_sweep_unlocks_and_audits_without_a_login_attempt(self) -> None:
        self.fail(5)
        self.clock.advance(minutes=16)
        with self.session_factory.begin() as db:
            auth_release_locks(db, {})
            auth_release_locks(db, {})  # idempotent
        with self.session_factory() as db:
            self.assertEqual(len(list(db.scalars(select(AuditEvent).where(AuditEvent.action == "auth.account_unlocked")))), 1)

    def test_locking_notifies_tenant_admins_only(self) -> None:
        self.fail(5)
        with self.session_factory() as db:
            rows = list(db.scalars(select(InAppNotification)))
            self.assertEqual([(r.recipient_id, r.type, r.params["email"]) for r in rows], [(self.ids["admin"], "security.account_locked", "member@example.com")])
            self.assertEqual(rows[0].link, "/members")

    def test_unknown_emails_behave_exactly_like_real_ones(self) -> None:
        real = self.fail(5, "member@example.com")
        ghost = self.fail(5, "nobody@example.com")
        self.assertEqual([r.status_code for r in real], [r.status_code for r in ghost])
        self.assertEqual([r.json()["code"] for r in real if r.status_code == 423], [r.json()["code"] for r in ghost if r.status_code == 423])
        self.assertEqual(real[0].json(), ghost[0].json())  # same wording for a wrong password
        self.assertEqual(real[-1].json(), ghost[-1].json())  # same wording for the lock
        with self.session_factory() as db:
            self.assertEqual(len(list(db.scalars(select(AuditEvent).where(AuditEvent.action == "auth.account_locked")))), 1)  # only the real one

    def test_locking_one_account_does_not_lock_another(self) -> None:
        self.fail(5)
        other = self.attempt("admin@example.com", PASSWORD)
        self.assertEqual(other.status_code, 200)

    # -- IP throttling ----------------------------------------------------------------------

    def test_too_many_failures_from_one_ip_return_429_with_retry_after(self) -> None:
        for index in range(login_security.IP_MAX_FAILURES):
            self.assertEqual(self.attempt(f"user{index}@example.com").status_code, 401, index)  # distinct emails: no account lock
        blocked = self.attempt("user-final@example.com")
        self.assertEqual((blocked.status_code, blocked.json()["code"]), (429, "auth.rate_limited"))
        self.assertEqual(blocked.headers["Retry-After"], "60")
        # Even a valid login from that address waits: the throttle runs before any password work.
        self.assertEqual(self.attempt("admin@example.com", PASSWORD).status_code, 429)
        self.clock.advance(seconds=30)
        self.assertEqual(self.attempt("admin@example.com", PASSWORD).headers["Retry-After"], "30")
        self.clock.advance(seconds=31)
        self.assertEqual(self.attempt("admin@example.com", PASSWORD).status_code, 200)

    def test_other_addresses_are_not_throttled(self) -> None:
        for index in range(login_security.IP_MAX_FAILURES):
            self.attempt(f"user{index}@example.com")
        other_ip = TestClient(app, client=("203.0.113.9", 50000))
        self.assertEqual(self.attempt("admin@example.com", PASSWORD, client=other_ip).status_code, 200)

    # -- sessions -----------------------------------------------------------------------------------

    def login(self) -> TestClient:
        client = TestClient(app)
        self.assertEqual(self.attempt("admin@example.com", PASSWORD, client=client).status_code, 200)
        return client

    def test_idle_sessions_expire_and_activity_slides_the_window(self) -> None:
        client = self.login()
        for _ in range(3):  # busy for 3 x 100 minutes, never idle for the 120-minute default
            self.clock.advance(minutes=100)
            self.assertEqual(client.get("/api/v1/auth/me").status_code, 200)
        self.clock.advance(minutes=121)
        expired = client.get("/api/v1/auth/me")
        self.assertEqual(expired.status_code, 401)
        self.assertIn("长时间无操作", expired.json()["detail"])
        with self.session_factory() as db:
            self.assertIsNotNone(db.scalar(select(AuthSession.revoked_at)))

    def test_background_polling_does_not_keep_a_session_alive(self) -> None:
        client = self.login()
        for _ in range(3):
            self.clock.advance(minutes=50)
            self.assertEqual(client.get("/api/v1/auth/me", headers={"X-Background-Request": "1"}).status_code, 200 if _ < 2 else 401)

    def test_the_absolute_lifetime_is_configurable(self) -> None:
        with mock.patch.object(settings, "session_max_minutes", 30), mock.patch.object(settings, "session_idle_minutes", 120):
            client = self.login()
            with self.session_factory() as db:
                session = db.scalar(select(AuthSession))
                self.assertEqual((session.expires_at.replace(tzinfo=UTC) - START).total_seconds(), 30 * 60)
            self.clock.advance(minutes=29)
            self.assertEqual(client.get("/api/v1/auth/me").status_code, 200)
            self.clock.advance(minutes=2)  # still active, but past 30 minutes since login
            self.assertEqual(client.get("/api/v1/auth/me").status_code, 401)

    def test_default_lifetime_is_twelve_hours_and_settings_are_range_checked(self) -> None:
        self.assertEqual(Settings().session_max_minutes, 720)
        for field in ("session_max_minutes", "session_idle_minutes"):
            for bad in (14, 7 * 24 * 60 + 1):
                with self.assertRaises(ValueError):
                    Settings(**{field: bad})
            Settings(**{field: 15})
            Settings(**{field: 7 * 24 * 60})


if __name__ == "__main__":
    unittest.main()
