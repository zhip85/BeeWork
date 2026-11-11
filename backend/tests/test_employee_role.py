"""T-142: the portal-only ``employee`` role — no console seat, no console permission, portal config only."""

import unittest
from uuid import UUID, uuid4

from sqlalchemy import select

import test_assistants
from beework import auth, rbac
from beework.entitlements import get_subscription, seat_usage
from beework.main import app
from beework.models import Membership, User

TENANT_DEPENDENCIES = {auth.get_tenant_membership, auth.get_tenant_membership_for_own_state}
# What an employee may reach: the portal's own surface and their own session / notifications.
EMPLOYEE_PREFIXES = ("/api/v1/portal/me/", "/api/v1/notifications", "/api/v1/_probe")  # _probe: routes other test modules add


def depends_on_tenant(dependant) -> bool:
    return any(dep.call in TENANT_DEPENDENCIES or depends_on_tenant(dep) for dep in dependant.dependencies)


class EmployeeTests(test_assistants.AssistantTests):
    def setUp(self) -> None:
        super().setUp()
        with self.session_factory.begin() as db:
            staff = User(email="staff@example.com", display_name="Staff", password_hash=test_assistants.hash_password(test_assistants.PASSWORD))
            db.add(staff)
            db.flush()
            db.add(Membership(tenant_id=UUID(self.ids["tenant"]), user_id=staff.id, organization_id=UUID(self.ids["root"]), role="employee"))
            self.staff_id = staff.id
        self.admin = self.login("admin@example.com")

    def seats(self) -> tuple[int, int]:
        with self.session_factory() as db:
            tenant = UUID(self.ids["tenant"])
            return seat_usage(db, tenant), int(get_subscription(db, tenant).quotas["seats"])

    def fill_seats(self) -> None:
        """Make the console seats exactly full."""
        used, limit = self.seats()
        with self.session_factory.begin() as db:
            tenant = UUID(self.ids["tenant"])
            for index in range(limit - used):
                user = User(email=f"filler{index}@example.com", display_name=f"Filler {index}", password_hash="x")
                db.add(user)
                db.flush()
                db.add(Membership(tenant_id=tenant, user_id=user.id, organization_id=UUID(self.ids["root"]), role="viewer"))
        self.assertEqual(self.seats()[0], self.seats()[1])

    # -- the role ---------------------------------------------------------------------

    def test_the_role_has_a_label_and_no_permissions(self) -> None:
        self.assertEqual(rbac.ROLE_LABELS["employee"], "员工")
        self.assertEqual(rbac.BUILTIN_PERMISSIONS["employee"], frozenset())
        roles = self.client.get("/api/v1/roles", headers=self.admin).json()["roles"]
        employee = next(r for r in roles if r.get("key") == "employee")
        self.assertEqual((employee["name"], employee["permissions"], employee["is_builtin"], employee["member_count"]), ("员工", {}, True, 1))

    def test_an_employee_signs_in_with_no_permissions(self) -> None:
        headers = self.login("staff@example.com")
        me = self.client.get("/api/v1/auth/me", headers=headers).json()
        membership = next(m for m in me["memberships"] if m["id"] == self.ids["tenant"])
        self.assertEqual((membership["role"], membership["permissions"]), ("employee", []))

    # -- seats -------------------------------------------------------------------------

    def test_employees_do_not_use_a_console_seat(self) -> None:
        used, _limit = self.seats()
        with self.session_factory() as db:
            members = db.scalars(select(Membership).where(Membership.tenant_id == UUID(self.ids["tenant"]), Membership.status == "active")).all()
        self.assertEqual(used, len(members) - 1)  # everybody but the employee
        with self.session_factory.begin() as db:
            extra = User(email="staff2@example.com", display_name="Staff 2", password_hash="x")
            db.add(extra)
            db.flush()
            db.add(Membership(tenant_id=UUID(self.ids["tenant"]), user_id=extra.id, organization_id=UUID(self.ids["root"]), role="employee"))
        self.assertEqual(self.seats()[0], used)

    def invite(self, role: str, *emails: str, headers=None):
        return self.client.post("/api/v1/members/invitations", json={"emails": list(emails), "role": role, "organization_id": self.ids["root"]}, headers=headers or self.admin)

    def test_invitations_for_employees_ignore_the_seat_limit_and_do_not_count(self) -> None:
        self.fill_seats()
        blocked = self.invite("builder", "late@example.com")
        self.assertEqual((blocked.status_code, blocked.json()["code"]), (409, "plan.quota_exceeded"))
        allowed = self.invite("employee", "new-employee@example.com", "another@example.com")
        self.assertEqual(allowed.status_code, 201, allowed.text)
        self.assertEqual(self.seats()[0], self.seats()[1])  # still exactly full: pending employee invitations count for nothing

    def test_accepting_an_employee_invitation_works_with_full_seats(self) -> None:
        self.fill_seats()
        created = self.invite("employee", "joiner@example.com").json()
        token = created[0]["invitation_url"].split("token=")[1] if isinstance(created, list) else created["invitations"][0]["invitation_url"].split("token=")[1]
        self.client.cookies.clear()
        accepted = self.client.post("/api/v1/members/invitations/accept", json={"token": token, "display_name": "Joiner", "password": "a very long password 123"})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        with self.session_factory() as db:
            joined = db.scalar(select(Membership).join(User).where(User.email == "joiner@example.com"))
        self.assertEqual(joined.role, "employee")

    def role_change(self, user_id, role: str, headers=None):
        return self.client.patch(f"/api/v1/members/{user_id}/role", json={"role": role}, headers=headers or self.admin)

    def test_turning_an_employee_into_a_console_user_takes_a_seat_and_must_fit(self) -> None:
        self.fill_seats()
        full = self.role_change(self.staff_id, "builder")
        self.assertEqual((full.status_code, full.json()["code"]), (409, "plan.quota_exceeded"))
        with self.session_factory() as db:
            self.assertEqual(db.scalar(select(Membership.role).where(Membership.user_id == self.staff_id)), "employee")
        with self.session_factory.begin() as db:  # free one seat
            filler = db.scalar(select(User).where(User.email == "filler0@example.com"))
            db.scalar(select(Membership).where(Membership.user_id == filler.id)).status = "suspended"
        self.assertEqual(self.role_change(self.staff_id, "builder").status_code, 200)
        self.assertEqual(self.seats()[0], self.seats()[1])

    def test_a_console_user_can_become_an_employee_and_frees_the_seat(self) -> None:
        with self.session_factory() as db:
            viewer = db.scalar(select(User).where(User.email == "viewer@example.com"))
        before = self.seats()[0]
        self.assertEqual(self.role_change(viewer.id, "employee").status_code, 200)
        self.assertEqual(self.seats()[0], before - 1)

    def test_restoring_a_suspended_employee_needs_no_seat(self) -> None:
        self.fill_seats()
        suspend = self.client.patch(f"/api/v1/members/{self.staff_id}/status", json={"status": "suspended"}, headers=self.admin)
        self.assertEqual(suspend.status_code, 200, suspend.text)
        restore = self.client.patch(f"/api/v1/members/{self.staff_id}/status", json={"status": "active"}, headers=self.admin)
        self.assertEqual(restore.status_code, 200, restore.text)

    def test_the_tenant_member_list_and_usage_report_console_seats_only(self) -> None:
        listing = self.client.get("/api/v1/members", headers=self.admin).json()
        used, limit = self.seats()
        self.assertEqual((listing["seats_used"], listing["seat_limit"]), (used, limit))
        self.assertIn("employee", {m["role"] for m in listing["members"]})

    # -- permissions ---------------------------------------------------------------------

    def test_every_console_endpoint_refuses_an_employee(self) -> None:
        headers = self.login("staff@example.com")
        refused, reachable = 0, []
        routes = [ctx for included in app.routes if hasattr(included, "effective_route_contexts") for ctx in included.effective_route_contexts()]
        self.assertGreater(len(routes), 200)
        for route in routes:
            if not depends_on_tenant(route.dependant):
                continue
            path = route.path
            for name in route.dependant.path_params:
                path = path.replace("{" + name.name + "}", str(uuid4()))
            for method in route.methods - {"HEAD", "OPTIONS"}:
                response = self.client.request(method, path, headers=headers, json={} if method not in ("GET", "DELETE") else None)
                if path.startswith(EMPLOYEE_PREFIXES):
                    continue
                if response.status_code == 403:
                    refused += 1
                else:
                    reachable.append((method, route.path, response.status_code))
        self.assertGreater(refused, 150)
        self.assertEqual(reachable, [], "console endpoints an employee can reach")

    def test_the_portal_config_is_open_to_an_employee(self) -> None:
        headers = self.login("staff@example.com")
        config = self.client.get("/api/v1/portal/me/config", headers=headers)
        self.assertEqual(config.status_code, 200, config.text)
        self.assertEqual((config.json()["has_console"], config.json()["portal_name"]), (False, "Acme AI"))

    def test_the_employee_cannot_reach_another_tenants_portal(self) -> None:
        headers = {**self.login("staff@example.com"), "X-Tenant-ID": self.ids["other"]}
        self.assertEqual(self.client.get("/api/v1/portal/me/config", headers=headers).status_code, 403)


for _name in [n for n in dir(test_assistants.AssistantTests) if n.startswith("test_")]:
    setattr(EmployeeTests, _name, None)  # inherited fixtures only; the assistant tests run in their own module


if __name__ == "__main__":
    unittest.main()
