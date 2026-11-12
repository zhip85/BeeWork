"""T-161: the L2 intent decision — pure functions and a scripted fake model; no database."""

import json
import time
import unittest
from types import SimpleNamespace

from beework.runtime import intent
from beework.runtime.intent import (
    Candidate,
    ClassifyResult,
    LlmJsonClassifier,
    Scored,
    decide,
    extract_arguments,
    parse_classification,
)
from beework.runtime.oc import IntentConfig

POLICY = Candidate("e1", "mcp_tool", "search_policy", "Search the refund policy", "用户问退款政策时用", ("退货要几天",), direct=True, input_schema={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]})
TICKET = Candidate("e2", "api_tool", "create_ticket", "Open a ticket", direct=True, writes=True, input_schema={"type": "object"})
ORDER = Candidate("e3", "api_tool", "get_order", "Get an order", direct=True, input_schema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]})
LEAF = Candidate("e4", "agent", "Leaf agent", "退货专家", direct=False, input_schema={"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]})
KB = Candidate("e5", "kb", "退货知识库", "制度", direct=False, input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})
FLOW = Candidate("e6", "dify_chatflow", "售后流程", "售后", direct=True, input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})
ALL = [POLICY, TICKET, ORDER, LEAF, KB, FLOW]


def config(**extra) -> IntentConfig:
    return IntentConfig.model_validate({"enabled": True, **extra})


def rule(rid: str, pattern: str, target: Candidate | None = POLICY, *, priority: int = 10, match_type: str = "keyword", enabled: bool = True, created_at: str = "2026-01-01") -> dict:
    return {"id": rid, "name": f"规则 {rid}", "priority": priority, "match_type": match_type, "pattern": pattern, "enabled": enabled, "target_entry_id": target.id if target else None, "created_at": created_at}


def scored(*items: tuple[Candidate, float], failure: str | None = None):
    return lambda text: ClassifyResult(scored=[Scored(c.id, v, "because") for c, v in items], failure=failure, ms=7)


class FakeLLM:
    def __init__(self, reply="", *, delay: float = 0.0, error: Exception | None = None) -> None:
        self.reply, self.delay, self.error, self.calls = reply, delay, error, []

    def invoke(self, messages, config=None):
        self.calls.append(messages)
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return SimpleNamespace(content=self.reply if isinstance(self.reply, str) else json.dumps(self.reply))


class RuleTests(unittest.TestCase):
    def test_the_smallest_priority_wins_and_equal_priorities_go_by_creation_time(self) -> None:
        cfg = config(priority_rules_enabled=True, rules=[rule("late", "退款", ORDER, priority=20), rule("first", "退款", POLICY, priority=10),
                                                      rule("b", "发票", FLOW, priority=30, created_at="2026-02-01"), rule("a", "发票", ORDER, priority=30, created_at="2026-01-01")])
        refund = decide(text="我要退款", candidates=ALL, config=cfg, classify=None)
        self.assertEqual((refund.path, refund.source, refund.rule_id, refund.target.id, refund.confidence), ("A", "rule", "first", "e1", 1.0))
        self.assertEqual(decide(text="开发票", candidates=ALL, config=cfg, classify=None).rule_id, "a")

    def test_disabled_rules_and_rules_whose_target_is_not_callable_are_skipped(self) -> None:
        cfg = config(priority_rules_enabled=True, rules=[rule("off", "退款", POLICY, priority=1, enabled=False), rule("gone", "退款", Candidate("zz", "mcp_tool", "x"), priority=2),
                                                      rule("write", "退款", TICKET, priority=3), rule("notdirect", "退款", LEAF, priority=4), rule("none", "退款", None, priority=5), rule("ok", "退款", ORDER, priority=6)])
        self.assertEqual(decide(text="退款", candidates=ALL, config=cfg, classify=None).rule_id, "ok")

    def test_rules_are_ignored_while_the_switch_is_off_and_a_hit_does_not_call_the_classifier(self) -> None:
        off = config(priority_rules_enabled=False, rules=[rule("r", "退款")])
        self.assertEqual(decide(text="退款", candidates=ALL, config=off, classify=scored((ORDER, 0.9))).source, "classifier")
        calls = []
        on = config(priority_rules_enabled=True, rules=[rule("r", "退款")])
        decide(text="退款", candidates=ALL, config=on, classify=lambda t: calls.append(t) or ClassifyResult())
        self.assertEqual(calls, [])

    def test_keywords_ignore_case_accept_both_commas_and_a_regex_matches_and_a_slow_one_does_not(self) -> None:
        r = config(rules=[rule("k", "Refund，退货, 退钱")]).rules[0]
        for text, expected in (("I want a REFUND", True), ("我要退货", True), ("发票", False), ("", False)):
            self.assertEqual(intent.rule_matches(r, text), expected, text)
        rx = config(rules=[rule("r", r"退(款|货).*(订单|单号)", match_type="regex")]).rules[0]
        self.assertEqual((intent.rule_matches(rx, "我想退货，订单 123"), intent.rule_matches(rx, "我想退货")), (True, False))
        evil = config(rules=[rule("e", "(a+)+$", match_type="regex")]).rules[0]
        started = time.monotonic()
        self.assertFalse(intent.rule_matches(evil, "a" * 40 + "!"))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_invalid_rules_and_settings_are_refused_by_the_model(self) -> None:
        for bad in ({"rules": [rule("r", "(", match_type="regex")]}, {"rules": [rule("r", " , ，")]}, {"rules": [rule("a", "x"), rule("a", "y")]},
                    {"threshold": 0.2}, {"threshold": 0.99}, {"margin": 0.6}, {"recall_k": 2}, {"recall_k": 21}):
            with self.assertRaises(ValueError):
                config(**bad)


