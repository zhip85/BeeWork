"""T-149: the platform grants AI portal seats per contract — modes, plan defaults, audit, read-only tenant side."""

import importlib.util
import unittest
from pathlib import Path

from sqlalchemy import select

import test_plans
from beework import entitlements
from beework.models import AuditEvent, Membership, Organization, Tenant, User
from beework.models.plans import TenantSubscription
from beework.security import hash_password

PASSWORD = test_plans.PASSWORD


class PortalEntitlementTests(test_plans.PlanManagementTests):
    def setUp(self) -> None:
        super().setUp()
        self.platform = self.login("platform@example.com")

    def subscription(self, headers=None) -> dict:
        response = self.client.get(f"/api/v1/platform/tenants/{self.tenant_id}/subscription", headers=headers or self.platform)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def change(self, **body):
        return self.client.post(
            f"/api/v1/platform/tenants/{self.tenant_id}/subscription", json={"plan_code": "standard", **body}, headers=self.platform
        )

    # -- modes and validation -------------------------------------------------------------

    def test_plan_defaults_are_carried_into_the_snapshot(self) -> None:
        self.assertEqual(self.subscription()["portal"], {"mode": "named", "seats": 200, "overage": "hard", "assignable_limit": 200})
        professional = self.change(plan_code="professional")
        self.assertEqual(professional.json()["portal"], {"mode": "named", "seats": 1000, "overage": "grace_5", "assignable_limit": 1050})
        enterprise = self.change(plan_code="enterprise")
        self.assertEqual(enterprise.json()["portal"], {"mode": "unlimited", "seats": None, "overage": None, "assignable_limit": None})

    def test_the_three_modes_are_validated(self) -> None:
        self.assertEqual(self.change(portal_mode="unlimited", portal_seats=500).json()["portal"]["seats"], None)  # no number when unlimited
        for body in ({"portal_mode": "named", "portal_seats": 0}, {"portal_mode": "other"}, {"portal_overage": "grace_50"}):
            self.assertEqual(self.change(**body).status_code, 422, body)
        # named / active need a seat count: switching from unlimited without one is refused
        self.change(plan_code="enterprise")
        refused = self.client.post(
            f"/api/v1/platform/tenants/{self.tenant_id}/subscription", json={"plan_code": "enterprise", "portal_mode": "active"}, headers=self.platform
        )
        self.assertEqual((refused.status_code, refused.json()["code"]), (422, "portal.grant_invalid"))
        active = self.change(portal_mode="active", portal_seats=1800, portal_overage="grace_10")
        self.assertEqual(active.json()["portal"], {"mode": "active", "seats": 1800, "overage": "grace_10", "assignable_limit": 1980})

    def test_a_contract_change_makes_a_new_snapshot_and_audits_before_and_after(self) -> None:
        self.change(portal_mode="named", portal_seats=1500, portal_overage="hard")
        changed = self.change(portal_mode="active", portal_seats=1800)
        self.assertEqual(changed.status_code, 200, changed.text)
        with self.session_factory() as db:
            snapshots = db.scalars(select(TenantSubscription).where(TenantSubscription.tenant_id == self.tenant_id)).all()
            self.assertGreaterEqual(len(snapshots), 3)
            self.assertEqual(sum(1 for s in snapshots if s.is_current), 1)
            events = db.scalars(select(AuditEvent).where(AuditEvent.action == "tenant.subscription_changed")).all()
        notes = {e.details.get("portal") for e in events}
        self.assertIn("指定用户 · 1,500 · 硬限制 → 按月活跃 · 1,800 · 硬限制", notes)
        self.assertEqual({e.tenant_id is None for e in events if e.details.get("portal")}, {True, False})  # platform and tenant chains

    def test_reapplying_the_plan_keeps_the_negotiated_grant(self) -> None:
        self.change(portal_mode="named", portal_seats=1500)
        self.assertEqual(self.change().json()["portal"]["seats"], 1500)

    def test_a_historical_snapshot_without_portal_fields_reads_its_plan_defaults(self) -> None:
        with self.session_factory.begin() as db:
            snapshot = db.scalar(select(TenantSubscription).where(TenantSubscription.tenant_id == self.tenant_id))
            snapshot.entitlements = {k: v for k, v in snapshot.entitlements.items() if not k.startswith("portal_")}
            snapshot.quotas = {k: v for k, v in snapshot.quotas.items() if k != "portal_seats"}
        self.assertEqual(self.subscription()["portal"]["seats"], 200)
        # the migration's backfill produces the same values
        path = Path(__file__).parent.parent / "migrations" / "versions" / "0045_portal_seat_grant.py"
        spec = importlib.util.spec_from_file_location("m0045", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        entitlements_, quotas, policy = module._fill({"mcp": True}, {"seats": 5}, {"seats": "hard"}, "professional")
        self.assertEqual((entitlements_["portal_mode"], entitlements_["portal_overage"], quotas["portal_seats"], policy["portal_seats"]), ("named", "grace_5", 1000, "soft"))

    def test_the_portal_quota_policy_follows_the_overage_setting(self) -> None:
        with self.session_factory() as db:
            version = entitlements.latest_version(db, db.scalar(select(entitlements.Plan).where(entitlements.Plan.code == "professional")))
            self.assertEqual((version.quotas["portal_seats"], version.overage_policy["portal_seats"]), (1000, "soft"))
        self.change(portal_seats=300, portal_overage="hard")
        self.assertEqual(self.client.get("/api/v1/tenant/subscription", headers=self.login("admin@example.com")).json()["quotas"][-1]["policy"], "hard")

    # -- create / update tenant ----------------------------------------------------------------

    def tenant_body(self, **extra) -> dict:
        return {
            "name": "Beta Corp", "slug": "beta-corp", "seat_limit": 10, "plan": "professional",
            "admin_email": "beta@example.com", "admin_name": "Beta", "admin_password": "a very long password 123", **extra,
        }

    def test_creating_a_tenant_takes_the_plan_default_or_the_chosen_grant(self) -> None:
        default = self.client.post("/api/v1/platform/tenants", json=self.tenant_body(), headers=self.platform)
        self.assertEqual(default.status_code, 201, default.text)
        self.assertEqual((default.json()["portal_mode"], default.json()["portal_seats"], default.json()["portal_overage"]), ("named", 1000, "grace_5"))
        chosen = self.client.post(
            "/api/v1/platform/tenants",
            json=self.tenant_body(slug="gamma", admin_email="g@example.com", portal_mode="named", portal_seats=1500, portal_overage="hard"),
            headers=self.platform,
        )
        self.assertEqual((chosen.json()["portal_seats"], chosen.json()["portal_overage"]), (1500, "hard"))
        bad = self.client.post("/api/v1/platform/tenants", json=self.tenant_body(slug="delta", admin_email="d@example.com", portal_mode="named", portal_seats=0), headers=self.platform)
        self.assertEqual(bad.status_code, 422)

    def test_editing_a_tenant_changes_the_grant_and_audits_it(self) -> None:
        response = self.client.patch(f"/api/v1/platform/tenants/{self.tenant_id}", json={"portal_mode": "active", "portal_seats": 900}, headers=self.platform)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["portal_mode"], response.json()["portal_seats"]), ("active", 900))
        with self.session_factory() as db:
            event = db.scalars(select(AuditEvent).where(AuditEvent.action == "tenant.updated")).first()
        self.assertEqual(event.details["portal"], "合同变更 · AI 门户：指定用户 · 200 · 硬限制 → 按月活跃 · 900 · 硬限制")

    # -- tenant side --------------------------------------------------------------------------

    def test_the_tenant_reads_the_grant_but_cannot_change_it(self) -> None:
        admin = self.login("admin@example.com")
        self.assertEqual(self.client.get("/api/v1/tenant/subscription", headers=admin).json()["portal"]["seats"], 200)
        self.assertEqual(self.client.get("/api/v1/tenant/usage", headers=admin).json()["portal"]["mode"], "named")
        for method, path in (("POST", f"/api/v1/platform/tenants/{self.tenant_id}/subscription"), ("PATCH", f"/api/v1/platform/tenants/{self.tenant_id}")):
            response = self.client.request(method, path, json={"plan_code": "standard", "portal_seats": 9999}, headers=admin)
            self.assertEqual(response.status_code, 403, path)
        self.assertEqual(self.subscription(self.login("platform@example.com"))["portal"]["seats"], 200)

    def test_another_tenants_admin_cannot_read_this_grant(self) -> None:
        with self.session_factory.begin() as db:
            other = Tenant(name="Other", slug="other", plan="standard", seat_limit=3)
            user = User(email="other@example.com", display_name="Other", password_hash=hash_password(PASSWORD))
            db.add_all([other, user])
            db.flush()
            org = Organization(tenant_id=other.id, name="Other")
            db.add(org)
            db.flush()
            db.add(Membership(tenant_id=other.id, user_id=user.id, organization_id=org.id, role="tenant_admin"))
            entitlements.create_subscription(db, other, seats=3)
            other_id = other.id
        headers = {**self.login("other@example.com"), "X-Tenant-ID": str(self.tenant_id)}
        self.assertEqual(self.client.get("/api/v1/tenant/subscription", headers=headers).status_code, 403)
        mine = self.client.get("/api/v1/tenant/subscription", headers={**headers, "X-Tenant-ID": str(other_id)})
        self.assertEqual(mine.json()["portal"]["seats"], 200)

    def test_console_seat_accounting_is_unchanged(self) -> None:
        subscription = self.client.get("/api/v1/tenant/subscription", headers=self.login("admin@example.com")).json()
        seats = next(q for q in subscription["quotas"] if q["key"] == "seats")
        self.assertEqual((seats["limit"], seats["used"]), (3, 1))
        portal = next(q for q in subscription["quotas"] if q["key"] == "portal_seats")
        self.assertEqual((portal["limit"], portal["used"]), (200, 0))  # counted by T-150: nobody holds a portal seat yet


for _name in [n for n in dir(test_plans.PlanManagementTests) if n.startswith("test_")]:
    setattr(PortalEntitlementTests, _name, None)

if __name__ == "__main__":
    unittest.main()
