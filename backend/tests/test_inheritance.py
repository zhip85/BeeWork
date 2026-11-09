"""T-052: the pure inheritance engine, step by step against OC-012 (no database)."""

import unittest

from beework.governance.inheritance import Rule, Switches, effective_count, effective_rules


def platform(rule_id: str, *, enforced: bool = False, enabled: bool = True, **fields) -> Rule:
    return Rule(id=rule_id, layer="platform", name=rule_id, enforced=enforced, enabled=enabled, **fields)


def tenant(rule_id: str, from_id: str | None = None, **fields) -> Rule:
    return Rule(id=rule_id, layer="tenant", name=fields.pop("name", rule_id), from_id=from_id, **fields)


def assistant(rule_id: str, from_id: str | None = None, **fields) -> Rule:
    return Rule(id=rule_id, layer="assistant", name=fields.pop("name", rule_id), from_id=from_id, **fields)


# P1 enforced, P2 plain, P3 plain but switched off at the platform.
PLATFORM = [platform("P1", enforced=True, pattern="id"), platform("P2", pattern="phone"), platform("P3", enabled=False, pattern="mail")]


def view(rules):
    return {item.key: (item.label, item.enabled, item.enforced) for item in rules}


class TenantLayerTests(unittest.TestCase):
    def test_step_one_inherits_everything_or_only_the_enforced_rules(self) -> None:
        on = effective_rules(PLATFORM, [], switches=Switches(inherit_platform=True))
        self.assertEqual(view(on), {"P1": ("platform_inherited", True, True), "P2": ("platform_inherited", True, False), "P3": ("platform_inherited", False, False)})
        off = effective_rules(PLATFORM, [], switches=Switches(inherit_platform=False))
        self.assertEqual(list(view(off)), ["P1"])  # the enforced rule survives, the others are not inherited

    def test_step_two_a_tenant_override_replaces_the_platform_rule_and_own_rules_are_added(self) -> None:
        override = tenant("T1", from_id="P2", pattern="phone2", enabled=False)
        own = tenant("T2", pattern="member", name="会员号")
        result = effective_rules(PLATFORM, [override, own])
        by_key = {item.key: item for item in result}
        self.assertEqual(by_key["P2"].label, "tenant_modified")
        self.assertEqual((by_key["P2"].rule.pattern, by_key["P2"].enabled, by_key["P2"].override_id), ("phone2", False, "T1"))
        self.assertEqual((by_key["T2"].label, by_key["T2"].enabled, by_key["T2"].rule.name), ("tenant", True, "会员号"))
        self.assertEqual(by_key["P1"].label, "platform_inherited")  # untouched rules stay inherited
        self.assertEqual(effective_count(result), 2)  # P1 and T2; the P2 override is off and P3 is off at the platform

    def test_an_enforced_rule_cannot_be_switched_off_but_its_content_can_change(self) -> None:
        override = tenant("T1", from_id="P1", pattern="stricter", enabled=False, enforced=False)
        item = {i.key: i for i in effective_rules(PLATFORM, [override])}["P1"]
        self.assertEqual((item.label, item.rule.pattern, item.enabled, item.enforced), ("tenant_modified", "stricter", True, True))

    def test_an_enforced_rule_the_platform_switched_off_binds_every_layer(self) -> None:
        platform_rules = [platform("P1", enforced=True, enabled=False)]
        override = tenant("T1", from_id="P1", enabled=True)
        self.assertFalse(effective_rules(platform_rules, [override])[0].enabled)

    def test_a_tenant_can_switch_on_a_rule_the_platform_left_off(self) -> None:
        override = tenant("T1", from_id="P3", enabled=True)
        self.assertTrue({i.key: i for i in effective_rules(PLATFORM, [override])}["P3"].enabled)

    def test_overrides_of_rules_that_are_not_inherited_are_ignored(self) -> None:
        override = tenant("T1", from_id="P2", pattern="x")
        result = effective_rules(PLATFORM, [override], switches=Switches(inherit_platform=False))
        self.assertEqual([i.key for i in result], ["P1"])  # P2 is not inherited, so its override has nothing to modify

    def test_an_override_of_an_unknown_rule_is_dropped(self) -> None:
        result = effective_rules(PLATFORM, [tenant("T1", from_id="GONE")])
        self.assertEqual([i.key for i in result], ["P1", "P2", "P3"])

    def test_the_input_is_never_mutated(self) -> None:
        before = list(PLATFORM)
        effective_rules(PLATFORM, [tenant("T1", from_id="P2", enabled=False)])
        self.assertEqual(PLATFORM, before)