class DecisionTests(unittest.TestCase):
    def test_a_confident_and_clear_top_candidate_goes_straight_to_it(self) -> None:
        d = decide(text="x", candidates=ALL, config=config(threshold=0.7, margin=0.15), classify=scored((POLICY, 0.9), (ORDER, 0.5)))
        self.assertEqual((d.path, d.source, d.target.id, d.confidence, d.second, d.classifier_ms), ("A", "classifier", "e1", 0.9, 0.5, 7))

    def test_the_threshold_and_the_margin_boundaries(self) -> None:
        cfg = config(threshold=0.7, margin=0.15)
        self.assertEqual(decide(text="x", candidates=ALL, config=cfg, classify=scored((POLICY, 0.7))).path, "A")  # at the threshold
        low = decide(text="x", candidates=ALL, config=cfg, classify=scored((POLICY, 0.69)))
        self.assertEqual((low.path, low.handoff), ("B", "low_confidence"))
        self.assertEqual(decide(text="x", candidates=ALL, config=cfg, classify=scored((POLICY, 0.9), (ORDER, 0.75))).path, "A")  # leads by exactly the margin
        close = decide(text="x", candidates=ALL, config=cfg, classify=scored((POLICY, 0.9), (ORDER, 0.80)))
        self.assertEqual((close.path, close.handoff, close.target.id), ("B", "ambiguous", "e1"))  # the best one still goes along as a hint

    def test_a_write_or_a_capability_that_may_not_be_called_directly_goes_to_l3_with_the_hint(self) -> None:
        write = decide(text="x", candidates=ALL, config=config(), classify=scored((TICKET, 0.95)))
        self.assertEqual((write.path, write.handoff, write.target.id), ("B", "write", "e2"))
        sub = decide(text="x", candidates=ALL, config=config(), classify=scored((LEAF, 0.95)))
        self.assertEqual((sub.path, sub.handoff), ("B", "not_direct"))  # a sub-agent is delegated to by L3 unless it was made direct
        made_direct = Candidate("e4", "agent", "Leaf agent", direct=True)
        self.assertEqual(decide(text="x", candidates=[made_direct], config=config(), classify=scored((made_direct, 0.95))).path, "A")

    def test_a_knowledge_base_is_a_path_a_target_only_for_faq_answers_and_only_when_that_is_on(self) -> None:
        on = decide(text="x", candidates=ALL, config=config(faq_direct=True), classify=scored((KB, 0.9)))
        self.assertEqual((on.path, on.target.kind), ("A", "kb"))  # the stage then looks for a confident FAQ hit, and falls to L3 without one
        off = decide(text="x", candidates=ALL, config=config(faq_direct=False), classify=scored((KB, 0.9)))
        self.assertEqual((off.path, off.handoff), ("B", "not_direct"))

    def test_no_candidates_a_failing_classifier_or_an_empty_answer_all_mean_l3_without_an_error(self) -> None:
        self.assertEqual(decide(text="x", candidates=[], config=config(), classify=scored((POLICY, 0.9))).handoff, "no_candidates")
        for failure in ("timeout", "error", "invalid"):
            d = decide(text="x", candidates=ALL, config=config(), classify=scored(failure=failure))
            self.assertEqual((d.path, d.handoff, d.failure), ("B", "classifier_failed", failure))
        self.assertEqual(decide(text="x", candidates=ALL, config=config(), classify=None).handoff, "classifier_failed")
        self.assertEqual(decide(text="x", candidates=ALL, config=config(), classify=scored()).handoff, "low_confidence")

    def test_a_conversation_waiting_on_a_flow_sends_the_message_back_before_anything_else(self) -> None:
        cfg = config(priority_rules_enabled=True, rules=[rule("r", "退款")])
        d = decide(text="退款", candidates=ALL, config=cfg, classify=scored((ORDER, 0.99)), pending=FLOW)
        self.assertEqual((d.path, d.source, d.target.id), ("A", "pending", "e6"))


