"""T-151: employee-side portal seat check — the server decides on every call; requests, auto-assignment, last sign-in."""

import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select

import test_assistants
from beework import entitlements, portal_seat_service, portal_service
from beework.main import app as fastapi_app
from beework.models import AuditEvent, Membership, Tenant, User
from beework.models.notifications import InAppNotification
from beework.models.portal_seats import PortalSeatAssignment

# Stand-in for the employee endpoints of T-143 / T-144 / T-145: they will mount exactly this dependency.
_probe = APIRouter()


@_probe.get("/api/v1/_probe/seat")
def _seat_probe(_: Membership = Depends(portal_service.require_portal_seat)) -> dict:
    return {"ok": True}


fastapi_app.include_router(_probe)

EXEMPT = {"/api/v1/portal/me/config", "/api/v1/portal/me/seat", "/api/v1/portal/me/seat/request"}


def uses_seat(dependant) -> bool:
    return any(dep.call is portal_service.require_portal_seat or uses_seat(dep) for dep in dependant.dependencies)


class PortalSeatCheckTests(test_assistants.AssistantTests):
    def setUp(self) -> None:
        super().setUp()
        self.tenant = UUID(self.ids["tenant"])
        with self.session_factory.begin() as db:
            for email, name in (("staff@example.com", "Staff"), ("staff2@example.com", "Staff Two")):
                account = User(email=email, display_name=name, password_hash=test_assistants.hash_password(test_assistants.PASSWORD))
                db.add(account)
                db.flush()
                db.add(Membership(tenant_id=self.tenant, user_id=account.id, organization_id=UUID(self.ids["root"]), role="employee"))
        self.grant("named", 5)
        self.admin = self.login("admin@example.com")

    # -- helpers -------------------------------------------------------------------------

    def grant(self, mode="named", seats=5, overage="hard") -> None:
        with self.session_factory.begin() as db:
            tenant = db.get(Tenant, self.tenant)
            entitlements.change_subscription(db, tenant, tenant.plan, portal_mode=mode, portal_seats=seats, portal_overage=overage)

    def uid(self, email: str) -> UUID:
        with self.session_factory() as db:
            return db.scalar(select(User.id).where(User.email == email))

    def seat(self, headers) -> dict:
        response = self.client.get("/api/v1/portal/me/seat", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def ask(self, headers, reason="需要使用"):
        return self.client.post("/api/v1/portal/me/seat/request", json={"reason": reason}, headers=headers)

    def staff(self, email="staff@example.com") -> dict:
        return self.login(email)

    def assign(self, *emails: str) -> None:
        admin = self.login("admin@example.com")
        ids = [str(self.uid(e)) for e in emails]
        self.assertEqual(self.client.post("/api/v1/portal/seats/assign", json={"user_ids": ids}, headers=admin).status_code, 200)

    def policy(self, auto_assign: bool, idle_days=None) -> None:
        with self.session_factory.begin() as db:
            portal_seat_service.update_policy(db, self.tenant, auto_assign, idle_days, None)

    # -- the server decides ----------------------------------------------------------------

    def test_without_a_seat_every_portal_use_is_refused_with_portal_seat_required(self) -> None:
        headers = self.staff()
        refused = self.client.get("/api/v1/_probe/seat", headers=headers)
        self.assertEqual((refused.status_code, refused.json()["code"]), (403, "portal.seat_required"))
        self.assert_visible_apps(0)
        self.assign("staff@example.com")
        headers = self.staff()
        self.assertEqual(self.client.get("/api/v1/_probe/seat", headers=headers).status_code, 200)
        self.assert_visible_apps(1)

    def assert_visible_apps(self, expected: int) -> None:
        """Without a seat the visibility function returns nothing at all; with one it is back to the T-140 rules."""
        with self.session_factory() as db:
            membership = db.scalar(select(Membership).join(User).where(User.email == "staff@example.com"))
            self.assertEqual(portal_seat_service.has_seat(db, membership), bool(expected))
            if not expected:
                self.assertEqual(portal_service.visible_apps(db, membership), [])

    def test_every_employee_endpoint_that_uses_the_portal_is_gated(self) -> None:
        routes = [ctx for included in fastapi_app.routes if hasattr(included, "effective_route_contexts") for ctx in included.effective_route_contexts()]
        employee = [ctx for ctx in routes if ctx.path.startswith("/api/v1/portal/me/")]
        self.assertGreaterEqual(len(employee), 3)
        ungated = sorted(ctx.path for ctx in employee if ctx.path not in EXEMPT and not uses_seat(ctx.dependant))
        self.assertEqual(ungated, [], "employee endpoints missing require_portal_seat")

    def test_unlimited_grants_never_need_a_seat(self) -> None:
        self.grant("unlimited")
        headers = self.staff()
        self.assertEqual(self.client.get("/api/v1/_probe/seat", headers=headers).status_code, 200)
        self.assertEqual((self.seat(headers)["state"], self.seat(headers)["source"]), ("assigned", "unlimited"))

    def test_a_department_rule_gives_access_and_recalling_takes_it_away_at_once(self) -> None:
        self.grant("named", 20)
        admin = self.login("admin@example.com")
        self.assertEqual(self.client.put("/api/v1/portal/seats/rules", json={"organization_ids": [self.ids["root"]]}, headers=admin).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/_probe/seat", headers=self.staff()).status_code, 200)
        admin = self.login("admin@example.com")
        self.client.post("/api/v1/portal/seats/revoke", json={"user_ids": [str(self.uid("staff@example.com"))]}, headers=admin)
        refused = self.client.get("/api/v1/_probe/seat", headers=self.staff())  # same account, next call
        self.assertEqual((refused.status_code, refused.json()["code"]), (403, "portal.seat_required"))

    # -- seat state and requests -----------------------------------------------------------------

    def test_the_seat_state_endpoint_reports_the_three_states(self) -> None:
        headers = self.staff()
        state = self.seat(headers)
        self.assertEqual((state["state"], state["can_request"], state["portal_name"]), ("unassigned", True, "Acme AI"))
        self.assertEqual(self.ask(headers).json()["state"], "requested")
        waiting = self.seat(headers)
        self.assertEqual((waiting["state"], waiting["can_request"]), ("requested", False))
        self.assign("staff@example.com")
        self.assertEqual((self.seat(self.staff())["state"], self.seat(self.staff())["source"]), ("assigned", "manual"))

    def test_requesting_is_idempotent_and_notifies_the_approvers_once(self) -> None:
        headers = self.staff()
        self.assertEqual(self.ask(headers, "做月报").status_code, 200)
        self.assertEqual(self.ask(headers, "再说一遍").json()["state"], "requested")
        with self.session_factory() as db:
            notices = db.scalars(select(InAppNotification).where(InAppNotification.type == "portal.seat_requested")).all()
            row = db.scalar(select(PortalSeatAssignment).where(PortalSeatAssignment.user_id == self.uid("staff@example.com")))
        self.assertEqual(row.request_reason, "做月报")  # the repeat changed nothing
        self.assertTrue(notices and {n.recipient_id for n in notices} == {self.uid("admin@example.com")})
        self.assertEqual(len(notices), 1)

    def test_a_seat_holder_asking_again_is_told_they_have_one(self) -> None:
        self.assign("staff@example.com")
        self.assertEqual(self.ask(self.staff()).json()["state"], "assigned")

    def test_reason_is_limited_to_200_characters(self) -> None:
        refused = self.ask(self.staff(), "字" * 201)
        self.assertEqual(refused.status_code, 422)

    def test_approve_and_reject_flow_through_to_the_employee(self) -> None:
        first, second = self.staff("staff@example.com"), None
        self.ask(first, "A")
        second = self.staff("staff2@example.com")
        self.ask(second, "B")
        admin = self.login("admin@example.com")
        self.client.post("/api/v1/portal/seats/approve", json={"user_ids": [str(self.uid("staff@example.com"))]}, headers=admin)
        self.client.post("/api/v1/portal/seats/reject", json={"user_ids": [str(self.uid("staff2@example.com"))]}, headers=admin)
        self.assertEqual(self.seat(self.staff("staff@example.com"))["state"], "assigned")
        rejected = self.seat(self.staff("staff2@example.com"))
        self.assertEqual((rejected["state"], rejected["can_request"]), ("unassigned", True))  # may apply again
        self.assertEqual(self.ask(self.staff("staff2@example.com"), "C").json()["state"], "requested")

    # -- auto-assignment -----------------------------------------------------------------------------

    def test_first_visit_auto_assigns_when_enabled(self) -> None:
        self.policy(True)
        state = self.seat(self.staff())
        self.assertEqual((state["state"], state["source"], state["auto_assigned"]), ("assigned", "auto", True))
        again = self.seat(self.staff())
        self.assertEqual((again["state"], again["auto_assigned"]), ("assigned", False))  # the notice shows only once

    def test_auto_assignment_does_not_happen_when_off_or_when_full(self) -> None:
        self.assertEqual(self.seat(self.staff())["state"], "unassigned")  # policy off
        self.grant("named", 1)
        self.assign("admin@example.com")
        self.policy(True)
        full = self.seat(self.staff("staff2@example.com"))
        self.assertEqual((full["state"], full["auto_assigned"], full["can_request"]), ("unassigned", False, True))
        # still no seat on the next visit: auto-assignment is for the first visit only
        self.grant("named", 5)
        self.assertEqual(self.seat(self.staff("staff2@example.com"))["state"], "unassigned")

    def test_a_recalled_or_rejected_member_is_not_auto_assigned_again(self) -> None:
        self.policy(True)
        self.assertTrue(self.seat(self.staff())["auto_assigned"])
        admin = self.login("admin@example.com")
        self.client.post("/api/v1/portal/seats/revoke", json={"user_ids": [str(self.uid("staff@example.com"))]}, headers=admin)
        self.assertEqual(self.seat(self.staff())["state"], "unassigned")

    # -- last sign-in -----------------------------------------------------------------------------------

    def test_entering_the_portal_records_the_sign_in_time_for_idle_reclaim(self) -> None:
        self.assign("staff@example.com")
        with self.session_factory() as db:
            row = db.scalar(select(PortalSeatAssignment).where(PortalSeatAssignment.user_id == self.uid("staff@example.com")))
            self.assertIsNone(row.last_login_at)
        self.seat(self.staff())
        with self.session_factory() as db:
            row = db.scalar(select(PortalSeatAssignment).where(PortalSeatAssignment.user_id == self.uid("staff@example.com")))
            stamp = row.last_login_at
        self.assertIsNotNone(stamp)
        self.assertLess(datetime.now(UTC) - stamp.replace(tzinfo=UTC), timedelta(minutes=1))
        self.policy(False, 30)
        with self.session_factory() as db:
            self.assertNotIn(self.uid("staff@example.com"), portal_seat_service.idle_user_ids(db, self.tenant))

    def test_a_visit_without_a_seat_records_nothing(self) -> None:
        self.seat(self.staff())
        with self.session_factory() as db:
            row = db.scalar(select(PortalSeatAssignment).where(PortalSeatAssignment.user_id == self.uid("staff@example.com")))
        self.assertTrue(row is None or row.last_login_at is None)

    # -- isolation and misc ---------------------------------------------------------------------------------

    def test_another_tenants_member_cannot_use_or_ask_in_this_tenant(self) -> None:
        other = self.login("other@example.com", self.ids["other"])
        crossing = {**other, "X-Tenant-ID": str(self.tenant)}
        self.assertEqual(self.client.get("/api/v1/portal/me/seat", headers=crossing).status_code, 403)
        self.assertEqual(self.ask(crossing).status_code, 403)
        self.assertEqual(self.client.get("/api/v1/_probe/seat", headers=crossing).status_code, 403)

    def test_a_request_needs_csrf_and_a_session(self) -> None:
        headers = self.staff()
        no_csrf = {k: v for k, v in headers.items() if k != "X-CSRF-Token"}
        self.assertEqual(self.client.post("/api/v1/portal/me/seat/request", json={}, headers=no_csrf).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/v1/portal/me/seat", headers=headers).status_code, 401)

    def test_requests_are_recorded_for_the_applicant_only(self) -> None:
        self.ask(self.staff("staff@example.com"))
        second = self.seat(self.staff("staff2@example.com"))
        self.assertEqual(second["state"], "unassigned")
        with self.session_factory() as db:
            self.assertEqual(len(db.scalars(select(AuditEvent).where(AuditEvent.action == "portal_seat.assigned")).all()), 0)


for _name in [n for n in dir(test_assistants.AssistantTests) if n.startswith("test_")]:
    setattr(PortalSeatCheckTests, _name, None)

if __name__ == "__main__":
    unittest.main()
