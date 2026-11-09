"""T-052: the tenant PII rule library — overrides, restore, the inheritance switch, approvals, permissions, isolation."""

import unittest
from uuid import UUID

from sqlalchemy import select

import test_versions
from beework import governance
from beework.approvals import ensure_policies
from beework.models import AuditEvent, Membership, Organization, User
from beework.models.governance import PlatformPiiRule
from beework.models.tenant_rules import TenantPiiRule
from beework.security import hash_password

URL = "/api/v1/tenant/pii-rules"


class TenantPiiTests(test_versions.VersionTests):
    def setUp(self) -> None:
        super().setUp()
        with self.session_factory.begin() as db:
            ensure_policies(db, self.ids["tenant"])["rule"].enabled = False  # tests switch the approval on when needed
            for name, pattern, enforced, enabled, order in (
                ("身份证号", r"\d{17}[\dXx]", True, True, 0), ("手机号", r"1[3-9]\d{9}", False, True, 1), ("邮箱", r"[\w.]+@[\w.]+", False, False, 2),
            ):
                db.add(PlatformPiiRule(name=name, match_type="regex", pattern=pattern, stage="output", action="mask", enforced=enforced, enabled=enabled, sort_order=order))
            auditor = User(email="auditor@example.com", display_name="Auditor", password_hash=hash_password(test_versions.PASSWORD))
            db.add(auditor)
            db.flush()
            root = db.scalar(select(Organization.id).where(Organization.tenant_id == self.ids["tenant"]))
            db.add(Membership(tenant_id=self.ids["tenant"], user_id=auditor.id, organization_id=root, role="auditor"))
            db.flush()
            self.platform = {r.name: str(r.id) for r in db.scalars(select(PlatformPiiRule))}

    # -- helpers ------------------------------------------------------------------

    def state(self, headers=None, **params):
        return self.client.get(URL, params=params, headers=headers or self.admin)

    def items(self, **params) -> dict[str, dict]:
        return {i["name"]: i for i in self.state(**params).json()["items"]}

    def body(self, **overrides) -> dict:
        return {"name": "手机号", "match_type": "regex", "pattern": r"1[3-9]\d{9}", "stage": "output", "action": "mask", "block_message": "", "enabled": True, **overrides}

    def put(self, key: str, headers=None, **overrides):
        return self.client.put(f"{URL}/{key}", json=self.body(**overrides), headers=headers or self.admin)

    def create(self, headers=None, **overrides):
        fields = {"name": "会员号", "pattern": r"AC\d{10}", **overrides}
        return self.client.post(URL, json=self.body(**fields), headers=headers or self.admin)

    def tenant_rows(self) -> list[TenantPiiRule]:
        with self.session_factory() as db:
            return list(db.scalars(select(TenantPiiRule)))

    def platform_row(self, name: str) -> PlatformPiiRule:
        with self.session_factory() as db:
            return db.scalar(select(PlatformPiiRule).where(PlatformPiiRule.name == name))

    def audit_actions(self) -> list[str]:
        with self.session_factory() as db:
            return [a.action for a in db.scalars(select(AuditEvent).order_by(AuditEvent.created_at)) if a.action.startswith("tenant_pii")]

    def require_approval(self) -> None:
        with self.session_factory.begin() as db:
            policy = ensure_policies(db, self.ids["tenant"])["rule"]
            policy.enabled = True
            policy.approver_role = "tenant_admin"

    # -- reading ------------------------------------------------------------------

    def test_the_library_starts_as_the_platform_rules_with_the_chain_numbers(self) -> None:
        state = self.state().json()
        self.assertEqual((state["inherit_platform"], state["settings_version"], state["tenant_name"]), (True, 0, "Acme"))
        self.assertEqual(state["chain"], {"platform_total": 3, "platform_enforced": 1, "inherited": 3, "tenant_level": 0, "overrides": 0, "total": 3, "effective": 2})
        items = {i["name"]: i for i in state["items"]}
        self.assertEqual([(i["label"], i["enforced"], i["restorable"], i["deletable"]) for i in items.values()], [("platform_inherited", True, False, False), ("platform_inherited", False, False, False), ("platform_inherited", False, False, False)])
        self.assertEqual((items["身份证号"]["enabled"], items["邮箱"]["enabled"]), (True, False))
        self.assertTrue(state["approval"]["required"] is False)

    # -- overrides ----------------------------------------------------------------

    def test_editing_an_inherited_rule_creates_an_override_and_leaves_the_platform_untouched(self) -> None:
        before = self.platform_row("手机号")
        response = self.put(self.platform["手机号"], pattern=r"1\d{10}", enabled=False)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"outcome": "applied", "kind": "override_created", "approval_id": None})
        after = self.platform_row("手机号")
        self.assertEqual((after.pattern, after.enabled, after.row_version), (before.pattern, before.enabled, before.row_version))
        item = self.items()["手机号"]
        self.assertEqual((item["label"], item["pattern"], item["enabled"], item["restorable"], item["deletable"]), ("tenant_modified", r"1\d{10}", False, True, False))
        self.assertEqual(item["key"], self.platform["手机号"])  # the key stays the platform rule's
        again = self.put(self.platform["手机号"], pattern=r"1\d{9}", enabled=True).json()
        self.assertEqual(again["kind"], "updated")  # the same override, not a second one
        self.assertEqual(len(self.tenant_rows()), 1)
        self.assertEqual(self.state().json()["chain"]["overrides"], 1)
        self.assertEqual(self.audit_actions(), ["tenant_pii_rule.overridden", "tenant_pii_rule.updated"])

    def test_restore_deletes_the_override(self) -> None:
        self.put(self.platform["手机号"], pattern="x", enabled=False)
        restored = self.client.post(f"{URL}/{self.platform['手机号']}/restore", headers=self.admin)
        self.assertEqual((restored.status_code, restored.json()["kind"]), (200, "restored"))
        self.assertEqual((self.items()["手机号"]["label"], self.tenant_rows()), ("platform_inherited", []))
        again = self.client.post(f"{URL}/{self.platform['手机号']}/restore", headers=self.admin)
        self.assertEqual((again.status_code, again.json()["code"]), (409, "tenant_pii.nothing_to_restore"))
        self.assertEqual(self.client.post(f"{URL}/{self.platform['邮箱']}/restore", headers=self.admin).status_code, 409)

    def test_an_enforced_rule_stays_on_but_its_content_can_change(self) -> None:
        response = self.put(self.platform["身份证号"], name="身份证号", pattern=r"\d{18}", enabled=False)
        self.assertEqual(response.status_code, 200, response.text)
        item = self.items()["身份证号"]
        self.assertEqual((item["pattern"], item["enabled"], item["enforced"], item["label"]), (r"\d{18}", True, True, "tenant_modified"))
        self.assertEqual(self.tenant_rows()[0].enabled, False)  # stored as asked, but never honoured for an enforced rule

    def test_a_tenant_can_switch_on_a_rule_the_platform_left_off(self) -> None:
        self.put(self.platform["邮箱"], name="邮箱", pattern=r"[\w.]+@[\w.]+", enabled=True)
        self.assertTrue(self.items()["邮箱"]["enabled"])

    # -- own rules ----------------------------------------------------------------

    def test_own_rules_can_be_created_edited_and_deleted(self) -> None:
        created = self.create()
        self.assertEqual((created.status_code, created.json()["kind"]), (201, "created"), created.text)
        rule = self.items()["会员号"]
        self.assertEqual((rule["label"], rule["deletable"], rule["restorable"], rule["enforced"]), ("tenant", True, False, False))
        edited = self.put(rule["key"], name="会员编号", pattern=r"AC\d{12}", action="block", block_message="请勿发送")
        self.assertEqual(edited.json()["kind"], "updated")
        self.assertEqual(self.items()["会员编号"]["block_message"], "请勿发送")
        self.assertEqual(self.client.delete(f"{URL}/{rule['key']}", headers=self.admin).json()["kind"], "deleted")
        self.assertEqual(self.tenant_rows(), [])
        self.assertEqual(self.audit_actions(), ["tenant_pii_rule.created", "tenant_pii_rule.updated", "tenant_pii_rule.deleted"])

    def test_validation_names_regexes_enforced_flag_and_limits(self) -> None:
        bad = self.create(pattern="([")
        self.assertEqual((bad.status_code, bad.json()["code"]), (422, "governance.invalid"))
        self.assertIn("正则表达式无效", bad.json()["detail"])
        self.assertEqual(self.client.post(URL, json=self.body(name="  "), headers=self.admin).status_code, 422)
        self.assertEqual(self.create().status_code, 201)
        dup = self.create()
        self.assertEqual((dup.status_code, dup.json()["code"]), (409, "tenant_pii.duplicate_name"))
        clash = self.client.post(URL, json=self.body(name="邮箱"), headers=self.admin)  # the name of a platform rule
        self.assertEqual(clash.status_code, 409)
        self.assertFalse(self.items()["会员号"]["enforced"])
        self.assertEqual(self.client.delete(f"{URL}/{self.platform['手机号']}", headers=self.admin).json()["code"], "tenant_pii.not_deletable")
        self.assertEqual(self.client.delete(f"{URL}/{UUID(int=1)}", headers=self.admin).status_code, 404)
        self.assertEqual(self.client.put(f"{URL}/{UUID(int=1)}", json=self.body(), headers=self.admin).status_code, 404)

    # -- the inheritance switch ---------------------------------------------------

    def test_switching_off_inheritance_keeps_only_enforced_platform_rules(self) -> None:
        self.create()
        self.put(self.platform["手机号"], pattern="x")
        saved = self.client.put(f"{URL}/settings", json={"inherit_platform": False, "row_version": 0}, headers=self.admin)
        self.assertEqual((saved.status_code, saved.json()["kind"]), (200, "settings"))
        state = self.state().json()
        self.assertEqual((state["inherit_platform"], state["settings_version"]), (False, 1))
        self.assertEqual(sorted(i["name"] for i in state["items"]), ["会员号", "身份证号"])  # the override of 手机号 is not inherited either
        self.assertEqual(state["chain"]["effective"], 2)
        stale = self.client.put(f"{URL}/settings", json={"inherit_platform": True, "row_version": 0}, headers=self.admin)
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "conflict.stale"))
        self.client.put(f"{URL}/settings", json={"inherit_platform": True, "row_version": 1}, headers=self.admin)
        self.assertEqual(len(self.state().json()["items"]), 4)
        self.assertIn("tenant_pii_settings.updated", self.audit_actions())

    def test_the_scope_filter_shows_only_tenant_level_rules(self) -> None:
        self.create()
        self.put(self.platform["手机号"], pattern="x")
        self.assertEqual(sorted(self.items(scope="tenant")), ["会员号", "手机号"])
        self.assertEqual(len(self.items(scope="all")), 4)
        self.assertEqual(self.state(scope="bogus").status_code, 422)

    # -- approval (SET-053) ---------------------------------------------------------

    def test_changes_wait_for_approval_when_the_policy_is_on(self) -> None:
        self.require_approval()
        pending = self.put(self.platform["手机号"], pattern="x")
        self.assertEqual(pending.status_code, 202, pending.text)
        self.assertEqual((pending.json()["outcome"], pending.json()["kind"]), ("pending", "override_created"))
        self.assertEqual(self.tenant_rows(), [])  # nothing took effect
        approval = self.client.get(f"/api/v1/approvals/{pending.json()['approval_id']}", headers=self.admin).json()
        self.assertEqual((approval["type"], approval["title"]), ("rule_change", "租户规则变更 · 修改 手机号"))
        self.assertTrue(any(d["field"] == "pattern" and d["to"] == "x" for d in approval["change_diff"]))
        self.assertEqual(self.items()["手机号"]["pending_approval_id"], pending.json()["approval_id"])
        again = self.put(self.platform["手机号"], pattern="y")
        self.assertEqual((again.status_code, again.json()["code"]), (409, "approval.pending_exists"))
        self.assertEqual(self.decide(pending.json()["approval_id"]).status_code, 200)
        self.assertEqual(self.drain(), 1)
        self.assertEqual((self.items()["手机号"]["label"], self.items()["手机号"]["pattern"]), ("tenant_modified", "x"))

    def test_a_rejected_change_never_applies_and_other_rules_stay_editable(self) -> None:
        self.require_approval()
        pending = self.create().json()
        self.assertEqual(self.decide(pending["approval_id"], action="reject", comment="no").status_code, 200)
        self.assertEqual(self.drain(), 0)
        self.assertEqual(self.tenant_rows(), [])
        self.assertEqual(self.put(self.platform["手机号"], pattern="z").status_code, 202)  # a different rule: its own request

    def test_the_approval_executor_reports_a_rule_that_vanished(self) -> None:
        self.require_approval()
        own_id = None
        with self.session_factory.begin() as db:
            row = TenantPiiRule(tenant_id=self.ids["tenant"], name="临时", pattern="x")
            db.add(row)
            db.flush()
            own_id = str(row.id)
        pending = self.put(own_id, name="临时", pattern="y")
        self.assertEqual(pending.status_code, 202)
        with self.session_factory.begin() as db:
            db.delete(db.get(TenantPiiRule, UUID(own_id)))
        self.decide(pending.json()["approval_id"])
        self.drain()
        approval = self.client.get(f"/api/v1/approvals/{pending.json()['approval_id']}", headers=self.admin).json()
        self.assertNotEqual(approval["status"], "applied")

    # -- platform changes flow down -------------------------------------------------

    def test_platform_edits_reach_the_tenant_unless_overridden(self) -> None:
        self.put(self.platform["手机号"], pattern="mine")
        with self.session_factory.begin() as db:
            platform = db.scalar(select(PlatformPiiRule).where(PlatformPiiRule.name == "邮箱"))
            platform.pattern = "changed"
            platform.enabled = True
            other = db.scalar(select(PlatformPiiRule).where(PlatformPiiRule.name == "手机号"))
            other.pattern = "platform-changed"
        items = self.items()
        self.assertEqual((items["邮箱"]["pattern"], items["邮箱"]["enabled"]), ("changed", True))  # follows the platform at once
        self.assertEqual(items["手机号"]["pattern"], "mine")  # an override is not affected

    def test_deleting_a_platform_rule_turns_the_overrides_into_tenant_rules(self) -> None:
        self.put(self.platform["手机号"], pattern="mine")
        with self.session_factory.begin() as db:
            governance.apply_pii_change(db, "delete", UUID(self.platform["手机号"]), None, "platform")
        rows = self.tenant_rows()
        self.assertEqual((len(rows), rows[0].from_id, rows[0].pattern), (1, None, "mine"))
        item = self.items()["手机号"]
        self.assertEqual((item["label"], item["deletable"], item["restorable"]), ("tenant", True, False))

    # -- permissions and isolation --------------------------------------------------

    def test_auditors_read_but_cannot_change_and_viewers_have_no_access(self) -> None:
        auditor = self.login("auditor@example.com")
        self.assertEqual(self.state(auditor).status_code, 200)
        self.assertEqual(self.create(auditor).status_code, 403)
        self.assertEqual(self.put(self.platform["手机号"], auditor).status_code, 403)
        self.assertEqual(self.client.delete(f"{URL}/{self.platform['手机号']}", headers=auditor).status_code, 403)
        self.assertEqual(self.client.post(f"{URL}/{self.platform['手机号']}/restore", headers=auditor).status_code, 403)
        self.assertEqual(self.client.put(f"{URL}/settings", json={"inherit_platform": False, "row_version": 0}, headers=auditor).status_code, 403)
        viewer = self.login("viewer@example.com")  # “设置 · 规则库” is for tenant admins (V E D) and auditors (V)
        self.assertEqual(self.state(viewer).status_code, 403)
        self.assertEqual(self.create(viewer).status_code, 403)
        self.admin = self.login("admin@example.com")
        self.client.cookies.clear()
        self.assertEqual(self.client.get(URL).status_code, 401)

    def test_other_tenants_see_their_own_library_and_cannot_touch_this_tenants_rules(self) -> None:
        self.create()
        own_key = self.items()["会员号"]["key"]
        self.put(self.platform["手机号"], pattern="mine")
        with self.session_factory.begin() as db:
            ensure_policies(db, self.ids["other"])["rule"].enabled = False
        other = self.login("other@example.com", self.ids["other"])
        self.assertEqual(self.state(other).json()["chain"]["tenant_level"], 0)
        self.assertEqual(self.state(other).json()["chain"]["overrides"], 0)
        self.assertEqual(self.put(own_key, other).status_code, 404)  # another tenant's own rule id
        self.assertEqual(self.client.delete(f"{URL}/{own_key}", headers=other).status_code, 404)
        theirs = self.put(self.platform["手机号"], other, pattern="theirs")  # platform rule ids are shared, overrides are not
        self.assertEqual(theirs.status_code, 200)
        self.admin = self.login("admin@example.com")
        self.assertEqual(self.items()["手机号"]["pattern"], "mine")
        self.assertEqual(len(self.tenant_rows()), 3)


# Reuse the harness only: the inherited tests already run in test_versions and must not run a second time here.
for _name in dir(test_versions.VersionTests):
    if _name.startswith("test_") and _name not in TenantPiiTests.__dict__:
        setattr(TenantPiiTests, _name, None)

if __name__ == "__main__":
    unittest.main()
