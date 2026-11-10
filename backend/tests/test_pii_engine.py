"""T-055: detectors and the scan engine — priorities, masking format, failure policy, regex time limits, performance."""

import json
import statistics
import time
import unittest
from unittest import mock

import httpx

from beework.config import settings
from beework.governance.detectors import (
    REGISTRY,
    DetectorUnavailable,
    KeywordDetector,
    LlmJudgeDetector,
    PresidioDetector,
    RegexDetector,
)
from beework.governance.pii_engine import (
    DEFAULT_BLOCK_MESSAGE,
    JUDGE_PLACEHOLDER,
    PolicyRule,
    mask_text,
    redact_for_logs,
    scan,
)
from beework.pii import make_redactor

ID_NUMBER = "110101199003074518"


def rule(rule_id: str, pattern: str, *, action: str = "mask", stage: str = "both", match_type: str = "regex", message: str = "", **extra) -> PolicyRule:
    return PolicyRule(
        id=rule_id, name=rule_id, layer=extra.pop("layer", "platform"), source="pii", category="pii", match_type=match_type,
        pattern=pattern, stage=stage, action=action, block_message=message, enforced=extra.pop("enforced", False),
    )


class MaskTests(unittest.TestCase):
    def test_the_first_and_last_characters_are_kept_and_the_middle_is_starred(self) -> None:
        self.assertEqual(mask_text(ID_NUMBER), "110" + "*" * 11 + "4518")  # 18 characters: 3 + 11 + 4
        self.assertEqual(mask_text("13812345678"), "138****5678")  # a mobile number keeps the usual 3 + 4
        self.assertEqual(mask_text("AC12345678"), "AC******78")  # 8–10 characters keep 2 + 2
        self.assertEqual(mask_text("E1234"), "E***4")
        self.assertEqual(mask_text("ab"), "**")
        self.assertEqual(mask_text(""), "")
        for value in ("a" * 5, ID_NUMBER, "x" * 40):
            self.assertEqual(len(mask_text(value)), len(value))  # the length never changes


class ScanTests(unittest.TestCase):
    PHONE = rule("phone", r"(?<!\d)1[3-9]\d{9}(?!\d)")
    ID = rule("id", r"\d{17}[\dXx]")

    def test_mask_replaces_every_match_and_records_a_hit_without_the_text(self) -> None:
        result = scan(f"我的手机 13812345678，身份证 {ID_NUMBER}。", [self.PHONE, self.ID], "input")
        self.assertEqual(result.text, "我的手机 138****5678，身份证 110***********4518。")
        self.assertFalse(result.blocked)
        self.assertEqual([(h.rule_id, h.action, h.stage, h.count, h.layer) for h in result.hits], [("phone", "mask", "input", 1, "platform"), ("id", "mask", "input", 1, "platform")])
        for hit in result.hits:
            self.assertNotIn("13812345678", json.dumps(hit.as_event()))
            self.assertNotIn(ID_NUMBER, json.dumps(hit.as_event()))

    def test_block_beats_mask_beats_flag(self) -> None:
        text = "AB123 13812345678 secret"
        flag = rule("flag", "secret", action="flag", match_type="keyword")
        mask = self.PHONE
        block = rule("block", "AB123", action="block", match_type="keyword", message="请勿发送工单号")
        self.assertEqual(scan(text, [flag], "input").text, text)  # a flag only records
        self.assertEqual(scan(text, [flag, mask], "input").text, "AB123 138****5678 secret")
        blocked = scan(text, [flag, mask, block], "input")
        self.assertEqual((blocked.blocked, blocked.message, blocked.text), (True, "请勿发送工单号", text))
        self.assertEqual({h.action for h in blocked.hits}, {"flag", "mask", "block"})  # every hit is still recorded
        self.assertEqual(scan(text, [rule("b2", "AB123", action="block", match_type="keyword")], "input").message, DEFAULT_BLOCK_MESSAGE)

    def test_rules_only_run_in_their_stage(self) -> None:
        only_input = rule("i", r"\d+", stage="input")
        only_output = rule("o", r"\d+", stage="output")
        self.assertEqual(scan("a1", [only_input, only_output], "input").hits[0].rule_id, "i")
        self.assertEqual(scan("a1", [only_input, only_output], "output").hits[0].rule_id, "o")
        self.assertEqual(len(scan("a1", [rule("b", r"\d+", stage="both")], "output").hits), 1)

    def test_overlapping_matches_merge_into_one_mask(self) -> None:
        result = scan("123456789012345", [rule("a", r"\d{10}"), rule("b", r"\d{12}")], "input")
        self.assertEqual(result.text, mask_text("123456789012") + "345")

    def test_empty_text_and_no_rules(self) -> None:
        self.assertEqual(scan("", [self.PHONE], "input").text, "")
        self.assertEqual(scan("13812345678", [], "input").text, "13812345678")

    def test_long_text_is_scanned_in_windows_so_late_matches_are_found(self) -> None:
        text = "x" * 45_000 + " 13812345678 " + "y" * 100
        self.assertIn("138****5678", scan(text, [self.PHONE], "output").text)
        straddling = "z" * 19_995 + "13812345678" + "z" * 50  # a match across a window boundary
        self.assertIn("138****5678", scan(straddling, [self.PHONE], "output").text)

    def test_unsupported_match_types_are_recorded_as_failures(self) -> None:
        result = scan("text", [rule("w", "x", match_type="weird")], "input")
        self.assertEqual((result.text, result.failures[0]["reason"]), ("text", "unsupported"))