class AssistantLayerTests(unittest.TestCase):
    TENANT = [tenant("T1", from_id="P2", pattern="phone2"), tenant("T2", pattern="member", name="会员号")]

    def test_step_three_references_the_tenant_rules_or_only_the_enforced_ones(self) -> None:
        on = effective_rules(PLATFORM, self.TENANT, [], Switches(use_global=True))
        self.assertEqual(view(on), {"P1": ("platform_inherited", True, True), "P2": ("tenant_modified", True, False), "P3": ("platform_inherited", False, False), "T2": ("tenant", True, False)})
        off = effective_rules(PLATFORM, self.TENANT, [], Switches(use_global=False))
        self.assertEqual(list(view(off)), ["P1"])

    def test_assistant_overrides_win_over_tenant_and_platform_and_label_themselves(self) -> None:
        mine = [assistant("A1", from_id="P2", pattern="phone3", enabled=False), assistant("A2", from_id="T2", pattern="member2"), assistant("A3", name="工号", pattern="emp")]
        by_key = {i.key: i for i in effective_rules(PLATFORM, self.TENANT, mine)}
        self.assertEqual((by_key["P2"].label, by_key["P2"].rule.pattern, by_key["P2"].enabled, by_key["P2"].override_id), ("assistant_modified", "phone3", False, "A1"))
        self.assertEqual((by_key["T2"].label, by_key["T2"].rule.pattern), ("assistant_modified", "member2"))
        self.assertEqual((by_key["A3"].label, by_key["A3"].rule.name), ("assistant", "工号"))
        self.assertEqual(by_key["P2"].layers, ("platform", "tenant", "assistant"))

    def test_an_assistant_cannot_switch_off_an_enforced_rule(self) -> None:
        mine = [assistant("A1", from_id="P1", pattern="stricter", enabled=False)]
        item = {i.key: i for i in effective_rules(PLATFORM, self.TENANT, mine)}["P1"]
        self.assertEqual((item.rule.pattern, item.enabled, item.enforced, item.label), ("stricter", True, True, "assistant_modified"))

    def test_step_four_detection_off_leaves_only_enforced_rules_in_force(self) -> None:
        mine = [assistant("A3", name="工号")]
        result = effective_rules(PLATFORM, self.TENANT, mine, Switches(pii_detection=False))
        self.assertEqual({i.key: (i.enabled, i.switched_off) for i in result}, {"P1": (True, False), "P2": (False, True), "P3": (False, True), "T2": (False, True), "A3": (False, True)})
        self.assertEqual(effective_count(result), 1)

    def test_step_four_applies_at_the_tenant_layer_too(self) -> None:
        result = effective_rules(PLATFORM, self.TENANT, switches=Switches(pii_detection=False))
        self.assertEqual(effective_count(result), 1)

    def test_step_five_counts_enabled_rules_for_every_switch_combination(self) -> None:
        mine = [assistant("A3", name="工号")]
        cases = {
            (True, True, True): 4,   # P1, P2(override on), T2, A3; P3 is off at the platform
            (True, False, True): 2,  # P1 + the assistant's own rule
            (False, True, True): 3,  # P1, T2, A3 (the platform's non-enforced rules are not inherited)
            (False, False, True): 2,
            (True, True, False): 1,
            (False, False, False): 1,
        }
        for (inherit, use_global, detect), expected in cases.items():
            switches = Switches(inherit_platform=inherit, use_global=use_global, pii_detection=detect)
            self.assertEqual(effective_count(effective_rules(PLATFORM, self.TENANT, mine, switches)), expected, (inherit, use_global, detect))

    def test_without_an_assistant_layer_the_tenant_view_is_returned(self) -> None:
        self.assertEqual(effective_rules(PLATFORM, self.TENANT), effective_rules(PLATFORM, self.TENANT, None))


if __name__ == "__main__":
    unittest.main()
