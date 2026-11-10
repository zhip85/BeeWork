"""T-053: the pure term inheritance engine (industries → tenant → assistant), no database."""

import unittest

from beework.governance.term_inheritance import Term, assistant_terms, industry_terms, tenant_terms


def ind(term_id: str, term: str, definition: str = "", aliases: tuple[str, ...] = (), source: str = "g1") -> Term:
    return Term(id=term_id, layer="industry", term=term, aliases=aliases, definition=definition, source=source)


def ten(term_id: str, term: str, *, from_id: str | None = None, hidden: bool = False, definition: str = "", aliases: tuple[str, ...] = ()) -> Term:
    return Term(id=term_id, layer="tenant", term=term, aliases=aliases, definition=definition, from_id=from_id, hidden=hidden)


def asst(term_id: str, term: str, *, from_id: str | None = None, hidden: bool = False, definition: str = "") -> Term:
    return Term(id=term_id, layer="assistant", term=term, definition=definition, from_id=from_id, hidden=hidden)


RETAIL = [ind("R1", "SKU", "retail def", ("库存单位",), "retail"), ind("R2", "GMV", "gmv", source="retail")]
INSURANCE = [ind("I1", "理赔", "claim", source="insurance"), ind("I2", "sku", "insurance sku", source="insurance")]


def labels(view) -> dict[str, str]:
    return {item.id: item.label for item in view.terms}


class IndustryTests(unittest.TestCase):
    def test_a_standard_term_in_two_glossaries_is_inherited_once_from_the_first_selected(self) -> None:
        merged = industry_terms([RETAIL, INSURANCE])
        self.assertEqual([t.id for t in merged], ["R1", "R2", "I1"])  # "sku" of the insurance glossary is a duplicate of "SKU"
        self.assertEqual(merged[0].definition, "retail def")
        flipped = industry_terms([INSURANCE, RETAIL])
        self.assertEqual([t.id for t in flipped], ["I1", "I2", "R2"])  # the order of the selection decides
        self.assertEqual({t.id: t.definition for t in flipped}["I2"], "insurance sku")

    def test_no_glossary_means_no_inherited_terms(self) -> None:
        self.assertEqual(industry_terms([]), [])
        self.assertEqual(tenant_terms([], []).terms, [])

    def test_terms_are_compared_ignoring_case_and_surrounding_spaces(self) -> None:
        self.assertEqual(len(industry_terms([[ind("a", " Claim "), ind("b", "claim")]])), 1)


class TenantLayerTests(unittest.TestCase):
    INDUSTRY = industry_terms([RETAIL, INSURANCE])

    def test_untouched_terms_are_inherited(self) -> None:
        view = tenant_terms(self.INDUSTRY, [])
        self.assertEqual(labels(view), {"R1": "industry_inherited", "R2": "industry_inherited", "I1": "industry_inherited"})
        self.assertEqual(view.hidden, [])

    def test_an_override_replaces_the_inherited_term_and_is_restorable(self) -> None:
        override = ten("T1", "SKU", from_id="R1", definition="tenant def", aliases=("商品编码",))
        view = tenant_terms(self.INDUSTRY, [override])
        by_id = {item.id: item for item in view.terms}
        self.assertEqual((by_id["R1"].label, by_id["R1"].term.definition, by_id["R1"].override_id), ("tenant_modified", "tenant def", "T1"))
        self.assertEqual(by_id["R1"].term.aliases, ("商品编码",))
        self.assertEqual(by_id["R1"].layers, ("industry", "tenant"))
        self.assertEqual(by_id["R2"].label, "industry_inherited")

    def test_hiding_removes_the_term_and_is_counted(self) -> None:
        view = tenant_terms(self.INDUSTRY, [ten("T1", "GMV", from_id="R2", hidden=True)])
        self.assertEqual(sorted(labels(view)), ["I1", "R1"])
        self.assertEqual([t.term for t in view.hidden], ["GMV"])

    def test_own_terms_are_added(self) -> None:
        view = tenant_terms(self.INDUSTRY, [ten("T9", "会员号", definition="member")])
        by_id = {item.id: item for item in view.terms}
        self.assertEqual((by_id["T9"].label, by_id["T9"].term.term, by_id["T9"].layers), ("tenant", "会员号", ("tenant",)))

    def test_an_override_of_a_term_that_is_no_longer_inherited_is_dormant(self) -> None:
        # The tenant deselected the retail glossary: its override of R1 has nothing to modify, and nothing leaks into the view.
        view = tenant_terms(industry_terms([INSURANCE]), [ten("T1", "SKU", from_id="R1", definition="x"), ten("T2", "GMV", from_id="R2", hidden=True)])
        self.assertEqual(sorted(labels(view)), ["I1", "I2"])
        self.assertEqual(view.hidden, [])

    def test_changing_the_platform_does_not_change_an_overridden_term(self) -> None:
        override = ten("T1", "SKU", from_id="R1", definition="mine")
        changed = industry_terms([[ind("R1", "SKU", "platform changed"), RETAIL[1]]])
        by_id = {item.id: item for item in tenant_terms(changed, [override]).terms}
        self.assertEqual(by_id["R1"].term.definition, "mine")
        self.assertEqual(by_id["R2"].term.definition, "gmv")  # untouched terms follow the platform

    def test_the_input_lists_are_not_mutated(self) -> None:
        rows = [ten("T1", "SKU", from_id="R1")]
        before = (list(self.INDUSTRY), list(rows))
        tenant_terms(self.INDUSTRY, rows)
        self.assertEqual((self.INDUSTRY, rows), before)


class AssistantLayerTests(unittest.TestCase):
    INDUSTRY = industry_terms([RETAIL])
    TENANT = [ten("T1", "SKU", from_id="R1", definition="tenant def"), ten("T2", "会员号", definition="member")]

    def test_the_assistant_starts_from_the_tenant_view(self) -> None:
        view = assistant_terms(self.INDUSTRY, self.TENANT, [])
        self.assertEqual(labels(view), {"R1": "tenant_modified", "R2": "industry_inherited", "T2": "tenant"})

    def test_assistant_overrides_hides_and_own_terms(self) -> None:
        mine = [asst("A1", "GMV", from_id="R2", definition="asst"), asst("A2", "会员号", from_id="T2", hidden=True), asst("A3", "工号", definition="emp")]
        view = assistant_terms(self.INDUSTRY, self.TENANT, mine)
        by_id = {item.id: item for item in view.terms}
        self.assertEqual((by_id["R2"].label, by_id["R2"].term.definition, by_id["R2"].layers), ("assistant_modified", "asst", ("industry", "assistant")))
        self.assertNotIn("T2", by_id)  # hidden at the assistant layer; the tenant glossary itself is untouched
        self.assertEqual([t.term for t in view.hidden], ["会员号"])
        self.assertEqual(by_id["A3"].label, "assistant")
        self.assertEqual(by_id["R1"].label, "tenant_modified")  # still the tenant's version

    def test_without_inheriting_the_tenant_glossary_only_the_assistants_own_terms_remain(self) -> None:
        mine = [asst("A1", "GMV", from_id="R2", definition="ignored"), asst("A3", "工号")]
        view = assistant_terms(self.INDUSTRY, self.TENANT, mine, inherit_tenant=False)
        self.assertEqual(labels(view), {"A3": "assistant"})
        self.assertEqual(view.hidden, [])


if __name__ == "__main__":
    unittest.main()
