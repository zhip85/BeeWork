import shutil
import tempfile
import unittest
from datetime import timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from beework import approvals
from beework.approvals import ApplyContext, approval_action, submit_change
from beework.db import get_db
from beework.errors import ApiError
from beework.main import app
from beework.models import (
    AuditEvent,
    Base,
    Membership,
    Organization,
    Tenant,
    TenantRole,
    User,
    utc_now,
)
from beework.models.approvals import ApprovalRequest
from beework.models.jobs import Job
from beework.security import hash_password
from beework.worker.runner import run_once
from isolation import disable_external_integrations, stub_public_dns

PASSWORD = "correct horse battery"
APPLIED: list[dict] = []
FAIL_NEXT = [False]


@approval_action("test.apply")
def _apply(_db, context: ApplyContext) -> None:
    if FAIL_NEXT[0]:
        raise RuntimeError("boom")
    APPLIED.append({"object": str(context.object_id), "approval": context.approval_id, **context.payload})


class ApprovalTests(unittest.TestCase):
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
        APPLIED.clear()
        FAIL_NEXT[0] = False
        self.client = TestClient(app)
        with self.session_factory.begin() as db:
            tenant = Tenant(name="Acme", slug="acme", plan="enterprise", seat_limit=20)
            starter = Tenant(name="Small", slug="small", plan="standard", seat_limit=20)
            other = Tenant(name="Other", slug="other", plan="enterprise", seat_limit=20)
            db.add_all([tenant, starter, other])
            db.flush()
            roots = {t.slug: Organization(tenant_id=t.id, name=t.name) for t in (tenant, starter, other)}
            db.add_all(roots.values())
            db.flush()
            users = {}
            for name in ("admin", "admin2", "builder", "security", "viewer", "auditor", "outsider", "solo", "foreign"):
                users[name] = User(email=f"{name}@example.com", display_name=name.title(), password_hash=hash_password(PASSWORD))
            db.add_all(users.values())
            db.flush()
            security_role = TenantRole(
                tenant_id=tenant.id, name="Security", scope="tenant", permissions={"assistants": ["view"]},
                approver_tags=["security_officer"], created_by=users["admin"].id,
            )
            db.add(security_role)
            db.flush()
            root = roots["acme"]
            members = [
                ("admin", "tenant_admin"), ("admin2", "tenant_admin"), ("builder", "builder"), ("viewer", "viewer"),
                ("auditor", "auditor"), ("outsider", "builder"),
            ]
            for name, role in members:
                db.add(Membership(tenant_id=tenant.id, user_id=users[name].id, organization_id=root.id, role=role))
            db.add(Membership(tenant_id=tenant.id, user_id=users["security"].id, organization_id=root.id,
                              role="custom", custom_role_id=security_role.id))
            db.add(Membership(tenant_id=starter.id, user_id=users["solo"].id, organization_id=roots["small"].id, role="tenant_admin"))
            db.add(Membership(tenant_id=other.id, user_id=users["foreign"].id, organization_id=roots["other"].id, role="tenant_admin"))
            self.ids = {"tenant": tenant.id, "starter": starter.id, "other": other.id, "root": root.id,
                        **{name: user.id for name, user in users.items()}}

    # -- helpers -----------------------------------------------------------------

    def login(self, name: str, tenant: UUID | None = None) -> dict[str, str]:
        self.client.cookies.clear()
        response = self.client.post("/api/v1/auth/login", json={"email": f"{name}@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return {"X-CSRF-Token": self.client.cookies.get("beework_csrf"), "X-Tenant-ID": str(tenant or self.ids["tenant"])}

    def set_policy(self, event_name: str = "publish", **fields) -> None:
        with self.session_factory.begin() as db:
            policy = approvals.ensure_policies(db, self.ids["tenant"])[event_name]
            for key, value in fields.items():
                setattr(policy, key, value)

    def submit(self, requester: str = "builder", object_id: UUID | None = None, event_name: str = "publish", **kwargs):
        with self.session_factory.begin() as db:
            result = submit_change(
                db, tenant_id=kwargs.pop("tenant_id", self.ids["tenant"]), event=event_name, action="test.apply",
                object_type="assistant", object_id=object_id or self.object_id, title="发布 Helper v1.0",
                payload={"version": "1.0"}, requester_id=self.ids[requester], organization_id=self.ids["root"],
                checks=[{"label": "回归测试通过", "level": "ok"}], change_diff=[{"field": "prompt"}], **kwargs,
            )
            return result.approval.id if result.approval else None

    @property
    def object_id(self) -> UUID:
        if not hasattr(self, "_object_id"):
            self._object_id = uuid4()
        return self._object_id

    def act(self, name: str, approval_id: UUID, action: str, body: dict | None = None):
        headers = self.login(name)
        return self.client.post(f"/api/v1/approvals/{approval_id}/{action}", json=body or {}, headers=headers)

    def drain(self) -> int:
        return run_once("test-worker", self.session_factory, schedule=False)

    def request(self, approval_id: UUID) -> ApprovalRequest:
        with self.session_factory() as db:
            return db.get(ApprovalRequest, approval_id)

    # -- policies ------------------------------------------------------------------

    def test_default_policies_follow_the_prd(self) -> None:
        headers = self.login("admin")
        body = self.client.get("/api/v1/tenant/approval-policies", headers=headers).json()
        by_event = {item["event"]: item for item in body["items"]}
        self.assertEqual(list(by_event), ["publish", "channel", "byok", "kb", "skill", "rule", "export"])
        self.assertEqual((by_event["publish"]["approver_role"], by_event["publish"]["levels"], by_event["publish"]["enabled"]), ("tenant_admin", 1, True))
        self.assertEqual((by_event["byok"]["approver_role"], by_event["byok"]["levels"]), ("security_officer", 2))
        self.assertEqual([k for k, v in by_event.items() if not v["enabled"]], ["kb", "export"])
        self.assertTrue(body["multi_level_allowed"])

    def test_policy_update_validation_audit_and_permissions(self) -> None:
        headers = self.login("admin")
        item = {"event": "publish", "approver_role": "security_officer", "levels": 1, "enabled": True}
        ok = self.client.put("/api/v1/tenant/approval-policies", json={"items": [item]}, headers=headers)
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(next(i for i in ok.json()["items"] if i["event"] == "publish")["approver_role"], "security_officer")
        with self.session_factory() as db:
            row = db.scalar(select(AuditEvent).where(AuditEvent.action == "approval.policy_updated"))
            self.assertEqual(row.details["after"], "security_officer/1/on")
        stale = self.client.put("/api/v1/tenant/approval-policies", json={"items": [{**item, "enabled": False, "row_version": 1}]}, headers=headers)
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "conflict.stale"))
        bad = self.client.put("/api/v1/tenant/approval-policies", json={"items": [{**item, "levels": 3}]}, headers=headers)
        self.assertEqual(bad.status_code, 422)
        duplicate = self.client.put("/api/v1/tenant/approval-policies", json={"items": [item, item]}, headers=headers)
        self.assertEqual(duplicate.status_code, 422)
        builder = self.login("builder")
        self.assertEqual(self.client.put("/api/v1/tenant/approval-policies", json={"items": [item]}, headers=builder).status_code, 403)
        self.assertEqual(self.client.get("/api/v1/tenant/approval-policies", headers=self.login("auditor")).status_code, 200)

    def test_two_levels_require_the_enterprise_entitlement(self) -> None:
        solo = self.login("solo", self.ids["starter"])
        item = {"event": "publish", "approver_role": "tenant_admin", "levels": 2, "enabled": True}
        response = self.client.put("/api/v1/tenant/approval-policies", json={"items": [item]}, headers=solo)
        self.assertEqual((response.status_code, response.json()["code"]), (403, "plan.feature_locked"))
        self.assertFalse(self.client.get("/api/v1/tenant/approval-policies", headers=solo).json()["multi_level_allowed"])

    def test_two_levels_cannot_be_smuggled_in_while_a_policy_is_switched_off(self) -> None:
        solo = self.login("solo", self.ids["starter"])
        base = {"event": "publish", "approver_role": "tenant_admin"}
        put = lambda **fields: self.client.put(  # noqa: E731
            "/api/v1/tenant/approval-policies", json={"items": [{**base, **fields}]}, headers=solo
        )
        self.assertEqual(put(levels=2, enabled=False).status_code, 403)  # saving 2 levels while off is refused too
        self.assertEqual(put(levels=1, enabled=False).status_code, 200)
        self.assertEqual(put(levels=2, enabled=True).status_code, 403)

    # -- submit ----------------------------------------------------------------------

    def test_policy_off_applies_directly_without_a_request(self) -> None:
        self.set_policy(enabled=False)
        self.assertIsNone(self.submit())
        self.assertEqual(len(APPLIED), 1)
        self.assertIsNone(APPLIED[0]["approval"])
        with self.session_factory() as db:
            self.assertEqual(db.scalar(select(ApprovalRequest.id)), None)

    def test_submit_resolves_approvers_and_never_the_requester(self) -> None:
        approval_id = self.submit("builder")
        request = self.request(approval_id)
        self.assertEqual((request.status, request.current_level, request.type, request.apply_status), ("pending", 1, "publish", "pending"))
        self.assertEqual({UUID(u) for u in request.levels[0]["approvers"]}, {self.ids["admin"], self.ids["admin2"]})
        with self.session_factory() as db:
            self.assertIsNotNone(db.scalar(select(AuditEvent).where(AuditEvent.action == "approval.submitted")))

    def test_admin_requester_is_excluded_and_a_lone_admin_is_refused(self) -> None:
        request = self.request(self.submit("admin"))
        self.assertEqual([UUID(u) for u in request.levels[0]["approvers"]], [self.ids["admin2"]])
        with self.session_factory.begin() as db:
            policy = approvals.ensure_policies(db, self.ids["starter"])["publish"]
            policy.enabled = True
        with self.assertRaises(ApiError) as caught, self.session_factory.begin() as db:
            submit_change(db, tenant_id=self.ids["starter"], event="publish", action="test.apply", object_type="assistant",
                          object_id=uuid4(), title="t", requester_id=self.ids["solo"])
        self.assertEqual(caught.exception.code, "approval.no_approver")

    def test_security_officer_tag_selects_approvers_and_falls_back_to_admins(self) -> None:
        self.set_policy(approver_role="security_officer")
        request = self.request(self.submit("builder"))
        self.assertEqual([UUID(u) for u in request.levels[0]["approvers"]], [self.ids["security"]])
        with self.session_factory.begin() as db:
            db.query(TenantRole).update({"approver_tags": []})
        fallback = self.request(self.submit("builder", object_id=uuid4()))
        self.assertEqual({UUID(u) for u in fallback.levels[0]["approvers"]}, {self.ids["admin"], self.ids["admin2"]})

    def test_approvers_need_no_module_permission_and_outsiders_see_nothing(self) -> None:
        self.set_policy(approver_role="security_officer")
        approval_id = self.submit("builder")
        security = self.login("security")  # role has only assistants:view, but is tagged security officer
        summary = self.client.get("/api/v1/approvals/summary", headers=security).json()
        self.assertEqual((summary["pending_mine"], summary["visible_count"]), (1, 1))
        listing = self.client.get("/api/v1/approvals", headers=security).json()
        self.assertEqual([item["id"] for item in listing["items"]], [str(approval_id)])
        self.assertEqual(self.client.get(f"/api/v1/approvals/{approval_id}", headers=security).status_code, 200)
        self.assertEqual(self.client.post(f"/api/v1/approvals/{approval_id}/approve", json={}, headers=security).status_code, 200)
        outsider = self.login("viewer")
        self.assertEqual(self.client.get("/api/v1/approvals/summary", headers=outsider).json()["visible_count"], 0)

    def test_one_pending_request_per_object_with_replace(self) -> None:
        first = self.submit()
        with self.assertRaises(ApiError) as caught:
            self.submit()
        self.assertEqual(caught.exception.code, "approval.pending_exists")
        second = self.submit(replace_pending=True)
        self.assertEqual(self.request(first).status, "withdrawn")
        self.assertEqual(self.request(second).status, "pending")
        self.assertEqual(self.submit(object_id=uuid4()) is not None, True)  # other objects are independent

    # -- decisions ---------------------------------------------------------------------

    def test_single_level_approve_enqueues_apply_and_runs_once(self) -> None:
        approval_id = self.submit()
        response = self.act("admin", approval_id, "approve", {"comment": "ok"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["status"], body["apply_status"]), ("approved", "pending"))
        self.assertEqual(APPLIED, [])  # applied by the worker, not inline
        self.assertEqual(self.drain(), 1)
        self.assertEqual(len(APPLIED), 1)
        self.assertEqual(APPLIED[0]["approval"], approval_id)
        self.assertEqual(self.request(approval_id).apply_status, "succeeded")
        self.assertEqual(self.drain(), 0)
        with self.session_factory() as db:
            actions = [e.action for e in db.scalars(select(AuditEvent).where(AuditEvent.action.like("approval.%")).order_by(AuditEvent.created_at))]
        self.assertEqual(actions, ["approval.submitted", "approval.approved", "approval.applied"])

    def test_two_levels_flow_in_order(self) -> None:
        self.set_policy(levels=2)
        approval_id = self.submit()
        # A member who is neither requester nor approver cannot even see the request.
        self.assertEqual(self.act("security", approval_id, "approve").status_code, 404)
        first = self.act("admin", approval_id, "approve").json()
        self.assertEqual((first["status"], first["current_level"]), ("pending", 2))
        self.assertEqual(self.drain(), 0)
        second = self.act("admin2", approval_id, "approve").json()
        self.assertEqual(second["status"], "approved")
        self.assertEqual([level["status"] for level in second["levels"]], ["approved", "approved"])
        self.assertEqual(self.drain(), 1)

    def test_rejection_requires_a_comment_and_ends_the_request(self) -> None:
        approval_id = self.submit()
        self.assertEqual(self.act("admin", approval_id, "reject", {"comment": "  "}).status_code, 422)
        rejected = self.act("admin", approval_id, "reject", {"comment": "需要补充测试"})
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual((rejected.json()["status"], rejected.json()["comments"][0]["body"]), ("rejected", "需要补充测试"))
        self.assertEqual(self.act("admin2", approval_id, "approve").status_code, 409)
        self.assertEqual(self.drain(), 0)
        self.assertEqual(APPLIED, [])

    def test_only_current_approvers_decide_and_not_the_requester(self) -> None:
        approval_id = self.submit("admin")  # requester is an admin
        self.assertEqual(self.act("admin", approval_id, "approve").status_code, 403)
        self.assertEqual(self.act("builder", approval_id, "approve").status_code, 404)  # not involved: cannot see it
        self.assertEqual(self.act("outsider", approval_id, "reject", {"comment": "x"}).status_code, 404)
        self.assertEqual(self.act("admin2", approval_id, "approve").status_code, 200)

    def test_withdraw_is_for_the_requester_while_pending(self) -> None:
        approval_id = self.submit("builder")
        self.assertEqual(self.act("admin", approval_id, "withdraw").status_code, 403)
        done = self.act("builder", approval_id, "withdraw")
        self.assertEqual((done.status_code, done.json()["status"]), (200, "withdrawn"))
        self.assertEqual(self.act("builder", approval_id, "withdraw").status_code, 409)
        self.assertEqual(self.act("admin", approval_id, "approve").status_code, 409)

    def test_failed_apply_is_visible_and_can_be_retried(self) -> None:
        approval_id = self.submit()
        self.act("admin", approval_id, "approve")
        FAIL_NEXT[0] = True
        self.drain()
        failed = self.request(approval_id)
        self.assertEqual((failed.status, failed.apply_status, failed.apply_error), ("approved", "failed", "boom"))
        self.assertEqual(APPLIED, [])
        self.assertEqual(self.act("viewer", approval_id, "retry").status_code, 404)
        self.assertEqual(self.act("builder", approval_id, "retry").status_code, 200)  # the requester may retry
        FAIL_NEXT[0] = False
        self.assertEqual(self.drain(), 1)
        self.assertEqual(self.request(approval_id).apply_status, "succeeded")
        self.assertEqual(len(APPLIED), 1)
        self.assertEqual(self.act("builder", approval_id, "retry").status_code, 409)
        with self.session_factory() as db:
            actions = {e.action for e in db.scalars(select(AuditEvent).where(AuditEvent.action.like("approval.%")))}
            self.assertTrue({"approval.apply_failed", "approval.apply_retried", "approval.applied"} <= actions)
            self.assertEqual(len(list(db.scalars(select(Job).where(Job.kind == "approvals.apply")))), 2)

    def test_unregistered_action_fails_visibly(self) -> None:
        approval_id = self.submit()
        self.act("admin", approval_id, "approve")
        with self.session_factory.begin() as db:
            db.get(ApprovalRequest, approval_id).action = "nope.missing"
        self.drain()
        self.assertEqual(self.request(approval_id).apply_status, "failed")

    # -- expiry and reminders ---------------------------------------------------------------

    def test_sweep_reminds_once_after_a_day_and_expires_after_a_week(self) -> None:
        notices: list[str] = []
        approvals.NOTIFIERS.append(lambda _db, name, _req, _to: notices.append(name))
        self.addCleanup(approvals.NOTIFIERS.pop)
        approval_id = self.submit()
        notices.clear()
        now = utc_now()
        with self.session_factory.begin() as db:
            approvals.sweep(db, now=now + timedelta(hours=23))
        self.assertEqual(notices, [])
        for _ in range(2):  # a second sweep must not remind again
            with self.session_factory.begin() as db:
                approvals.sweep(db, now=now + timedelta(hours=25))
        self.assertEqual(notices, ["approval.reminder"])
        with self.session_factory.begin() as db:
            approvals.sweep(db, now=now + timedelta(days=7, minutes=1))
        self.assertEqual(self.request(approval_id).status, "expired")
        self.assertEqual(notices[-1], "approval.expired")
        self.assertEqual(self.act("admin", approval_id, "approve").status_code, 409)
        with self.session_factory() as db:
            self.assertIsNotNone(db.scalar(select(AuditEvent).where(AuditEvent.action == "approval.expired")))

    def test_sweep_job_is_scheduled(self) -> None:
        from datetime import UTC, datetime

        from beework.worker.scheduler import enqueue_scheduled_jobs

        with self.session_factory.begin() as db:
            enqueue_scheduled_jobs(db, now=datetime(2026, 11, 4, 10, 7, tzinfo=UTC))
            self.assertEqual(db.scalar(select(Job.idempotency_key).where(Job.kind == "approvals.sweep")), "approvals.sweep:2026-11-04T10:05")

    # -- visibility and the approval center ----------------------------------------------------

    def test_visibility_follows_the_role_matrix(self) -> None:
        mine = self.submit("builder")
        other = self.submit("outsider", object_id=uuid4())
        def ids(name: str, scope: str) -> set[str]:
            response = self.client.get(f"/api/v1/approvals?scope={scope}", headers=self.login(name))
            self.assertEqual(response.status_code, 200, response.text)
            return {item["id"] for item in response.json()["items"]}
        self.assertEqual(ids("admin", "all"), {str(mine), str(other)})
        self.assertEqual(ids("auditor", "all"), {str(mine), str(other)})
        self.assertEqual(ids("builder", "all"), {str(mine)})
        self.assertEqual(ids("builder", "submitted"), {str(mine)})
        self.assertEqual(ids("viewer", "all"), set())
        self.assertEqual(ids("admin", "pending_mine"), {str(mine), str(other)})
        self.assertEqual(ids("builder", "pending_mine"), set())
        # The auditor reads everything but cannot decide.
        self.assertEqual(self.act("auditor", mine, "approve").status_code, 403)
        self.assertEqual(self.client.get(f"/api/v1/approvals/{other}", headers=self.login("builder")).status_code, 404)

    def test_list_filters_search_sort_and_summary(self) -> None:
        a = self.submit("builder")
        b = self.submit("outsider", object_id=uuid4())
        self.act("admin", b, "approve")
        headers = self.login("admin")
        listing = lambda query: self.client.get(f"/api/v1/approvals?scope=all&{query}", headers=headers).json()  # noqa: E731
        self.assertEqual([i["id"] for i in listing("status=pending")["items"]], [str(a)])
        self.assertEqual(listing("type=data_export")["total"], 0)
        self.assertEqual(listing("q=outsider")["total"], 1)
        self.assertEqual(listing("sort=newest")["items"][0]["id"], str(b))
        item = listing("status=approved")["items"][0]
        self.assertEqual((item["requester_name"], item["organization_name"], item["level_count"]), ("Outsider", "Acme", 1))
        summary = self.client.get("/api/v1/approvals/summary", headers=headers).json()
        self.assertEqual((summary["pending_mine"], summary["completed_this_month"]), (1, 1))
        self.assertIsNotNone(summary["avg_duration_seconds"])
        self.assertEqual(self.client.get("/api/v1/approvals/summary", headers=self.login("builder")).json()["submitted"], 1)

    def test_detail_shows_progress_comments_and_diff(self) -> None:
        self.set_policy(levels=2)
        approval_id = self.submit()
        self.act("admin", approval_id, "approve", {"comment": "level one ok"})
        commented = self.act("builder", approval_id, "comment", {"body": "补充说明"})
        self.assertEqual(commented.status_code, 200)
        detail = self.client.get(f"/api/v1/approvals/{approval_id}", headers=self.login("admin2")).json()
        self.assertEqual([lv["status"] for lv in detail["levels"]], ["approved", "pending"])
        self.assertEqual(detail["levels"][0]["decided_by"], "Admin")
        self.assertEqual([c["body"] for c in detail["comments"]], ["level one ok", "补充说明"])
        self.assertEqual((detail["change_diff"], detail["checks"][0]["label"], detail["can_decide"]), ([{"field": "prompt"}], "回归测试通过", True))
        self.assertEqual(self.act("builder", approval_id, "comment", {"body": ""}).status_code, 422)

    def test_cross_tenant_and_anonymous_access_are_refused(self) -> None:
        approval_id = self.submit()
        foreign = self.login("foreign", self.ids["other"])
        self.assertEqual(self.client.get(f"/api/v1/approvals/{approval_id}", headers=foreign).status_code, 404)
        self.assertEqual(self.client.post(f"/api/v1/approvals/{approval_id}/approve", json={}, headers=foreign).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/approvals", headers=foreign).json()["total"], 0)
        self.client.cookies.clear()
        self.assertEqual(self.client.get(f"/api/v1/approvals/{approval_id}").status_code, 401)

    def test_roles_carry_approver_tags(self) -> None:
        headers = self.login("admin")
        body = {"name": "KB owners", "scope": "tenant", "permissions": {"approvals": ["view"]}, "approver_tags": ["knowledge_owner"]}
        created = self.client.post("/api/v1/roles", json=body, headers=headers)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["approver_tags"], ["knowledge_owner"])
        updated = self.client.put(f"/api/v1/roles/{created.json()['id']}", json={**body, "approver_tags": []}, headers=headers)
        self.assertEqual(updated.json()["approver_tags"], [])
        invalid = self.client.post("/api/v1/roles", json={**body, "name": "Bad", "approver_tags": ["root"]}, headers=headers)
        self.assertEqual(invalid.status_code, 422)
        listed = {r["name"]: r for r in self.client.get("/api/v1/roles", headers=headers).json()["roles"]}
        self.assertEqual(listed["Security"]["approver_tags"], ["security_officer"])


if __name__ == "__main__":
    unittest.main()