class ClassifierTests(unittest.TestCase):
    def test_it_asks_the_model_once_with_every_candidate_their_intent_and_the_custom_prompt(self) -> None:
        llm = FakeLLM({"candidates": [{"id": "e1", "confidence": 0.82, "reason": "退款"}]})
        classifier = LlmJsonClassifier(llm, config(custom_prompt={"enabled": True, "text": "涉及发票的优先订单"}))
        result = classifier.classify("退货要几天", [{"role": "user", "content": "你好"}], ALL)
        self.assertEqual(([(s.id, s.confidence) for s in result.scored], result.failure), ([("e1", 0.82)], None))
        system = llm.calls[0][0].content
        self.assertTrue(all(c.id in system and c.name in system for c in ALL))
        self.assertIn("用户问退款政策时用", system)  # the intent description
        self.assertIn("退货要几天", system)  # and an example phrasing
        self.assertIn("涉及发票的优先订单", system)
        self.assertNotIn("涉及发票", llm.calls[0][1].content)  # the custom text is part of the instructions only

    def test_invented_ids_duplicates_and_garbage_are_dropped_and_confidence_is_clamped(self) -> None:
        reply = json.dumps({"candidates": [{"id": "forged", "confidence": 0.99}, {"id": "e2", "confidence": 7}, {"id": "e2", "confidence": 0.1}, {"id": "e1", "confidence": "high"}, "nonsense", {"id": 5}]})
        result = parse_classification(reply, ALL)
        self.assertEqual([(s.id, s.confidence) for s in result.scored], [("e2", 1.0), ("e1", 0.0)])
        fenced = "```json\n" + json.dumps({"candidates": [{"id": "e3", "confidence": 0.7}]}) + "\n```"
        self.assertEqual(parse_classification(fenced, ALL).scored[0].id, "e3")
        for bad in ("not json", "{}", json.dumps({"candidates": "x"})):
            self.assertEqual(parse_classification(bad, ALL).failure, "invalid", bad)

    def test_at_most_three_are_kept_best_first(self) -> None:
        reply = {"candidates": [{"id": c.id, "confidence": v} for c, v in ((POLICY, 0.2), (ORDER, 0.9), (FLOW, 0.5), (KB, 0.4))]}
        self.assertEqual([s.id for s in parse_classification(json.dumps(reply), ALL).scored], ["e3", "e6", "e5"])

    def test_timeouts_and_errors_are_results_not_exceptions(self) -> None:
        self.assertEqual(LlmJsonClassifier(FakeLLM("{}", delay=0.4), config(), timeout=0.05).classify("x", [], ALL).failure, "timeout")
        self.assertEqual(LlmJsonClassifier(FakeLLM(error=RuntimeError("down")), config()).classify("x", [], ALL).failure, "error")


class SlotTests(unittest.TestCase):
    def test_arguments_that_need_no_model(self) -> None:
        self.assertEqual(extract_arguments(FakeLLM(), LEAF, "问题", []).args, {"task": "问题"})
        self.assertEqual(extract_arguments(FakeLLM(), FLOW, "问题", []).args, {"query": "问题"})
        skill = Candidate("s", "skill", "Skill")
        self.assertEqual((extract_arguments(FakeLLM(), skill, "x", []).status, extract_arguments(FakeLLM(), skill, "x", []).args), ("ok", {}))
        wf = Candidate("w", "dify_workflow", "WF", input_schema={"type": "object", "properties": {"inputs": {"type": "object"}}})
        self.assertEqual(extract_arguments(FakeLLM(), wf, "问题", []).args, {"inputs": {"query": "问题"}})
        llm = FakeLLM()
        extract_arguments(llm, LEAF, "x", [])
        self.assertEqual(llm.calls, [])  # no call was made

    def test_a_model_reads_the_arguments_and_unknown_keys_and_empty_values_are_dropped(self) -> None:
        llm = FakeLLM({"id": "A-1", "invented": 1, "note": ""})
        slots = extract_arguments(llm, ORDER, "订单 A-1 到哪了", [])
        self.assertEqual((slots.status, slots.args), ("ok", {"id": "A-1"}))
        self.assertIn("get_order", llm.calls[0][0].content)

    def test_a_missing_required_argument_is_reported_not_guessed(self) -> None:
        slots = extract_arguments(FakeLLM({}), ORDER, "我的订单呢", [])
        self.assertEqual((slots.status, slots.missing), ("missing", ["id"]))

    def test_a_value_of_the_wrong_type_is_invalid(self) -> None:
        slots = extract_arguments(FakeLLM({"id": 123}), ORDER, "订单 123", [])
        self.assertEqual(slots.status, "invalid")
        self.assertTrue(slots.problems)

    def test_a_failing_extractor_is_a_result_too(self) -> None:
        self.assertEqual(extract_arguments(FakeLLM(error=RuntimeError("x")), ORDER, "q", []).status, "failed")
        self.assertEqual(extract_arguments(FakeLLM("not json"), ORDER, "q", []).status, "failed")
        self.assertEqual(extract_arguments(FakeLLM("{}", delay=0.4), ORDER, "q", [], timeout=0.05).status, "failed")


if __name__ == "__main__":
    unittest.main()
