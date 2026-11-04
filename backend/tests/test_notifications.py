import shutil
import tempfile
import unittest
from datetime import timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from beework.api.platform_tenants import _apply_lifecycle
from beework.approvals import approval_action, submit_change
from beework.db import get_db
from beework.main import app
from beework.models import Base, Membership, Organization, Tenant, User, utc_now
from beework.models.notifications import InAppNotification
from beework.notifications import notify, safe_link
from beework.security import hash_password
from isolation import disable_external_integrations, stub_public_dns

PASSWORD = "correct horse battery"


@approval_action("notif.apply")
def _apply(_db, _context) -> None:
    return None


class NotificationTests(unittest.TestCase):
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
        self.client = TestClient(app)
        with self.session_factory.begin() as db:
            tenant = Tenant(name="Acme", slug="acme", plan="enterprise", seat_limit=20)
            other = Tenant(name="Other", slug="other", plan="enterprise", seat_limit=20)
            db.add_all([tenant, other])
            db.flush()
            roots = {t.slug: Organization(tenant_id=t.id, name=t.name) for t in (tenant, other)}
            db.add_all(roots.values())
            db.flush()
            users = {n: User(email=f"{n}@example.com", display_name=n.title(), password_hash=hash_password(PASSWORD))
                     for n in ("admin", "builder", "foreign")}
            db.add_all(users.values())
            db.flush()
            db.add_all([
                Membership(tenant_id=tenant.id, user_id=users["admin"].id, organization_id=roots["acme"].id, role="tenant_admin"),
                Membership(tenant_id=tenant.id, user_id=users["builder"].id, organization_id=roots["acme"].id, role="builder"),
                Membership(tenant_id=other.id, user_id=users["foreign"].id, organization_id=roots["other"].id, role="tenant_admin"),
            ])
            self.ids = {"tenant": tenant.id, "other": other.id, **{n: u.id for n, u in users.items()}}

    def login(self, name: str, tenant: UUID | None = None) -> dict[str, str]:
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/api/v1/auth/login", json={"email": f"{name}@example.com", "password": PASSWORD}).status_code, 200)
        return {"X-CSRF-Token": self.client.cookies.get("beework_csrf"), "X-Tenant-ID": str(tenant or self.ids["tenant"])}

    def add(self, user: str, title: str = "t", tenant: str = "tenant", **kwargs) -> UUID:
        with self.session_factory.begin() as db:
            rows = notify(db, tenant_id=self.ids[tenant], recipient_ids=[self.ids[user]], type=kwargs.pop("type", "test"), title=title, **kwargs)
            db.flush()
            return rows[0].id

    # -- service --------------------------------------------------------------------

    def test_links_must_be_in_app_relative_paths(self) -> None:
        for good in (None, "", "/approvals", "/approvals?id=1&x=2"):
            safe_link(good)
        for bad in ("https://evil.example", "//evil.example", "javascript:alert(1)", "approvals", "/a b", "/\\evil", "/x\ny"):
            with self.assertRaises(ValueError, msg=bad):
                safe_link(bad)
        with self.session_factory.begin() as db, self.assertRaises(ValueError):
            notify(db, tenant_id=self.ids["tenant"], recipient_ids=[self.ids["admin"]], type="t", title="x", link="https://evil.example")

    def test_notify_by_role_and_ids_writes_one_row_per_distinct_user(self) -> None:
        with self.session_factory.begin() as db:
            rows = notify(db, tenant_id=self.ids["tenant"], role="tenant_admin", recipient_ids=[self.ids["admin"], self.ids["builder"]], type="t", title="x")
            self.assertEqual({r.recipient_id for r in rows}, {self.ids["admin"], self.ids["builder"]})
            self.assertEqual(len(rows), 2)
            self.assertEqual(notify(db, tenant_id=self.ids["tenant"], recipient_ids=[], type="t", title="x"), [])

    # -- API ---------------------------------------------------------------------------

    def test_users_only_read_their_own_notifications_per_tenant(self) -> None:
        mine = self.add("admin", "mine")
        theirs = self.add("builder", "theirs")
        foreign = self.add("foreign", "foreign", tenant="other")
        headers = self.login("admin")
        listing = self.client.get("/api/v1/notifications", headers=headers).json()
        self.assertEqual([i["title"] for i in listing["items"]], ["mine"])
        self.assertEqual(self.client.get("/api/v1/notifications/unread-count", headers=headers).json(), {"unread": 1})
        for other_id in (theirs, foreign):
            self.assertEqual(self.client.post(f"/api/v1/notifications/{other_id}/read", headers=headers).status_code, 404)
        self.assertEqual(self.client.post(f"/api/v1/notifications/{uuid4()}/read", headers=headers).status_code, 404)
        # Someone else's notification is untouched by read-all.
        self.assertEqual(self.client.post("/api/v1/notifications/read-all", headers=headers).json(), {"updated": 1})
        with self.session_factory() as db:
            self.assertIsNone(db.get(InAppNotification, theirs).read_at)
            self.assertIsNone(db.get(InAppNotification, foreign).read_at)
            self.assertIsNotNone(db.get(InAppNotification, mine).read_at)
        # Another tenant's workspace of the same user shows nothing from this tenant.
        foreign_headers = self.login("foreign", self.ids["other"])
        self.assertEqual([i["title"] for i in self.client.get("/api/v1/notifications", headers=foreign_headers).json()["items"]], ["foreign"])
        crossing = self.client.get("/api/v1/notifications", headers={**foreign_headers, "X-Tenant-ID": str(self.ids["tenant"])})
        self.assertEqual(crossing.status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/v1/notifications").status_code, 401)

    def test_unread_filter_ordering_pagination_and_marking_read(self) -> None:
        ids = []
        for index in range(5):
            ids.append(self.add("admin", f"n{index}"))
            with self.session_factory.begin() as db:
                db.get(InAppNotification, ids[-1]).created_at = utc_now() + timedelta(seconds=index)
        headers = self.login("admin")
        page = self.client.get("/api/v1/notifications?limit=2&offset=1", headers=headers).json()
        self.assertEqual((page["total"], [i["title"] for i in page["items"]]), (5, ["n3", "n2"]))  # newest first
        read = self.client.post(f"/api/v1/notifications/{ids[4]}/read", headers=headers)
        self.assertEqual((read.status_code, read.json()["read_at"] is not None), (200, True))
        again = self.client.post(f"/api/v1/notifications/{ids[4]}/read", headers=headers)
        self.assertEqual(again.json()["read_at"].rstrip("Z"), read.json()["read_at"].rstrip("Z"))  # idempotent
        unread = self.client.get("/api/v1/notifications?unread=true", headers=headers).json()
        self.assertEqual((unread["total"], "n4" in [i["title"] for i in unread["items"]]), (4, False))
        self.assertEqual(self.client.get("/api/v1/notifications/unread-count", headers=headers).json(), {"unread": 4})
        no_csrf = self.client.post("/api/v1/notifications/read-all", headers={"X-Tenant-ID": headers["X-Tenant-ID"]})
        self.assertEqual(no_csrf.status_code, 403)

    def test_notifications_can_be_read_while_the_workspace_is_read_only(self) -> None:
        # The status-change notice itself arrives when a tenant turns read-only or suspended.
        for status in ("read_only", "suspended"):
            with self.session_factory.begin() as db:
                db.get(Tenant, self.ids["tenant"]).status = status
                db.query(InAppNotification).delete()
            note = self.add("admin", f"in {status}")
            headers = self.login("admin")
            self.assertEqual(self.client.post(f"/api/v1/notifications/{note}/read", headers=headers).status_code, 200, status)
            self.add("admin", "second")
            self.assertEqual(self.client.post("/api/v1/notifications/read-all", headers=headers).json(), {"updated": 1}, status)
        # Other writes stay blocked in a read-only workspace.
        self.assertEqual(self.client.post("/api/v1/roles", json={}, headers=headers).status_code, 403)

    # -- producers ----------------------------------------------------------------------

    def test_approval_events_notify_approvers_then_the_requester(self) -> None:
        with self.session_factory.begin() as db:
            approval_id = submit_change(
                db, tenant_id=self.ids["tenant"], event="publish", action="notif.apply", object_type="assistant",
                object_id=uuid4(), title="发布 Helper v1.0", requester_id=self.ids["builder"],
            ).approval.id
        admin = self.login("admin")
        item = self.client.get("/api/v1/notifications", headers=admin).json()["items"][0]
        self.assertEqual((item["type"], item["link"], item["params"]["title"]), ("approval.submitted", f"/approvals?id={approval_id}", "发布 Helper v1.0"))
        self.assertEqual(self.client.get("/api/v1/notifications", headers=self.login("builder")).json()["total"], 0)
        admin = self.login("admin")
        self.assertEqual(self.client.post(f"/api/v1/approvals/{approval_id}/approve", json={}, headers=admin).status_code, 200)
        types = [i["type"] for i in self.client.get("/api/v1/notifications", headers=self.login("builder")).json()["items"]]
        self.assertEqual(types, ["approval.approved"])

    def test_tenant_status_change_notifies_tenant_admins_only(self) -> None:
        with self.session_factory.begin() as db:
            tenant = db.get(Tenant, self.ids["tenant"])
            _apply_lifecycle(db, tenant, "suspend", "suspended")
            _apply_lifecycle(db, tenant, "suspend", "suspended")  # no change, no second notification
        admin_items = self.client.get("/api/v1/notifications", headers=self.login("admin")).json()["items"]
        self.assertEqual([(i["type"], i["params"]) for i in admin_items], [("tenant.status_changed", {"status": "suspended"})])
        with self.session_factory() as db:
            recipients = set(db.scalars(select(InAppNotification.recipient_id)))
        self.assertEqual(recipients, {self.ids["admin"]})


if __name__ == "__main__":
    unittest.main()
