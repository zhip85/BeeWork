"""T-087: the event bus and Webhook endpoints — signing, the retry schedule, auto-disable, the API and the events the platform emits."""

import hashlib
import hmac
import json
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

import test_channels
from beework import events, webhooks
from beework.db import get_db
from beework.main import app
from beework.models import AuditEvent, Base, Membership, Organization, Tenant, User
from beework.models.jobs import Job
from beework.models.notifications import InAppNotification
from beework.models.webhooks import DomainEvent, WebhookDelivery, WebhookEndpoint
from beework.security import hash_password
from isolation import disable_external_integrations, stub_public_dns

PASSWORD = "correct horse battery"
PUBLIC = lambda _host: ["93.184.216.34"]  # noqa: E731
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
URL = "https://hooks.example.com/beework"


def verify(secret: str, headers: dict[str, str], body: bytes) -> bool:
    """The receiver's side, exactly as docs/webhooks.md shows it."""
    expected = "sha256=" + hmac.new(secret.encode(), headers["x-beework-timestamp"].encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, headers["x-beework-signature"])


class SigningTests(unittest.TestCase):
    def test_the_signature_is_hmac_sha256_over_timestamp_dot_body(self) -> None:
        # A fixed vector, computed by hand with the documented formula, so the formula cannot drift unnoticed.
        self.assertEqual(
            webhooks.sign("whsec_test", "1700000000", b'{"a":1}'),
            "sha256=" + hmac.new(b"whsec_test", b'1700000000.{"a":1}', hashlib.sha256).hexdigest(),
        )
        self.assertNotEqual(webhooks.sign("whsec_test", "1700000000", b'{"a":1}'), webhooks.sign("whsec_test", "1700000001", b'{"a":1}'))
        self.assertNotEqual(webhooks.sign("whsec_test", "1700000000", b'{"a":1}'), webhooks.sign("whsec_other", "1700000000", b'{"a":1}'))

    def test_the_retry_schedule_is_1_5_30_minutes_then_hourly(self) -> None:
        self.assertEqual([webhooks.retry_delay(n) for n in range(1, 7)], [timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=1), timedelta(hours=1), timedelta(hours=1)])


class DocumentedVerifierTests(unittest.TestCase):
    """The example in docs/webhooks.md is code a customer will copy: run it against what the platform really signs."""

    def documented_verify(self):
        import re
        from pathlib import Path

        text = (Path(__file__).resolve().parents[2] / "docs" / "webhooks.md").read_text(encoding="utf-8")
        block = re.search(r"```python\n(.*?)```", text, re.S).group(1)
        namespace: dict = {}
        exec(block, namespace)  # noqa: S102 - our own documentation, run only here
        return namespace["verify"]

    def test_the_documented_python_verifier_accepts_what_the_platform_signs_and_rejects_tampering(self) -> None:
        verify_documented = self.documented_verify()
        secret, body = webhooks.new_secret(), b'{"data":{"a":1},"event":"approval.updated","id":"x"}'
        stamp = str(int(datetime.now(UTC).timestamp()))
        headers = {"x-beework-timestamp": stamp, "x-beework-signature": webhooks.sign(secret, stamp, body)}
        self.assertTrue(verify_documented(secret, headers, body))
        self.assertFalse(verify_documented(secret, headers, body + b" "))
        self.assertFalse(verify_documented("whsec_other", headers, body))
        old = str(int((datetime.now(UTC) - timedelta(minutes=10)).timestamp()))
        self.assertFalse(verify_documented(secret, {"x-beework-timestamp": old, "x-beework-signature": webhooks.sign(secret, old, body)}, body))  # replay window


