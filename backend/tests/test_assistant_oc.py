"""T-054: the OC page backend — draft saving, the OC-only approval flow and permissions."""

import unittest

from sqlalchemy import select

import test_versions
from beework.approvals import ensure_policies
from beework.models import AuditEvent
from beework.models.assistants import Assistant
from beework.runtime.oc import OCConfig


class OCTests(test_versions.VersionTests):
    def oc_url(self, suffix: str = "") -> str:
        return f"/api/v1/assistants/{self.aid}/oc{suffix}"

    def get_oc(self, headers=None):
        return self.client.get(self.oc_url(), headers=headers or self.admin)

    def save_oc(self, change: dict | None = None, headers=None, row_version: int | None = None):
        state = self.get_oc(headers).json()
        oc = state["oc"]
        for key, value in (change or {}).items():
            oc[key] = {**oc[key], **value}
        return self.client.put(
            self.oc_url(), json={"oc": oc, "row_version": row_version or state["row_version"]}, headers=headers or self.admin
        )

    def submit(self, note: str = "", headers=None, **extra):
        return self.client.post(self.oc_url("/submit"), json={"change_note": note, **extra}, headers=headers or self.admin)

    def policy(self, enabled: bool, **fields) -> None:
        with self.session_factory.begin() as db:
            policy = ensure_policies(db, self.ids["tenant"])["rule"]
            policy.enabled = enabled
            policy.approver_role = "tenant_admin"
            for key, value in fields.items():
                setattr(policy, key, value)

    def live(self) -> dict:
        version_id = self.refresh()["live_version_id"]
        return self.client.get(f"/api/v1/assistants/{self.aid}/versions/{version_id}", headers=self.admin).json()

    # -- reading and saving -------------------------------------------------------

    def test_state_has_defaults_stage_rows_and_no_live_oc_before_publishing(self) -> None:
        state = self.get_oc().json()
        self.assertEqual(state["oc"], OCConfig().model_dump(mode="json"))
        self.assertIsNone(state["live_oc"])
        self.assertEqual((state["has_changes"], state["diff"]), (False, []))
        self.assertEqual([s["stage"] for s in state["stages"]], ["pii", "memory", "glossary", "rewrite", "routing", "components"])
        self.assertEqual([s["index"] for s in state["stages"]], [1, 2, 3, 4, 5, 6])
        self.assertEqual({s["stage"] for s in state["stages"] if s["available"]}, {"pii", "glossary", "rewrite"})  # memory, routing and components have not landed on this branch
        self.assertEqual((state["assistant"]["name"], state["assistant"]["live_version"], state["assistant"]["can_edit"]), ("Helper", None, True))
        self.assertEqual(state["approval"]["approver_role"], "security_officer")  # default of “智能体规则变更”

    def test_the_pii_summary_counts_baseline_tenant_and_assistant_input_rules(self) -> None:
        from beework.models.governance import SecurityBaselineRule
        from beework.models.tenant_rules import TenantPiiRule

        with self.session_factory.begin() as db:
            db.add(SecurityBaselineRule(name="内容安全", category="content_safety", stage="output", level="enforced", scope="all", status="enabled", action="block", match_type="keyword", pattern="x"))
            db.add(SecurityBaselineRule(name="仅中国区", category="content_safety", stage="input", level="enforced", scope="region_cn", status="enabled", action="block", match_type="keyword", pattern="y"))
            db.add(TenantPiiRule(tenant_id=self.ids["tenant"], name="会员号", pattern="AC", enabled=True))
        row = next(s for s in self.get_oc().json()["stages"] if s["stage"] == "pii")
        self.assertEqual(row["summary"], "2 条生效规则 · PII 开启 · 平台强制基线不受开关影响")  # one baseline row applies + one tenant rule
        self.save_oc({"pii": {"enabled": False, "rules": [{"id": "a1", "name": "工号", "pattern": "EMP", "stage": "input"}, {"id": "a2", "name": "输出", "pattern": "x", "stage": "output"}]}})
        row = next(s for s in self.get_oc().json()["stages"] if s["stage"] == "pii")
        self.assertEqual(row["summary"], "3 条生效规则 · PII 关闭 · 平台强制基线不受开关影响")  # + the assistant's enabled input rule

    def test_the_pii_preview_folds_the_layers_without_saving(self) -> None:
        from beework.models.governance import PlatformPiiRule
        from beework.models.tenant_rules import TenantPiiRule

        with self.session_factory.begin() as db:
            db.add_all([
                PlatformPiiRule(name="身份证号", pattern="id", enforced=True, sort_order=0),
                PlatformPiiRule(name="手机号", pattern="phone", sort_order=1),
            ])
            db.add(TenantPiiRule(tenant_id=self.ids["tenant"], name="会员号", pattern="AC"))
        url = f"/api/v1/assistants/{self.aid}/oc/pii/preview"
        base = self.client.post(url, json={"pii": {}}, headers=self.admin)
        self.assertEqual(base.status_code, 200, base.text)
        body = base.json()
        self.assertEqual({i["name"]: (i["label"], i["enforced"]) for i in body["items"]}, {"身份证号": ("platform_inherited", True), "手机号": ("platform_inherited", False), "会员号": ("tenant", False)})
        self.assertEqual(body["chain"], {"platform_total": 2, "platform_enforced": 1, "tenant_inherited": 2, "tenant_level": 1, "assistant_inherited": 3, "assistant_level": 0, "effective": 3})
        phone_key = next(i["key"] for i in body["items"] if i["name"] == "手机号")
        draft = {"pii": {"enabled": False, "rules": [
            {"id": "o1", "from_id": phone_key, "name": "手机号", "pattern": "mine", "enabled": True},
            {"id": "a1", "name": "工号", "pattern": "EMP"},
        ]}}
        folded = self.client.post(url, json=draft, headers=self.admin).json()
        by_name = {i["name"]: i for i in folded["items"]}
        self.assertEqual((by_name["手机号"]["label"], by_name["手机号"]["restorable"], by_name["手机号"]["pattern"]), ("assistant_modified", True, "mine"))
        self.assertEqual((by_name["工号"]["label"], by_name["工号"]["deletable"]), ("assistant", True))
        self.assertEqual((by_name["身份证号"]["enabled"], by_name["手机号"]["enabled"], by_name["手机号"]["switched_off"]), (True, False, True))  # detection off
        self.assertEqual(folded["chain"]["effective"], 1)
        self.assertEqual(self.refresh()["config"]["oc"]["pii"]["rules"], [])  # nothing was saved
        stopped = self.client.post(url, json={"pii": {"use_global": False}}, headers=self.admin).json()
        self.assertEqual([i["name"] for i in stopped["items"]], ["身份证号"])  # only the enforced rule remains

    def test_the_pii_preview_and_saving_validate_assistant_rules(self) -> None:
        url = f"/api/v1/assistants/{self.aid}/oc/pii/preview"
        for rule in ({"id": "a", "name": "坏", "pattern": "(["}, {"id": "a", "name": " ", "pattern": "x"}, {"id": "a", "name": "空", "pattern": ""}, {"id": "a", "name": "x" * 81, "pattern": "x"}, {"id": "a", "name": "y", "pattern": "x", "action": "bogus"}):
            self.assertEqual(self.client.post(url, json={"pii": {"rules": [rule]}}, headers=self.admin).status_code, 422, rule)
        state = self.get_oc().json()
        bad = {**state["oc"], "pii": {**state["oc"]["pii"], "rules": [{"id": "a", "name": "坏", "pattern": "(["}]}}
        self.assertEqual(self.client.put(self.oc_url(), json={"oc": bad, "row_version": state["row_version"]}, headers=self.admin).status_code, 422)
        good = {**state["oc"], "pii": {**state["oc"]["pii"], "rules": [{"id": "a", "name": "工号", "pattern": "EMP", "action": "block", "block_message": "禁止"}]}}
        saved = self.client.put(self.oc_url(), json={"oc": good, "row_version": state["row_version"]}, headers=self.admin)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["oc"]["pii"]["rules"][0]["block_message"], "禁止")
        many = {**state["oc"], "pii": {"rules": [{"id": f"r{i}", "name": f"n{i}", "pattern": "x"} for i in range(101)]}}
        self.assertEqual(self.client.put(self.oc_url(), json={"oc": many, "row_version": saved.json()["row_version"]}, headers=self.admin).status_code, 422)

    def test_the_pii_preview_respects_permissions_and_tenants(self) -> None:
        url = f"/api/v1/assistants/{self.aid}/oc/pii/preview"
        viewer = self.login("viewer@example.com")
        self.assertEqual(self.client.post(url, json={"pii": {}}, headers=viewer).status_code, 200)
        other = self.login("other@example.com", self.ids["other"])
        self.assertEqual(self.client.post(url, json={"pii": {}}, headers=other).status_code, 404)
        self.client.cookies.clear()
        self.assertEqual(self.client.post(url, json={"pii": {}}).status_code, 401)

    def test_a_registered_stage_summary_shows_up(self) -> None:
        from beework import assistant_oc

        assistant_oc.register_summary("memory", lambda db, assistant, oc: f"短期 {'开' if oc.memory.short_term.enabled else '关'}")
        try:
            row = next(s for s in self.get_oc().json()["stages"] if s["stage"] == "memory")
            self.assertEqual((row["available"], row["summary"]), (True, "短期 开"))
        finally:
            assistant_oc.STAGE_SUMMARIES.pop("memory")
        with self.assertRaises(ValueError):
            assistant_oc.register_summary("nope", lambda *_: "")

    def test_saving_writes_the_draft_only_and_audits(self) -> None:
        self.publish("v1")
        before = self.refresh()
        saved = self.save_oc({"glossary": {"use_tenant": False}})
        self.assertEqual(saved.status_code, 200, saved.text)
        state = saved.json()
        self.assertEqual((state["oc"]["glossary"]["use_tenant"], state["live_oc"]["glossary"]["use_tenant"]), (False, True))
        self.assertTrue(state["has_changes"])
        self.assertEqual(state["diff"][0]["section"], "oc.glossary")
        after = self.refresh()
        self.assertEqual(after["row_version"], before["row_version"] + 1)
        self.assertEqual(after["config"]["oc"]["glossary"]["use_tenant"], False)
        self.assertEqual(after["config"]["prompt"], before["config"]["prompt"])
        self.assertTrue(self.live()["snapshot"]["config"]["oc"]["glossary"]["use_tenant"])  # live is untouched
        with self.session_factory() as db:
            self.assertIsNotNone(db.scalar(select(AuditEvent).where(AuditEvent.action == "assistant.oc_saved")))

    def test_save_validates_and_detects_stale_versions(self) -> None:
        state = self.get_oc().json()
        bad = {"oc": {**state["oc"], "memory": {"short_term": {"window_rounds": 99}}}, "row_version": state["row_version"]}
        self.assertEqual(self.client.put(self.oc_url(), json=bad, headers=self.admin).status_code, 422)
        unknown = {"oc": {**state["oc"], "bogus": 1}, "row_version": state["row_version"]}
        self.assertEqual(self.client.put(self.oc_url(), json=unknown, headers=self.admin).status_code, 422)
        self.assertEqual(self.save_oc({"pii": {"enabled": False}}).status_code, 200)
        stale = self.save_oc({"pii": {"enabled": True}}, row_version=state["row_version"])
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "conflict.stale"))

    def test_saving_an_oc_draft_leaves_other_draft_config_intact(self) -> None:
        self.edit_draft(prompt="Custom prompt.")
        self.save_oc({"routing": {"priority_rules_enabled": True}})
        config = self.refresh()["config"]
        self.assertEqual((config["prompt"], config["oc"]["routing"]["priority_rules_enabled"]), ("Custom prompt.", True))

    # -- submitting ---------------------------------------------------------------

    def test_submitting_needs_a_published_assistant_and_a_difference(self) -> None:
        self.policy(False)
        self.save_oc({"glossary": {"use_tenant": False}})
        never = self.submit()
        self.assertEqual((never.status_code, never.json()["code"]), (409, "assistant.not_published"))
        self.publish("v1")
        self.policy(False)
        self.save_oc()  # same as live
        none = self.submit()
        self.assertEqual((none.status_code, none.json()["code"]), (409, "assistant.no_changes"))

    def test_policy_off_applies_directly_and_only_oc_changes(self) -> None:
        self.publish("v1")
        self.edit_draft(prompt="Unapproved prompt edit.")
        self.save_oc({"rewrite": {"enabled": True}})
        self.policy(False)
        response = self.submit("turn rewrite on")
        self.assertEqual(response.status_code, 201, response.text)
        version = response.json()
        self.assertEqual((version["version"], version["kind"], version["change_note"]), ("1.1", "oc_change", "turn rewrite on"))
        snapshot = version["snapshot"]
        self.assertEqual(snapshot["config"]["prompt"], "You are helpful.")  # the prompt edit stays a draft
        self.assertEqual(snapshot["dsl"]["prompt"], "You are helpful.")
        self.assertTrue(snapshot["config"]["oc"]["rewrite"]["enabled"])
        self.assertTrue(snapshot["dsl"]["oc"]["rewrite"]["enabled"])
        self.assertEqual({d["section"] for d in version["change_diff"]}, {"oc.rewrite"})
        shown = self.refresh()
        self.assertEqual((shown["live_version_id"], shown["config"]["prompt"]), (version["id"], "Unapproved prompt edit."))
        state = self.get_oc().json()
        self.assertEqual((state["has_changes"], state["assistant"]["live_version"]), (False, "1.1"))
        with self.session_factory() as db:
            self.assertIsNotNone(db.scalar(select(AuditEvent).where(AuditEvent.action == "assistant.oc_published")))

    def test_the_approval_applies_only_the_oc_and_keeps_a_later_publish(self) -> None:
        self.publish("v1")
        self.policy(True)
        self.edit_draft(prompt="Draft prompt, never approved.")
        self.save_oc({"glossary": {"use_tenant": False}})
        pending = self.submit("glossary on")
        self.assertEqual(pending.status_code, 202, pending.text)
        approval_id = pending.json()["approval_id"]
        approval = self.client.get(f"/api/v1/approvals/{approval_id}", headers=self.admin).json()
        self.assertEqual((approval["title"], approval["type"], approval["description"]), ("OC 变更 · Helper", "rule_change", "glossary on"))
        self.assertEqual({d["section"] for d in approval["change_diff"]}, {"oc.glossary"})
        # The assistant itself is not shown as “审批中” and can still be published meanwhile.
        self.assertIsNone(self.refresh()["pending_approval_id"])
        self.assertEqual(self.get_oc().json()["pending_approval_id"], approval_id)
        with self.session_factory.begin() as db:
            ensure_policies(db, self.ids["tenant"])["publish"].enabled = False
            ensure_policies(db, self.ids["tenant"])["rule"].enabled = True
        self.edit_draft(prompt="Published in between.")
        self.assertEqual(self.publish("v1.1").status_code, 201)
        self.assertEqual(self.decide(approval_id).status_code, 200)
        self.drain()
        live = self.live()
        self.assertEqual((live["version"], live["kind"], live["approval_id"]), ("1.2", "oc_change", approval_id))
        self.assertEqual(live["snapshot"]["config"]["prompt"], "Published in between.")  # the newer publish survives
        self.assertFalse(live["snapshot"]["config"]["oc"]["glossary"]["use_tenant"])
        self.assertEqual(self.get_oc().json()["pending_approval_id"], None)

    def test_rejection_keeps_the_draft_and_the_live_oc(self) -> None:
        self.publish("v1")
        self.policy(True)
        self.save_oc({"components": {"enabled": True}})
        approval_id = self.submit().json()["approval_id"]
        self.assertEqual(self.decide(approval_id, action="reject", comment="no").status_code, 200)
        self.assertEqual(self.drain(), 0)
        state = self.get_oc().json()
        self.assertEqual((state["oc"]["components"]["enabled"], state["live_oc"]["components"]["enabled"]), (True, False))
        self.assertIsNone(state["pending_approval_id"])
        self.assertEqual(self.refresh()["status"], "published")

    def test_a_second_submission_needs_replace(self) -> None:
        self.publish("v1")
        self.policy(True)
        self.save_oc({"components": {"enabled": True}})
        first = self.submit().json()["approval_id"]
        again = self.submit()
        self.assertEqual((again.status_code, again.json()["code"]), (409, "approval.pending_exists"))
        replaced = self.submit(replace_pending=True)
        self.assertEqual(replaced.status_code, 202)
        self.assertNotEqual(replaced.json()["approval_id"], first)

    def test_applying_is_refused_for_a_disabled_assistant(self) -> None:
        self.publish("v1")
        self.policy(True)
        self.save_oc({"components": {"enabled": True}})
        approval_id = self.submit().json()["approval_id"]
        with self.session_factory.begin() as db:
            db.get(Assistant, self.assistant_uuid()).status = "disabled"
        self.decide(approval_id)
        self.drain()
        approval = self.client.get(f"/api/v1/approvals/{approval_id}", headers=self.admin).json()
        self.assertNotEqual(approval["status"], "applied")
        self.assertEqual(len(self.client.get(f"/api/v1/assistants/{self.aid}/versions", headers=self.admin).json()["items"]), 1)

    def assistant_uuid(self):
        from uuid import UUID

        return UUID(self.aid)

    def test_a_disabled_assistant_cannot_submit(self) -> None:
        self.publish("v1")
        self.policy(False)
        self.save_oc({"components": {"enabled": True}})
        with self.session_factory.begin() as db:
            db.get(Assistant, self.assistant_uuid()).status = "disabled"
        self.assertEqual(self.submit().json()["code"], "assistant.invalid_transition")

    def test_the_next_runtime_uses_the_oc_of_the_live_snapshot(self) -> None:
        self.publish("v1")
        self.policy(False)
        self.save_oc({"pii": {"enabled": False}})
        self.assertEqual(self.submit().status_code, 201)
        self.assertFalse(self.live()["snapshot"]["dsl"]["oc"]["pii"]["enabled"])

    # -- selector, permissions, isolation -----------------------------------------

    def test_assistant_selector_lists_visible_assistants_with_live_versions(self) -> None:
        self.publish("v1")
        second = self.client.post(
            "/api/v1/assistants", json={"name": "Second", "description": "d", "config": {"prompt": "x"}}, headers=self.admin
        )
        self.assertEqual(second.status_code, 201)
        items = self.client.get("/api/v1/oc/assistants", headers=self.admin).json()["items"]
        self.assertEqual({(i["name"], i["version"]) for i in items}, {("Helper", "1.0"), ("Second", None)})
        self.assertNotIn("Foreign", {i["name"] for i in items})

    def test_viewers_read_but_cannot_save_or_submit(self) -> None:
        self.publish("v1")
        self.assertEqual(self.save_oc({"pii": {"enabled": False}}).status_code, 200)
        viewer = self.login("viewer@example.com")
        self.assertEqual(self.get_oc(viewer).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/oc/assistants", headers=viewer).status_code, 200)
        state = self.get_oc(viewer).json()
        put = self.client.put(self.oc_url(), json={"oc": state["oc"], "row_version": state["row_version"]}, headers=viewer)
        self.assertEqual(put.status_code, 403)
        self.assertEqual(self.submit(headers=viewer).status_code, 403)

    def test_other_tenants_and_anonymous_users_are_refused(self) -> None:
        state = self.get_oc().json()
        other = self.login("other@example.com", self.ids["other"])
        self.assertEqual(self.get_oc(other).status_code, 404)
        self.assertEqual(
            self.client.put(self.oc_url(), json={"oc": state["oc"], "row_version": state["row_version"]}, headers=other).status_code, 404
        )
        self.assertEqual(self.submit(headers=other).status_code, 404)
        self.admin = self.login("admin@example.com")
        foreign = self.client.get(f"/api/v1/assistants/{self.ids['other_assistant']}/oc", headers=self.admin)
        self.assertEqual(foreign.status_code, 404)
        self.client.cookies.clear()
        self.assertEqual(self.client.get(self.oc_url()).status_code, 401)

    def test_unknown_assistant_is_404(self) -> None:
        self.assertEqual(self.client.get("/api/v1/assistants/00000000-0000-0000-0000-000000000000/oc", headers=self.admin).status_code, 404)


# Reuse the harness only: the inherited tests already run in test_versions and must not run a second time here.
for _name in dir(test_versions.VersionTests):
    if _name.startswith("test_") and _name not in OCTests.__dict__:
        setattr(OCTests, _name, None)
del test_versions

if __name__ == "__main__":
    unittest.main()