class DetectorTests(unittest.TestCase):
    def test_keywords_match_ignoring_case_and_find_every_occurrence(self) -> None:
        found = KeywordDetector().find("Secret and SECRET and secrets", "keyword", "secret, 绝密")
        self.assertEqual([(m.start, m.end) for m in found], [(0, 6), (11, 17), (22, 28)])
        self.assertEqual(KeywordDetector().find("abc", "keyword", " , "), [])

    def test_a_catastrophic_regex_is_cut_off_and_reported_not_hung(self) -> None:
        started = time.monotonic()
        with self.assertRaises(DetectorUnavailable) as caught:
            RegexDetector().find("a" * 60 + "!", "regex", r"(a|aa)+$")
        self.assertEqual(caught.exception.reason, "timeout")
        self.assertLess(time.monotonic() - started, 1.0)
        result = scan("a" * 60 + "!", [rule("bad", r"(a|aa)+$")], "input")  # fail open: the text goes through, the failure is recorded
        self.assertEqual((result.text, result.blocked, result.failures), ("a" * 60 + "!", False, [{"rule_id": "bad", "name": "bad", "reason": "timeout"}]))

    def test_invalid_and_oversized_patterns_are_failures(self) -> None:
        for pattern in ("([", "", "a" * 1001):
            with self.assertRaises(DetectorUnavailable) as caught:
                RegexDetector().find("text", "regex", pattern)
            self.assertEqual(caught.exception.reason, "invalid_pattern", pattern[:10])

    def test_presidio_returns_spans_and_sends_the_entities(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content), path=request.url.path)
            return httpx.Response(200, json=[{"start": 4, "end": 9, "entity_type": "PERSON", "score": 0.9}])

        detector = PresidioDetector("http://presidio", httpx.MockTransport(handler))
        found = detector.find("Hi, Alice is here", "classifier", "person, location")
        self.assertEqual([(m.start, m.end) for m in found], [(4, 9)])
        self.assertEqual((seen["path"], seen["entities"], seen["language"]), ("/analyze", ["PERSON", "LOCATION"], settings.presidio_language))
        masked = scan("Hi, Alice is here", [rule("p", "PERSON", match_type="classifier")], "input", detectors={"classifier": detector})
        self.assertEqual(masked.text, "Hi, " + mask_text("Alice") + " is here")

    def test_presidio_failures_never_block_the_text(self) -> None:
        for transport, reason in (
            (httpx.MockTransport(lambda request: httpx.Response(500)), "failed"),
            (httpx.MockTransport(lambda request: httpx.Response(200, text="not json")), "failed"),
            (httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow"))), "timeout"),
        ):
            result = scan("Alice", [rule("p", "PERSON", match_type="classifier", action="block")], "input", detectors={"classifier": PresidioDetector("http://presidio", transport)})
            self.assertEqual((result.blocked, result.text, result.failures[0]["reason"]), (False, "Alice", reason))

    def test_presidio_without_a_url_is_reported_as_not_configured(self) -> None:
        with mock.patch.object(settings, "presidio_analyzer_url", ""):
            result = scan("Alice", [rule("p", "PERSON", match_type="classifier")], "input")
        self.assertEqual((result.text, result.failures[0]["reason"]), ("Alice", "not_configured"))

    def configured_judge(self, handler):
        patches = (
            mock.patch.object(settings, "safety_judge_model", "judge"), mock.patch.object(settings, "litellm_base_url", "http://gateway"),
            mock.patch.object(settings, "litellm_master_key", "key"),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return LlmJudgeDetector(httpx.MockTransport(handler))

    def test_the_llm_judge_flags_the_whole_text_and_masking_it_replaces_it(self) -> None:
        asked = {}

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            asked.update(model=body["model"], prompt=body["messages"][1]["content"], auth=request.headers["Authorization"])
            return httpx.Response(200, json={"choices": [{"message": {"content": "YES"}}]})

        detector = self.configured_judge(handler)
        judged = rule("j", "是否披露健康隐私", match_type="llm_judge", action="mask")
        result = scan("我患有糖尿病", [judged], "output", detectors={"llm_judge": detector})
        self.assertEqual((result.text, asked["model"], asked["auth"]), (JUDGE_PLACEHOLDER, "judge", "Bearer key"))
        self.assertIn("是否披露健康隐私", asked["prompt"])
        no = self.configured_judge(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "NO"}}]}))
        self.assertEqual(scan("你好", [judged], "output", detectors={"llm_judge": no}).text, "你好")

    def test_the_llm_judge_times_out_or_is_unconfigured_without_stopping_the_text(self) -> None:
        slow = self.configured_judge(lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow")))
        result = scan("x", [rule("j", "criteria", match_type="llm_judge", action="block")], "input", detectors={"llm_judge": slow})
        self.assertEqual((result.blocked, result.failures[0]["reason"]), (False, "timeout"))
        with mock.patch.object(settings, "safety_judge_model", ""):
            self.assertEqual(scan("x", [rule("j", "c", match_type="llm_judge")], "input").failures[0]["reason"], "not_configured")

    def test_the_registry_covers_the_four_match_types(self) -> None:
        self.assertEqual(set(REGISTRY), {"regex", "keyword", "classifier", "llm_judge"})


class LogRedactionTests(unittest.TestCase):
    def test_logs_use_the_rules_and_ignore_external_detectors_and_flags(self) -> None:
        rules = [rule("id", r"\d{17}[\dXx]"), rule("kw", "绝密", match_type="keyword", action="block"), rule("flag", "hello", action="flag"), rule("p", "PERSON", match_type="classifier")]
        text = f"hello 绝密 {ID_NUMBER}"
        self.assertEqual(redact_for_logs(text, rules), "hello " + mask_text("绝密") + " " + mask_text(ID_NUMBER))
        self.assertEqual(redact_for_logs(text, [rules[2]]), text)
        self.assertEqual(redact_for_logs("", rules), "")

    def test_the_fixed_patterns_are_the_floor_when_no_masking_rule_exists(self) -> None:
        self.assertEqual(make_redactor([])("联系 13812345678"), "联系 [手机号]")
        flag_only = [rule("f", "x", action="flag")]
        self.assertEqual(make_redactor(flag_only)("13812345678"), "[手机号]")
        custom = make_redactor([rule("id", r"\d{17}[\dXx]")])
        self.assertEqual(custom("13812345678 " + ID_NUMBER), "13812345678 " + mask_text(ID_NUMBER))


class PerformanceTests(unittest.TestCase):
    def test_100_rules_stay_well_below_50_ms_at_p95(self) -> None:
        """NFR-001: the PII stage with 100 in-process rules on a ~1 KB message (external detectors excluded)."""
        rules = [rule(f"r{i}", rf"1[3-9]\d{{3}}{i % 10}\d{{5}}") for i in range(60)] + [
            rule(f"k{i}", f"敏感词{i},forbidden{i}", match_type="keyword", action="flag") for i in range(40)
        ]
        text = ("您好，我想咨询一下订单的退换货政策以及发票开具的流程。" * 25) + " 手机 13812345678 邮箱 a@b.com 身份证 " + ID_NUMBER
        scan(text, rules, "input")  # warm the pattern cache
        timings = []
        for _ in range(300):
            started = time.perf_counter()
            scan(text, rules, "input")
            timings.append((time.perf_counter() - started) * 1000)
        timings.sort()
        p50, p95, worst = statistics.median(timings), timings[int(len(timings) * 0.95)], timings[-1]
        print(f"\nPII stage, 100 rules, {len(text)} chars: p50={p50:.2f} ms p95={p95:.2f} ms max={worst:.2f} ms")
        self.assertLess(p95, 50)


if __name__ == "__main__":
    unittest.main()
