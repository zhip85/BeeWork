"""T-161: the intent-judgement configuration — targets, rule test, status, references, save validation, publish check."""

from uuid import UUID

import test_capability_registry as base


class IntentConfigTests(base.RegistryTests):
    def setUp(self) -> None:
        super().setUp()
        self.mcp = self.make_mcp()
        self.mount(mcp=[self.mcp])
        self.search = self.live(self.mcp)["search_policy"]
        self.ticket = self.live(self.mcp)["create_ticket"]

    def url(self, suffix: str) -> str:
        return f"/api/v1/assistants/{self.aid}/oc/routing/{suffix}"

    def rule(self, target, **extra) -> dict:
        return {"id": "r1", "name": "退款", "priority": 10, "match_type": "keyword", "pattern": "退款", "target_entry_id": str(target.id), "enabled": True, "created_at": "2026-01-01", **extra}

    def save_intent(self, **intent):
        state = self.client.get(f"/api/v1/assistants/{self.aid}/oc", headers=self.admin).json()
        oc = {**state["oc"], "intent": {**state["oc"]["intent"], **intent}}
        return self.client.put(f"/api/v1/assistants/{self.aid}/oc", json={"oc": oc, "row_version": state["row_version"]}, headers=self.admin)

    def test_targets_list_the_scope_and_say_why_an_entry_cannot_be_a_rule_target(self) -> None:
        self.put(self.search.id, direct_enabled=True)
        items = {i["name"]: i for i in self.client.get(self.url("targets"), headers=self.admin).json()["items"]}
        self.assertEqual((items["search_policy"]["callable"], items["search_policy"]["reason"]), (True, None))
        self.assertEqual((items["create_ticket"]["callable"], items["create_ticket"]["reason"]), (False, "write"))  # a write never runs without the agent
        self.put(self.search.id, direct_enabled=False)
        search = next(i for i in self.client.get(self.url("targets"), headers=self.admin).json()["items"] if i["name"] == "search_policy")
        self.assertEqual((search["callable"], search["reason"]), (False, "not_direct"))

    def test_the_rule_test_uses_the_runtime_matching_and_saves_nothing(self) -> None:
        def test(**body):
            response = self.client.post(self.url("rule-test"), json=body, headers=self.admin)
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

        hit = test(question="我要退款", match_type="keyword", pattern="退款, 退货")
        self.assertEqual((hit["valid"], hit["matched"], hit["keywords"]), (True, True, ["退款", "退货"]))
        self.assertFalse(test(question="你好", match_type="keyword", pattern="退款")["matched"])
        self.assertTrue(test(question="订单 A12", match_type="regex", pattern=r"A\d+")["matched"])
        bad = test(question="x", match_type="regex", pattern="(")
        self.assertEqual((bad["valid"], bad["matched"]), (False, False))
        self.assertFalse(test(question="x", match_type="keyword", pattern="  ")["valid"])

    def test_status_and_the_publish_check_flag_a_rule_whose_target_stopped_being_callable(self) -> None:
        self.put(self.search.id, direct_enabled=True)
        self.assertEqual(self.save_intent(enabled=True, priority_rules_enabled=True, rules=[self.rule(self.search)]).status_code, 200)
        status = self.client.get(self.url("status"), headers=self.admin).json()
        self.assertEqual((status["valid_rules"], status["invalid_rules"], status["problems"]), (1, 0, []))
        self.put(self.search.id, direct_enabled=False)
        status = self.client.get(self.url("status"), headers=self.admin).json()
        self.assertEqual((status["valid_rules"], status["invalid_rules"], status["problems"][0]["code"]), (0, 1, "route.rule_target_invalid"))
        checks = self.client.get(f"/api/v1/assistants/{self.aid}/checks", headers=self.admin).json()["issues"]
        issue = next(i for i in checks if i["code"] == "route.rule_target_invalid")
        self.assertEqual(issue["severity"], "high")
        self.save_intent(priority_rules_enabled=False)  # not executed: no problem to publish with
        checks = self.client.get(f"/api/v1/assistants/{self.aid}/checks", headers=self.admin).json()["issues"]
        self.assertNotIn("route.rule_target_invalid", [i["code"] for i in checks])

    def test_references_name_the_rules_that_point_into_a_source(self) -> None:
        self.put(self.search.id, direct_enabled=True)
        self.save_intent(rules=[self.rule(self.search)])
        found = self.client.get(self.url("references"), params={"source_id": str(self.mcp)}, headers=self.admin).json()
        self.assertEqual((found["rules"], found["total"]), (["退款"], 1))
        other = self.client.get(self.url("references"), params={"source_id": str(self.make_api())}, headers=self.admin).json()
        self.assertEqual(other["total"], 0)

    def test_saving_validates_the_model_and_the_rules(self) -> None:
        unknown = self.save_intent(model_id=str(UUID(int=1)))
        self.assertEqual((unknown.status_code, unknown.json()["code"]), (422, "intent.model_unavailable"))
        self.assertEqual(self.save_intent(threshold=0.1).status_code, 422)
        self.assertEqual(self.save_intent(rules=[self.rule(self.search, pattern="(", match_type="regex")]).status_code, 422)
        self.assertEqual(self.save_intent(rules=[self.rule(self.search), self.rule(self.search)]).status_code, 422)  # duplicate ids
        self.assertEqual(self.save_intent(enabled=True, rules=[self.rule(self.search)]).status_code, 200)

    def test_the_stage_summary_reflects_the_switch(self) -> None:
        self.save_intent(enabled=True, threshold=0.8)
        stages = self.client.get(f"/api/v1/assistants/{self.aid}/oc", headers=self.admin).json()["stages"]
        summary = next(s for s in stages if s["stage"] == "routing")
        self.assertTrue(summary["available"])
        self.assertIn("0.8", summary["summary"])


# The registry tests are inherited only for their helpers; they run in test_capability_registry.
for _name in [n for n in dir(base.RegistryTests) if n.startswith("test_")]:
    setattr(IntentConfigTests, _name, None)