class Base_(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        disable_external_integrations()
        stub_public_dns()
        cls._db_dir = tempfile.mkdtemp(prefix="beework-test-")
        cls.engine = create_engine(f"sqlite:///{cls._db_dir}/test.db", connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(cls.engine, "connect")
        def pragmas(connection, _record) -> None:
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
            tenants = {code: Tenant(name=code, slug=code, plan="enterprise", seat_limit=20) for code in ("mine", "other")}
            db.add_all(tenants.values())
            db.flush()
            self.ids = {}
            for code, tenant in tenants.items():
                root = Organization(tenant_id=tenant.id, name=code)
                db.add(root)
                db.flush()
                for role in ("admin", "viewer"):
                    user = User(email=f"{role}@{code}.example.com", display_name=f"{role}-{code}", password_hash=hash_password(PASSWORD))
                    db.add(user)
                    db.flush()
                    db.add(Membership(tenant_id=tenant.id, user_id=user.id, organization_id=root.id, role="tenant_admin" if role == "admin" else "viewer"))
                self.ids[code] = tenant.id

    def login(self, role: str = "admin", code: str = "mine") -> dict[str, str]:
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/api/v1/auth/login", json={"email": f"{role}@{code}.example.com", "password": PASSWORD}).status_code, 200)
        return {"X-CSRF-Token": self.client.cookies.get("beework_csrf"), "X-Tenant-ID": str(self.ids[code])}


class Helpers(Base_):
    def endpoint(self, *, code: str = "mine", events_: tuple[str, ...] = ("approval.updated",), enabled: bool = True) -> tuple[UUID, str]:
        secret = webhooks.new_secret()
        with self.session_factory.begin() as db:
            row = WebhookEndpoint(tenant_id=self.ids[code], url=URL, events=list(events_), enabled=enabled, secret_encrypted="", secret_last4="")
            webhooks.seal_secret(row, secret)
            db.add(row)
            db.flush()
            return row.id, secret

    def queue(self, endpoint_id: UUID, *, created: datetime = NOW, payload: dict | None = None) -> UUID:
        with self.session_factory.begin() as db:
            row = db.get(WebhookEndpoint, endpoint_id)
            delivery = webhooks.queue_delivery(db, row, uuid4(), "approval.updated", payload or {"approval_id": "a1", "state": "approved"})
            delivery.created_at = created
            return delivery.id

    def send(self, delivery_id: UUID, now: datetime, handler) -> WebhookDelivery:
        with self.session_factory() as db:
            webhooks.deliver(db, delivery_id, now=now, transport=httpx.MockTransport(handler), resolver=PUBLIC)
        with self.session_factory() as db:
            return db.get(WebhookDelivery, delivery_id)


class DeliveryTests(Helpers):
    def test_a_delivery_carries_the_event_the_delivery_id_a_timestamp_and_a_signature_the_receiver_can_verify(self) -> None:
        endpoint_id, secret = self.endpoint()
        delivery_id = self.queue(endpoint_id)
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"ok": True})

        row = self.send(delivery_id, NOW, handler)
        request = seen[0]
        headers = {k.lower(): v for k, v in request.headers.items()}
        # The connection is pinned to the validated address (DNS rebinding); the host and path the receiver sees are the configured ones.
        self.assertEqual((request.method, request.url.path, headers["host"]), ("POST", "/beework", "hooks.example.com"))
        self.assertEqual((headers["x-beework-event"], headers["x-beework-delivery"], headers["x-beework-timestamp"]), ("approval.updated", str(delivery_id), str(int(NOW.timestamp()))))
        self.assertTrue(verify(secret, headers, request.content))  # recomputed from the secret, the timestamp and the exact bytes received
        self.assertFalse(verify("whsec_wrong", headers, request.content))
        self.assertFalse(verify(secret, headers, request.content + b" "))  # a changed body fails
        body = json.loads(request.content)
        self.assertEqual((body["id"], body["event"], body["data"]), (str(delivery_id), "approval.updated", {"approval_id": "a1", "state": "approved"}))
        self.assertEqual((row.status, row.status_code, row.attempts, row.error), ("delivered", 200, 1, None))
        with self.session_factory() as db:
            endpoint = db.get(WebhookEndpoint, endpoint_id)
            self.assertEqual((endpoint.last_status, endpoint.last_status_code, endpoint.failing_since), ("delivered", 200, None))

    def test_failures_are_retried_on_schedule_and_the_delivery_id_never_changes(self) -> None:
        endpoint_id, secret = self.endpoint()
        delivery_id = self.queue(endpoint_id)
        ids, signatures, bodies = [], [], []

        def handler(request: httpx.Request) -> httpx.Response:
            ids.append(request.headers["x-beework-delivery"])
            signatures.append(request.headers["x-beework-signature"])
            bodies.append(request.content)
            return httpx.Response(500)

        clock = NOW
        for expected in (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=1), timedelta(hours=1)):
            row = self.send(delivery_id, clock, handler)
            self.assertEqual((row.status, row.status_code, row.error), ("retrying", 500, "对方返回 500"))
            self.assertEqual(webhooks.aware(row.next_retry_at), clock + expected)
            clock = clock + expected
        self.assertEqual(set(ids), {str(delivery_id)})  # receivers de-duplicate on this
        self.assertEqual(len(set(bodies)), 1)  # the very same body each time
        with self.session_factory() as db:
            jobs = list(db.scalars(select(Job).where(Job.kind == "webhooks.deliver").order_by(Job.created_at)))
            self.assertEqual(len(jobs), 6)  # the first attempt and five retries, each its own job at its own time
            self.assertEqual(len({j.idempotency_key for j in jobs}), 6)
            retries = sorted(webhooks.aware(j.run_after) for j in jobs if not j.idempotency_key.endswith(":0"))
            self.assertEqual(retries, [NOW + timedelta(minutes=1), NOW + timedelta(minutes=6), NOW + timedelta(minutes=36), NOW + timedelta(minutes=96), NOW + timedelta(minutes=156)])

    def test_a_success_ends_the_retries_and_clears_the_failure_run(self) -> None:
        endpoint_id, _ = self.endpoint()
        delivery_id = self.queue(endpoint_id)
        self.send(delivery_id, NOW, lambda _r: httpx.Response(503))
        row = self.send(delivery_id, NOW + timedelta(minutes=1), lambda _r: httpx.Response(204))
        self.assertEqual((row.status, row.attempts, row.next_retry_at), ("delivered", 2, None))
        with self.session_factory() as db:
            self.assertIsNone(db.get(WebhookEndpoint, endpoint_id).failing_since)
        again = self.send(delivery_id, NOW + timedelta(minutes=2), lambda _r: httpx.Response(500))
        self.assertEqual((again.status, again.attempts), ("delivered", 2))  # a delivered delivery is not sent again

    def test_after_24_hours_the_delivery_is_given_up(self) -> None:
        endpoint_id, _ = self.endpoint()
        delivery_id = self.queue(endpoint_id, created=NOW)
        row = self.send(delivery_id, NOW + timedelta(hours=23, minutes=59), lambda _r: httpx.Response(500))
        self.assertEqual(row.status, "retrying")
        row = self.send(delivery_id, NOW + timedelta(hours=24), lambda _r: httpx.Response(500))
        self.assertEqual((row.status, row.next_retry_at), ("dead", None))

    def test_24_hours_of_failures_switch_the_endpoint_off_and_tell_the_administrators(self) -> None:
        endpoint_id, _ = self.endpoint()
        first = self.queue(endpoint_id, created=NOW)
        self.send(first, NOW, lambda _r: httpx.Response(500))  # the run of failures starts here
        second = self.queue(endpoint_id, created=NOW + timedelta(hours=23))
        self.send(second, NOW + timedelta(hours=23, minutes=59), lambda _r: httpx.Response(500))
        with self.session_factory() as db:
            self.assertTrue(db.get(WebhookEndpoint, endpoint_id).enabled)  # not 24 hours yet
        third = self.queue(endpoint_id, created=NOW + timedelta(hours=24))
        self.send(third, NOW + timedelta(hours=24), lambda _r: httpx.Response(500))
        with self.session_factory() as db:
            endpoint = db.get(WebhookEndpoint, endpoint_id)
            self.assertEqual((endpoint.enabled, endpoint.disabled_reason), (False, "failing"))
            self.assertEqual(db.scalar(select(AuditEvent.action).where(AuditEvent.action == "webhook.auto_disabled")), "webhook.auto_disabled")
            note = db.scalars(select(InAppNotification).where(InAppNotification.type == "webhook.disabled")).first()
            self.assertIsNotNone(note)
            self.assertNotIn("whsec_", json.dumps([note.title, note.body]))

    def test_a_success_in_between_restarts_the_24_hours(self) -> None:
        endpoint_id, _ = self.endpoint()
        self.send(self.queue(endpoint_id, created=NOW), NOW, lambda _r: httpx.Response(500))
        self.send(self.queue(endpoint_id, created=NOW + timedelta(hours=10)), NOW + timedelta(hours=10), lambda _r: httpx.Response(200))
        self.send(self.queue(endpoint_id, created=NOW + timedelta(hours=30)), NOW + timedelta(hours=30), lambda _r: httpx.Response(500))
        with self.session_factory() as db:
            endpoint = db.get(WebhookEndpoint, endpoint_id)
            self.assertTrue(endpoint.enabled)
            self.assertEqual(webhooks.aware(endpoint.failing_since), NOW + timedelta(hours=30))

    def test_a_switched_off_or_deleted_endpoint_gets_nothing_more(self) -> None:
        endpoint_id, _ = self.endpoint()
        delivery_id = self.queue(endpoint_id)
        with self.session_factory.begin() as db:
            db.get(WebhookEndpoint, endpoint_id).enabled = False
        calls: list[int] = []
        row = self.send(delivery_id, NOW, lambda _r: (calls.append(1), httpx.Response(200))[1])
        self.assertEqual((row.status, calls), ("cancelled", []))

    def test_an_endpoint_that_resolves_to_an_internal_address_is_refused_at_delivery_time(self) -> None:
        endpoint_id, _ = self.endpoint()
        delivery_id = self.queue(endpoint_id)
        with self.session_factory() as db:
            webhooks.deliver(db, delivery_id, now=NOW, transport=httpx.MockTransport(lambda _r: httpx.Response(200)), resolver=lambda _h: ["10.0.0.5"])  # DNS now points inside
        with self.session_factory() as db:
            row = db.get(WebhookDelivery, delivery_id)
            self.assertEqual((row.status, row.status_code), ("retrying", None))
            self.assertTrue(row.error)

    def test_a_timeout_is_a_failure_with_a_short_reason(self) -> None:
        endpoint_id, _ = self.endpoint()
        delivery_id = self.queue(endpoint_id)

        def slow(_request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("took too long")

        row = self.send(delivery_id, NOW, slow)
        self.assertEqual(row.status, "retrying")
        self.assertTrue(row.error)
        self.assertLess(len(row.error), 200)

    def test_only_the_latest_100_deliveries_of_an_endpoint_are_kept(self) -> None:
        endpoint_id, _ = self.endpoint()
        for index in range(105):
            self.queue(endpoint_id, created=NOW + timedelta(seconds=index))
        with self.session_factory() as db:
            self.assertLessEqual(len(db.scalars(select(WebhookDelivery).where(WebhookDelivery.endpoint_id == endpoint_id)).all()), 105)
        with self.session_factory.begin() as db:
            webhooks.prune(db, endpoint_id)
        with self.session_factory() as db:
            self.assertEqual(len(db.scalars(select(WebhookDelivery).where(WebhookDelivery.endpoint_id == endpoint_id)).all()), 100)


class BusTests(Helpers):
    def emit(self, code: str = "mine", kind: str = "approval.updated") -> DomainEvent:
        with self.session_factory.begin() as db:
            return events.emit_event(db, self.ids[code], kind, {"approval_id": "a1", "state": "approved"})

    def dispatch(self, event_id: UUID) -> None:
        with self.session_factory() as db:
            events.dispatch_event(db, {"event_id": str(event_id)})

    def test_an_event_nobody_listens_to_is_only_recorded_and_costs_no_job(self) -> None:
        self.endpoint(events_=("usage.threshold",))  # a different event
        self.endpoint(events_=("approval.updated",), enabled=False)  # switched off
        self.endpoint(code="other", events_=("approval.updated",))  # another tenant
        quiet = self.emit()
        with self.session_factory() as db:
            self.assertIsNotNone(db.get(DomainEvent, quiet.id).dispatched_at)
            self.assertIsNone(db.scalar(select(Job.id).where(Job.kind == "events.dispatch")))

    def test_an_event_is_an_outbox_row_with_a_dispatch_job_and_unknown_types_are_refused(self) -> None:
        self.endpoint(events_=("approval.updated",))
        event_row = self.emit()
        with self.session_factory() as db:
            self.assertIsNone(db.get(DomainEvent, event_row.id).dispatched_at)
            self.assertEqual(db.scalar(select(Job.kind).where(Job.idempotency_key == f"events.dispatch:{event_row.id}")), "events.dispatch")
        with self.session_factory.begin() as db:
            with self.assertRaises(ValueError):
                events.emit_event(db, self.ids["mine"], "something.else", {})

    def test_dispatch_reaches_only_enabled_subscribed_endpoints_of_the_same_tenant_once(self) -> None:
        wanted, _ = self.endpoint(events_=("approval.updated", "usage.threshold"))
        self.endpoint(events_=("assistant.published",))  # not subscribed
        self.endpoint(events_=("approval.updated",), enabled=False)  # switched off
        self.endpoint(code="other", events_=("approval.updated",))  # another tenant's
        event_row = self.emit()
        self.dispatch(event_row.id)
        self.dispatch(event_row.id)  # a retried dispatch job must not send twice
        with self.session_factory() as db:
            rows = db.scalars(select(WebhookDelivery)).all()
            self.assertEqual([(r.endpoint_id, r.event_type, r.event_id) for r in rows], [(wanted, "approval.updated", event_row.id)])
            self.assertIsNotNone(db.get(DomainEvent, event_row.id).dispatched_at)

    def test_other_subscribers_can_join_the_bus(self) -> None:
        seen: list[str] = []
        listener = events.subscribe(lambda _db, e: seen.append(e.type))  # no interest check: it wants everything
        try:
            event_row = self.emit(kind="usage.threshold")
            with self.session_factory() as db:
                self.assertEqual(db.scalar(select(Job.kind).where(Job.idempotency_key == f"events.dispatch:{event_row.id}")), "events.dispatch")
            self.dispatch(event_row.id)
        finally:
            events.SUBSCRIBERS.remove(listener)
        self.assertEqual(seen, ["usage.threshold"])


class ApiTests(Base_):
    def create(self, headers, **fields):
        return self.client.post("/api/v1/webhooks", json={"url": URL, "events": ["approval.updated"], **fields}, headers=headers)

    def test_creating_shows_the_secret_once_and_the_list_only_a_mask(self) -> None:
        admin = self.login()
        created = self.create(admin)
        self.assertEqual(created.status_code, 201, created.text)
        secret = created.json()["signing_secret"]
        self.assertRegex(secret, r"^whsec_[A-Za-z0-9_-]{40,}$")
        listed = self.client.get("/api/v1/webhooks", headers=admin).json()
        self.assertEqual(listed["events"], list(events.EVENT_TYPES))
        item = listed["items"][0]
        self.assertEqual((item["secret_mask"], item["enabled"], item["last_delivery"]), (f"whsec_••••{secret[-4:]}", True, None))
        self.assertNotIn(secret, json.dumps(listed))
        with self.session_factory() as db:
            self.assertNotIn(secret, db.scalar(select(WebhookEndpoint.secret_encrypted)))  # stored encrypted
        rotated = self.client.post(f"/api/v1/webhooks/{item['id']}/rotate-secret", headers=admin).json()
        self.assertNotEqual(rotated["signing_secret"], secret)
        with self.session_factory() as db:
            self.assertEqual(webhooks.signing_secret(db.get(WebhookEndpoint, UUID(item["id"]))), rotated["signing_secret"])

    def test_addresses_must_be_public_https_and_events_known(self) -> None:
        admin = self.login()
        for url in ("http://hooks.example.com/x", "https://localhost/x", "https://10.0.0.5/x", "https://127.0.0.1/x", "https://intranet/x", "ftp://hooks.example.com"):
            refused = self.create(admin, url=url)
            self.assertEqual(refused.status_code, 400, url)
            self.assertEqual(refused.json()["code"], "security.invalid_url")
        self.assertEqual(self.create(admin, events=[]).status_code, 422)  # at least one event
        self.assertEqual(self.create(admin, events=["nope"]).status_code, 422)
        self.assertEqual(self.create(admin, events=["approval.updated", "approval.updated"]).json()["events"], ["approval.updated"])

    def test_permissions_isolation_and_the_endpoint_limit(self) -> None:
        admin = self.login()
        webhook = self.create(admin).json()
        viewer = self.login("viewer")
        self.assertEqual(self.client.get("/api/v1/webhooks", headers=viewer).status_code, 403)  # settings are for administrators
        self.assertEqual(self.create(viewer).status_code, 403)
        self.assertEqual(self.client.delete(f"/api/v1/webhooks/{webhook['id']}", headers=viewer).status_code, 403)
        other = self.login("admin", "other")
        self.assertEqual(self.client.get("/api/v1/webhooks", headers=other).json()["items"], [])
        for call in (lambda: self.client.put(f"/api/v1/webhooks/{webhook['id']}", json={"url": URL, "events": ["approval.updated"]}, headers=other), lambda: self.client.delete(f"/api/v1/webhooks/{webhook['id']}", headers=other),
                     lambda: self.client.post(f"/api/v1/webhooks/{webhook['id']}/test", headers=other), lambda: self.client.get(f"/api/v1/webhooks/{webhook['id']}/deliveries", headers=other),
                     lambda: self.client.post(f"/api/v1/webhooks/{webhook['id']}/rotate-secret", headers=other)):
            self.assertEqual(call().status_code, 404)
        admin = self.login()
        for _ in range(19):
            self.assertEqual(self.create(admin).status_code, 201)
        self.assertEqual(self.create(admin).status_code, 409)

    def test_updating_and_turning_an_endpoint_on_again_starts_a_new_run_of_failures(self) -> None:
        admin = self.login()
        webhook = self.create(admin).json()
        with self.session_factory.begin() as db:
            row = db.get(WebhookEndpoint, UUID(webhook["id"]))
            row.enabled, row.disabled_reason, row.failing_since = False, "failing", NOW
        shown = self.client.get("/api/v1/webhooks", headers=admin).json()["items"][0]
        self.assertEqual((shown["enabled"], shown["disabled_reason"]), (False, "failing"))
        saved = self.client.put(f"/api/v1/webhooks/{webhook['id']}", json={"url": "https://hooks.example.com/new", "events": ["usage.threshold"], "enabled": True, "row_version": shown["row_version"]}, headers=admin)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual((saved.json()["enabled"], saved.json()["disabled_reason"], saved.json()["failing_since"], saved.json()["url"]), (True, None, None, "https://hooks.example.com/new"))
        self.assertEqual(self.client.put(f"/api/v1/webhooks/{webhook['id']}", json={"url": "https://10.0.0.1/x", "events": ["usage.threshold"]}, headers=admin).status_code, 400)
        stale = self.client.put(f"/api/v1/webhooks/{webhook['id']}", json={"url": URL, "events": ["usage.threshold"], "row_version": 1}, headers=admin)
        self.assertEqual(stale.status_code, 409)

    def test_the_test_event_is_sent_even_to_a_switched_off_endpoint_and_shows_in_the_records(self) -> None:
        admin = self.login()
        webhook = self.create(admin, enabled=False).json()
        sent = self.client.post(f"/api/v1/webhooks/{webhook['id']}/test", headers=admin)
        self.assertEqual(sent.status_code, 202, sent.text)
        self.assertEqual((sent.json()["event"], sent.json()["is_test"], sent.json()["status"]), ("webhook.test", True, "pending"))
        with self.session_factory() as db:
            webhooks.deliver(db, UUID(sent.json()["id"]), now=NOW, transport=httpx.MockTransport(lambda _r: httpx.Response(200)), resolver=PUBLIC)
        records = self.client.get(f"/api/v1/webhooks/{webhook['id']}/deliveries", headers=admin).json()["items"]
        self.assertEqual([(r["event"], r["status"], r["status_code"], r["attempts"]) for r in records], [("webhook.test", "delivered", 200, 1)])
        last = self.client.get("/api/v1/webhooks", headers=admin).json()["items"][0]["last_delivery"]
        self.assertEqual((last["status"], last["status_code"]), ("delivered", 200))

    def test_redelivery_sends_the_same_delivery_again(self) -> None:
        admin = self.login()
        webhook = self.create(admin).json()
        sent = self.client.post(f"/api/v1/webhooks/{webhook['id']}/test", headers=admin).json()
        with self.session_factory() as db:
            webhooks.deliver(db, UUID(sent["id"]), now=NOW, transport=httpx.MockTransport(lambda _r: httpx.Response(500)), resolver=PUBLIC)
        again = self.client.post(f"/api/v1/webhooks/{webhook['id']}/deliveries/{sent['id']}/redeliver", headers=admin)
        self.assertEqual((again.status_code, again.json()["id"], again.json()["status"]), (202, sent["id"], "pending"))
        seen: list[str] = []
        with self.session_factory() as db:
            webhooks.deliver(db, UUID(sent["id"]), now=NOW + timedelta(minutes=2), transport=httpx.MockTransport(lambda r: (seen.append(r.headers["x-beework-delivery"]), httpx.Response(200))[1]), resolver=PUBLIC)
        self.assertEqual(seen, [sent["id"]])  # the receiver sees the id it already knows
        self.assertEqual(self.client.post(f"/api/v1/webhooks/{webhook['id']}/deliveries/{uuid4()}/redeliver", headers=admin).status_code, 404)

    def test_deleting_an_endpoint_removes_its_deliveries(self) -> None:
        admin = self.login()
        webhook = self.create(admin).json()
        self.client.post(f"/api/v1/webhooks/{webhook['id']}/test", headers=admin)
        self.assertEqual(self.client.delete(f"/api/v1/webhooks/{webhook['id']}", headers=admin).status_code, 204)
        with self.session_factory() as db:
            self.assertEqual(db.scalars(select(WebhookDelivery)).all(), [])


class EmittedEventTests(test_channels.ChannelBase):
    """The events the platform emits, from the real code paths that cause them (PRD 5.8)."""

    def require_approval(self) -> None:
        from beework.approvals import ensure_policies

        with self.session_factory.begin() as db:
            ensure_policies(db, self.ids["tenant"])["channel"].enabled = True

    def types(self) -> list[tuple[str, dict]]:
        with self.session_factory() as db:
            return [(e.type, e.payload) for e in db.scalars(select(DomainEvent).order_by(DomainEvent.created_at))]

    def test_publishing_an_assistant_emits_assistant_published_with_ids_only(self) -> None:
        published = [p for t, p in self.types() if t == "assistant.published"]  # the fixture published one
        self.assertEqual(len(published), 1)
        self.assertEqual(set(published[0]), {"assistant_id", "version", "version_id", "kind"})
        self.assertEqual(published[0]["assistant_id"], self.assistant_id)

    def test_every_change_of_an_approval_emits_approval_updated(self) -> None:
        self.require_approval()
        channel = self.client.post("/api/v1/channels", json={"name": "Site", "assistant_id": self.assistant_id, "allowed_origins": ["https://www.example.com"]}, headers=self.login("admin")).json()
        approval_id = self.client.post(f"/api/v1/channels/{channel['id']}/publish", json={}, headers=self.login("admin")).json()["approval_id"]
        self.client.post(f"/api/v1/approvals/{approval_id}/approve", json={}, headers=self.login("admin2"))
        states = [p["state"] for t, p in self.types() if t == "approval.updated" and p["approval_id"] == approval_id]
        self.assertEqual(states, ["submitted", "approved"])
        payload = next(p for t, p in self.types() if t == "approval.updated")
        self.assertEqual(set(payload), {"approval_id", "state", "type", "object_type", "object_id", "level"})  # no title, no diff, no comments
        rejected = self.client.post("/api/v1/channels", json={"name": "Site 2", "assistant_id": self.assistant_id, "allowed_origins": ["https://b.example.com"]}, headers=self.login("admin")).json()
        second = self.client.post(f"/api/v1/channels/{rejected['id']}/publish", json={}, headers=self.login("admin")).json()["approval_id"]
        self.client.post(f"/api/v1/approvals/{second}/reject", json={"comment": "私密意见"}, headers=self.login("admin2"))
        later = [p["state"] for t, p in self.types() if t == "approval.updated" and p["approval_id"] == second]
        self.assertEqual(later, ["submitted", "rejected"])
        self.assertNotIn("私密意见", json.dumps(self.types(), ensure_ascii=False))

    def test_a_conversation_that_ends_emits_conversation_completed_without_its_text(self) -> None:
        key = self.create_key()["key"]
        chat = self.chat(key, "我的手机号是 13800138000，请帮我查订单")
        self.assertEqual(chat.status_code, 200, chat.text)
        conversation_id = chat.json()["conversation_id"]
        from beework.conversations import end_conversation
        from beework.models.conversations import Conversation

        with self.session_factory.begin() as db:
            end_conversation(db, db.get(Conversation, UUID(conversation_id)), datetime.now(UTC))
        completed = [p for t, p in self.types() if t == "conversation.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(set(completed[0]), {"conversation_id", "assistant_id", "status", "source", "turns", "ended_at"})
        self.assertEqual((completed[0]["conversation_id"], completed[0]["status"]), (conversation_id, "resolved"))
        self.assertNotIn("13800138000", json.dumps(self.types(), ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
