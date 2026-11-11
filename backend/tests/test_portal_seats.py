"""T-150: AI portal seats — capacity, rules, exclusions, requests, idle reclaim, audit, permissions, isolation."""

import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select

import test_assistants
from beework import entitlements, portal_seat_service
from beework.models import AuditEvent, Membership, Tenant, User
from beework.models.notifications import InAppNotification
from beework.models.portal_seats import PortalSeatAssignment


class PortalSeatTests(test_assistants.AssistantTests):
    def setUp(self) -> None:
        super().setUp()
        self.tenant = UUID(self.ids["tenant"])
        self.admin = self.login("admin@example.com")

    # -- helpers --------------------------------------------------------------------------

    def grant(self, mode="named", seats=10, overage="hard") -> None:
        with self.session_factory.begin() as db:
            tenant = db.get(Tenant, self.tenant)
            entitlements.change_subscription(db, tenant, tenant.plan, portal_mode=mode, portal_seats=seats, portal_overage=overage)

    def uid(self, email: str) -> UUID:
        with self.session_factory() as db:
            return db.scalar(select(User.id).where(User.email == email))

    def employees(self, count: int, org: str = "root") -> list[UUID]:
        ids = []
        with self.session_factory.begin() as db:
            start = len(db.scalars(select(User).where(User.email.like("emp%"))).all())
            for index in range(start, start + count):
                user = User(email=f"emp{index}@example.com", display_name=f"Emp {index:02d}", password_hash="x")
                db.add(user)
                db.flush()
                db.add(Membership(tenant_id=self.tenant, user_id=user.id, organization_id=UUID(self.ids[org]), role="employee"))
                ids.append(user.id)
        return ids

    def post(self, path: str, body: dict | None = None, headers=None):
        return self.client.post(f"/api/v1/portal/seats/{path}", json=body or {}, headers=headers or self.admin)

    def assign(self, *user_ids, headers=None):
        return self.post("assign", {"user_ids": [str(u) for u in user_ids]}, headers)

    def summary(self, headers=None) -> dict:
        return self.client.get("/api/v1/portal/seats/summary", headers=headers or self.admin).json()

    def set_rules(self, *org_keys: str):
        return self.client.put("/api/v1/portal/seats/rules", json={"organization_ids": [self.ids[k] for k in org_keys]}, headers=self.admin)

    def request_seat(self, user_id: UUID, reason: str = "需要使用") -> None:
        with self.session_factory.begin() as db:
            row = PortalSeatAssignment(tenant_id=self.tenant, user_id=user_id, status="requested", excluded=False, request_reason=reason)
            db.add(row)

    def members(self, **params) -> dict:
        return self.client.get("/api/v1/portal/seats/members", params=params, headers=self.admin).json()

    def actions(self, action: str) -> list[AuditEvent]:
        with self.session_factory() as db:
            return list(db.scalars(select(AuditEvent).where(AuditEvent.action == action)))

    # -- capacity ---------------------------------------------------------------------------

    def test_hard_limit_refuses_the_whole_batch_and_changes_nothing(self) -> None:
        self.grant("named", 3)
        staff = self.employees(5)
        self.assertEqual(self.assign(*staff[:2]).status_code, 200)
        refused = self.assign(*staff[2:5])  # 2 + 3 > 3
        self.assertEqual((refused.status_code, refused.json()["code"]), (409, "portal.seats_exhausted"))
        self.assertIn("最多还能分配 1 个", refused.json()["detail"])
        self.assertEqual(self.summary()["assigned"], 2)  # nothing from the refused batch was applied
        self.assertEqual(self.assign(staff[2]).status_code, 200)
        exhausted = self.assign(staff[3])
        self.assertIn("合同席位已用尽", exhausted.json()["detail"])

    def test_grace_allows_beyond_the_contract_with_a_notice_then_refuses(self) -> None:
        self.grant("named", 10, "grace_10")  # contract 10, assignable 11
        staff = self.employees(12)
        self.assertIsNone(self.assign(*staff[:10]).json()["notice"])
        over = self.assign(staff[10])
        self.assertEqual((over.status_code, over.json()["notice"]), (200, "当前已超出合同 10 席位，处于宽限期"))
        self.assertTrue(over.json()["summary"]["over_contract"])
        self.assertEqual(self.assign(staff[11]).status_code, 409)

    def test_unlimited_never_blocks(self) -> None:
        self.grant("unlimited")
        staff = self.employees(30)
        self.assertEqual(self.assign(*staff).status_code, 200)
        summary = self.summary()
        self.assertEqual((summary["assigned"], summary["remaining"], summary["grant"]["mode"]), (30, None, "unlimited"))

    def test_rules_manual_and_exclusions_are_counted_without_duplicates(self) -> None:
        self.grant("named", 20)
        sales = self.employees(2, "sales")
        self.assertEqual(self.set_rules("sales").status_code, 200)
        sales_user = self.uid("sales@example.com")
        self.assertEqual(self.summary()["assigned"], 3)  # SalesUser + two sales employees
        self.assertEqual(self.assign(sales_user, *sales).json()["changed"], 0)  # already hold one through the rule
        outsider = self.employees(1)[0]
        self.assign(outsider)
        self.assertEqual(self.summary()["assigned"], 4)
        # recalling inside a rule department excludes the member
        self.assertEqual(self.post("revoke", {"user_ids": [str(sales[0])]}).json()["summary"]["assigned"], 3)
        # re-saving the rule does not hand the seat back
        self.set_rules("sales")
        self.assertEqual(self.summary()["assigned"], 3)
        # closing the rule releases rule seats only; the manual one stays
        closed = self.set_rules()
        self.assertEqual(closed.json()["summary"]["assigned"], 1)
        # assigning an excluded member clears the exclusion
        self.assertEqual(self.assign(sales[0]).status_code, 200)
        self.assertEqual(self.summary()["assigned"], 2)

    def test_a_rule_that_would_exceed_the_limit_is_refused_whole(self) -> None:
        self.grant("named", 3)
        self.employees(5, "sales")
        refused = self.set_rules("sales")
        self.assertEqual((refused.status_code, refused.json()["code"]), (409, "portal.seats_exhausted"))
        rules = self.client.get("/api/v1/portal/seats/rules", headers=self.admin).json()["organizations"]
        self.assertFalse(any(org["enabled"] for org in rules))
        sales_rule = next(org for org in rules if org["name"] == "Sales")
        self.assertEqual(sales_rule["member_count"], 6)  # five employees plus SalesUser

    def test_suspended_members_hold_no_seat(self) -> None:
        self.grant("named", 5)
        staff = self.employees(2)
        self.assign(*staff)
        with self.session_factory.begin() as db:
            db.scalar(select(Membership).where(Membership.user_id == staff[0])).status = "suspended"
        self.assertEqual(self.summary()["assigned"], 1)

    # -- requests -------------------------------------------------------------------------

    def test_approving_a_request_assigns_and_notifies_the_applicant(self) -> None:
        self.grant("named", 5)
        applicant = self.employees(1)[0]
        self.request_seat(applicant, "做销售日报")
        self.assertEqual(self.summary()["requested"], 1)
        row = self.members(filter="requested")["items"][0]
        self.assertEqual((row["state"], row["reason"]), ("requested", "做销售日报"))
        approved = self.post("approve", {"user_ids": [str(applicant)]})
        self.assertEqual((approved.status_code, approved.json()["summary"]["assigned"], approved.json()["summary"]["requested"]), (200, 1, 0))
        with self.session_factory() as db:
            notice = db.scalar(select(InAppNotification).where(InAppNotification.type == "portal.seat_approved"))
        self.assertEqual(notice.recipient_id, applicant)
        self.assertTrue(self.actions("portal_seat.approved"))

    def test_rejecting_clears_the_reason_and_notifies(self) -> None:
        self.grant("named", 5)
        applicant = self.employees(1)[0]
        self.request_seat(applicant)
        self.assertEqual(self.post("reject", {"user_ids": [str(applicant)]}).json()["changed"], 1)
        item = next(m for m in self.members()["items"] if m["user_id"] == str(applicant))
        self.assertEqual((item["state"], item["reason"]), ("unassigned", None))
        with self.session_factory() as db:
            self.assertEqual(db.scalar(select(InAppNotification).where(InAppNotification.type == "portal.seat_rejected")).recipient_id, applicant)

    def test_approval_is_bound_by_capacity_and_needs_a_request(self) -> None:
        self.grant("named", 1)
        first, second = self.employees(2)
        self.assign(first)
        self.request_seat(second)
        refused = self.post("approve", {"user_ids": [str(second)]})
        self.assertEqual((refused.status_code, refused.json()["code"]), (409, "portal.seats_exhausted"))
        self.assertEqual(self.summary()["requested"], 1)  # still waiting
        none = self.post("approve", {"user_ids": [str(first)]})
        self.assertEqual((none.status_code, none.json()["code"]), (409, "portal.no_request"))

    # -- idle reclaim -----------------------------------------------------------------------

    def old_login(self, user_id: UUID, days: int) -> None:
        with self.session_factory.begin() as db:
            row = db.scalar(select(PortalSeatAssignment).where(PortalSeatAssignment.user_id == user_id))
            if row is None:
                row = PortalSeatAssignment(tenant_id=self.tenant, user_id=user_id, status="none", excluded=False)
                db.add(row)
            row.last_login_at = datetime.now(UTC) - timedelta(days=days)

    def test_idle_seats_are_reclaimed_by_the_policy(self) -> None:
        self.grant("named", 10)
        active, idle_manual = self.employees(2)
        idle_rule = self.employees(1, "sales")[0]
        self.assign(active, idle_manual)
        self.set_rules("sales")
        self.old_login(active, 1)
        self.old_login(idle_manual, 45)
        self.old_login(idle_rule, 45)
        self.assertEqual(self.summary()["idle"], 0)  # policy is “不回收” until set
        self.assertEqual(self.post("reclaim-idle").json()["reclaimed"], 0)
        self.assertEqual(self.client.put("/api/v1/portal/seats/policy", json={"auto_assign": False, "idle_days": 30}, headers=self.admin).status_code, 200)
        self.assertEqual(self.summary()["idle"], 2)
        self.assertEqual({m["display_name"] for m in self.members(filter="idle")["items"]}, {"Emp 01", "Emp 02"})
        reclaimed = self.post("reclaim-idle").json()
        self.assertEqual((reclaimed["reclaimed"], reclaimed["summary"]["idle"]), (2, 0))
        states = {m["user_id"]: m["state"] for m in self.members()["items"]}
        self.assertEqual((states[str(idle_manual)], states[str(idle_rule)], states[str(active)]), ("unassigned", "unassigned", "assigned"))
        self.assertTrue(self.actions("portal_seat.reclaimed"))

    def test_the_daily_job_applies_each_tenants_policy(self) -> None:
        self.grant("named", 10)
        stale, fresh = self.employees(2)
        self.assign(stale, fresh)
        self.old_login(stale, 100)
        self.old_login(fresh, 2)
        with self.session_factory.begin() as db:
            portal_seat_service.update_policy(db, self.tenant, False, 90, None)
        with self.session_factory() as db:
            portal_seat_service.reclaim_idle_for_all_tenants(db)
        self.assertEqual(self.summary()["assigned"], 1)

    def test_policy_values_are_validated_and_audited(self) -> None:
        bad = self.client.put("/api/v1/portal/seats/policy", json={"auto_assign": True, "idle_days": 45}, headers=self.admin)
        self.assertEqual(bad.status_code, 422)
        good = self.client.put("/api/v1/portal/seats/policy", json={"auto_assign": True, "idle_days": 60}, headers=self.admin)
        self.assertEqual(good.json(), {"auto_assign": True, "idle_days": 60})
        self.assertEqual(self.client.get("/api/v1/portal/seats/policy", headers=self.admin).json()["idle_days"], 60)
        self.assertTrue(self.actions("portal_seat_policy.updated"))

    def test_auto_assignment_follows_the_policy_and_capacity(self) -> None:
        self.grant("named", 1)
        first, second = self.employees(2)
        with self.session_factory.begin() as db:
            portal_seat_service.update_policy(db, self.tenant, True, None, None)
        with self.session_factory.begin() as db:
            membership = db.scalar(select(Membership).where(Membership.user_id == first))
            self.assertTrue(portal_seat_service.try_auto_assign(db, membership))
        with self.session_factory.begin() as db:
            membership = db.scalar(select(Membership).where(Membership.user_id == second))
            self.assertFalse(portal_seat_service.try_auto_assign(db, membership))  # full: no assignment, no error
        source = next(m for m in self.members()["items"] if m["user_id"] == str(first))["source"]
        self.assertEqual(source, "auto")

    # -- listing, audit, notices --------------------------------------------------------------

    def test_member_list_filters_searches_and_pages(self) -> None:
        self.grant("named", 20)
        staff = self.employees(5)
        self.assign(*staff[:2])
        everyone = self.members(page_size=100)
        self.assertEqual(everyone["total"], 4 + 5)  # admin, builder, viewer, SalesUser + employees
        self.assertEqual(self.members(filter="assigned")["total"], 2)
        self.assertEqual(self.members(filter="unassigned")["total"], 7)
        self.assertEqual([m["email"] for m in self.members(q="emp3")["items"]], ["emp3@example.com"])
        page = self.members(page=2, page_size=4)
        self.assertEqual((len(page["items"]), page["total"]), (4, 9))
        self.assertEqual(self.client.get("/api/v1/portal/seats/members", params={"filter": "bogus"}, headers=self.admin).status_code, 422)

    def test_summary_reports_console_seats_separately(self) -> None:
        self.grant("named", 20)
        self.employees(3)
        summary = self.summary()
        self.assertEqual((summary["console_seats_used"], summary["console_seats_limit"]), (4, 20))  # employees use no console seat
        self.assertEqual((summary["grant"]["seats"], summary["assigned"], summary["remaining"]), (20, 0, 20))

    def test_every_change_is_audited_and_ninety_percent_notifies_the_admins(self) -> None:
        self.grant("named", 10)
        staff = self.employees(9)
        self.assign(*staff)  # 9 of 10: crosses 90%
        self.post("revoke", {"user_ids": [str(staff[0])]})
        self.set_rules("sales")
        self.assertTrue(self.actions("portal_seat.assigned") and self.actions("portal_seat.revoked") and self.actions("portal_seat_rule.updated"))
        with self.session_factory() as db:
            notices = db.scalars(select(InAppNotification).where(InAppNotification.type == "portal.seats_near_limit")).all()
        self.assertTrue(notices and all(n.recipient_id == self.uid("admin@example.com") for n in notices))

    def test_the_usage_counter_is_registered(self) -> None:
        self.grant("named", 5)
        self.assign(*self.employees(2))
        quotas = self.client.get("/api/v1/tenant/subscription", headers=self.admin).json()["quotas"]
        portal = next(q for q in quotas if q["key"] == "portal_seats")
        self.assertEqual((portal["limit"], portal["used"]), (5, 2))

    # -- permissions and isolation ------------------------------------------------------------

    def test_permissions_are_checked_on_the_server(self) -> None:
        self.grant("named", 5)
        staff = self.employees(1)[0]
        builder = self.login("builder@example.com")  # portal:view + portal:edit, no publish
        self.assertEqual(self.client.get("/api/v1/portal/seats/summary", headers=builder).status_code, 200)
        self.assertEqual(self.assign(staff, headers=builder).status_code, 403)
        self.assertEqual(self.post("revoke", {"user_ids": [str(staff)]}, builder).status_code, 403)
        self.assertEqual(self.post("approve", {"user_ids": [str(staff)]}, builder).status_code, 403)
        self.assertEqual(self.post("reclaim-idle", None, builder).status_code, 403)
        self.assertEqual(self.client.put("/api/v1/portal/seats/policy", json={"auto_assign": True, "idle_days": 30}, headers=builder).status_code, 200)
        viewer = self.login("viewer@example.com")
        self.assertEqual(self.client.put("/api/v1/portal/seats/rules", json={"organization_ids": []}, headers=viewer).status_code, 403)
        self.assertEqual(self.client.put("/api/v1/portal/seats/policy", json={"auto_assign": True, "idle_days": 30}, headers=viewer).status_code, 403)

    def test_validation_errors(self) -> None:
        self.grant("named", 5)
        self.assertEqual(self.post("assign", {"user_ids": []}).status_code, 422)
        self.assertEqual(self.post("assign", {"user_ids": ["not-a-uuid"]}).status_code, 422)
        self.assertEqual(self.assign(UUID(int=7)).status_code, 404)
        self.assertEqual(self.client.put("/api/v1/portal/seats/rules", json={"organization_ids": [str(UUID(int=9))]}, headers=self.admin).status_code, 404)

    def test_another_tenants_members_and_departments_look_missing(self) -> None:
        self.grant("named", 5)
        staff = self.employees(1)[0]
        with self.session_factory.begin() as db:
            other = UUID(self.ids["other"])
            from beework.models import Organization

            other_org = db.scalar(select(Organization.id).where(Organization.tenant_id == other))
        other_admin = self.login("other@example.com", self.ids["other"])
        self.assertEqual(self.assign(staff, headers=other_admin).status_code, 404)
        self.admin = self.login("admin@example.com")
        self.assertEqual(self.client.put("/api/v1/portal/seats/rules", json={"organization_ids": [str(other_org)]}, headers=self.admin).status_code, 404)
        self.assertEqual(self.summary(self.login("other@example.com", self.ids["other"]))["assigned"], 0)
        # a tenant admin cannot act inside a tenant they do not belong to
        crossing = {**self.login("admin@example.com"), "X-Tenant-ID": self.ids["other"]}
        self.assertEqual(self.client.get("/api/v1/portal/seats/summary", headers=crossing).status_code, 403)
        self.assertEqual(self.summary(self.login("admin@example.com"))["assigned"], 0)

    def test_employees_cannot_manage_seats(self) -> None:
        with self.session_factory.begin() as db:
            from beework.security import hash_password

            account = User(email="worker@example.com", display_name="Worker", password_hash=hash_password(test_assistants.PASSWORD))
            db.add(account)
            db.flush()
            db.add(Membership(tenant_id=self.tenant, user_id=account.id, organization_id=UUID(self.ids["root"]), role="employee"))
        worker = self.login("worker@example.com")
        self.assertEqual(self.client.get("/api/v1/portal/seats/summary", headers=worker).status_code, 403)
        self.assertEqual(self.assign(account.id, headers=worker).status_code, 403)


for _name in [n for n in dir(test_assistants.AssistantTests) if n.startswith("test_")]:
    setattr(PortalSeatTests, _name, None)

if __name__ == "__main__":
    unittest.main()
