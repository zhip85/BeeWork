"""T-057: glossary expansion (OC-037), the rewrite service (OC-040–051) and both in the run pipeline."""

import json
import time
import unittest
from uuid import UUID

from sqlalchemy import select

import test_runtime
from beework.governance.glossary_runtime import DictEntry, expand, prompt_section, retrieval_query
from beework.models.assistants import Assistant
from beework.models.conversations import Message
from beework.models.glossary import Industry, IndustryGlossary, PlatformTerm
from beework.models.tenant_glossary import TenantGlossarySettings, TenantTerm
from beework.runtime.oc import OCConfig, RewriteConfig
from beework.runtime.rewrite import TIMEOUT_SECONDS, active_steps, build_messages, rewrite
from test_runtime import RecordingRetriever, ScriptedChat

SKU = DictEntry("SKU", ("库存单位", "商品编码"), "Stock Keeping Unit，最小库存管理单元。")
GMV = DictEntry("GMV", ("成交总额",), "")


def judge(**data) -> ScriptedChat:
    return ScriptedChat(chunks=[json.dumps(data, ensure_ascii=False)], seen_messages=[])


class ExpansionTests(unittest.TestCase):
    def test_a_mentioned_term_adds_its_other_names_as_synonyms(self) -> None:
        result = expand("这个 SKU 咋退货？", [SKU, GMV])
        self.assertEqual((result.synonyms, [m.term for m in result.matches]), (["库存单位", "商品编码"], ["SKU"]))
        self.assertEqual(retrieval_query("这个 SKU 咋退货？", result.synonyms), "这个 SKU 咋退货？ 库存单位 商品编码")

    def test_an_alias_adds_the_standard_term_and_the_other_aliases(self) -> None:
        result = expand("库存单位怎么管理", [SKU])
        self.assertEqual(result.synonyms, ["SKU", "商品编码"])  # the name already in the query is not repeated

    def test_latin_names_match_whole_words_only_and_ignore_case(self) -> None:
        self.assertEqual(expand("tasks and skus", [SKU]).synonyms, [])
        self.assertEqual(expand("what is a sku?", [SKU]).synonyms, ["库存单位", "商品编码"])
        self.assertEqual(expand("SKU123", [SKU]).synonyms, [])

    def test_chinese_names_match_as_substrings(self) -> None:
        self.assertEqual(expand("查询成交总额报表", [GMV]).synonyms, ["GMV"])

    def test_no_match_no_change_and_the_caps_hold(self) -> None:
        self.assertEqual((expand("你好", [SKU]).synonyms, expand("你好", [SKU]).matches), ([], []))
        many = [DictEntry(f"T{i}", tuple(f"a{i}_{j}" for j in range(5)), "d") for i in range(30)]
        text = " ".join(f"T{i}" for i in range(30))
        result = expand(text, many)
        self.assertLessEqual(len(result.synonyms), 20)
        self.assertLessEqual(len(result.matches), 10)

    def test_the_prompt_section_lists_definitions_and_skips_empty_matches(self) -> None:
        text = prompt_section([SKU, GMV])
        self.assertIn("- SKU（别名：库存单位、商品编码）：Stock Keeping Unit，最小库存管理单元。", text)
        self.assertIn("- GMV（别名：成交总额）", text)
        self.assertEqual(prompt_section([]), "")


class RewriteServiceTests(unittest.TestCase):
    def config(self, **overrides) -> RewriteConfig:
        return RewriteConfig.model_validate({"enabled": True, **overrides})

    def test_every_enabled_step_is_reported_only_when_it_changed_something(self) -> None:
        llm = judge(coreference="SKU 咋退货？", normalized="SKU 如何退货？")
        result = rewrite("这个咋退货？", [{"role": "user", "content": "SKU 是什么"}, {"role": "assistant", "content": "库存单位"}], self.config(), llm)
        self.assertEqual([(s["step"], s["before"], s["after"]) for s in result.steps], [("coreference", "这个咋退货？", "SKU 咋退货？"), ("normalize", "SKU 咋退货？", "SKU 如何退货？")])
        self.assertEqual((result.query, result.failure, result.used_original), ("SKU 如何退货？", None, False))
        unchanged = rewrite("如何退货", [], self.config(), judge(coreference="如何退货", normalized="如何退货"))
        self.assertEqual((unchanged.steps, unchanged.query, unchanged.used_original), ([], "如何退货", True))

    def test_the_prompt_asks_only_for_enabled_steps_and_carries_history_and_the_custom_prompt(self) -> None:
        config = self.config(
            coreference={"enabled": True, "rounds": 1}, normalize=False, decompose={"enabled": True, "max_subquestions": 4},
            translate={"enabled": True, "language": "日本語"}, custom_prompt={"enabled": True, "text": "保留产品型号原文"},
        )
        history = [{"role": "user", "content": f"问{i}"} for i in range(6)]
        system, human = build_messages("这个多少钱", history, config)
        self.assertEqual(active_steps(config), ["coreference", "decompose", "translate"])
        self.assertIn("coreference", system.content)
        self.assertNotIn("normalized", system.content)
        self.assertIn("最多 4 个", system.content)
        self.assertIn("日本語", system.content)
        self.assertTrue(system.content.endswith("保留产品型号原文"))
        self.assertIn("问5", human.content)
        self.assertNotIn("问3", human.content)  # one round = two messages of context
        self.assertIn("最新问题：这个多少钱", human.content)

    def test_decomposition_and_translation(self) -> None:
        both = self.config(normalize=False, coreference={"enabled": False}, decompose={"enabled": True, "max_subquestions": 2})
        result = rewrite("退货和换货分别怎么办？", [], both, judge(sub_questions=["如何退货", "如何换货", "多余的"]))
        self.assertEqual((result.sub_questions, result.query), (["如何退货", "如何换货"], "如何退货；如何换货"))  # capped at the maximum
        single = rewrite("退货？", [], both, judge(sub_questions=["只有一个"]))
        self.assertEqual((single.sub_questions, single.steps, single.query), ([], [], "退货？"))  # one sub-question is no decomposition
        translated = rewrite("如何退货", [], self.config(normalize=False, coreference={"enabled": False}, translate={"enabled": True}), judge(translated="How do I return an item?"))
        self.assertEqual((translated.query, translated.steps[0]["step"]), ("How do I return an item?", "translate"))

    def test_a_timeout_falls_back_to_the_original_text_quickly(self) -> None:
        slow = ScriptedChat(chunks=["{}"], seen_messages=[], before_chunk=lambda index: time.sleep(0.6))
        started = time.monotonic()
        result = rewrite("原文", [], self.config(), slow, timeout=0.2)
        self.assertLess(time.monotonic() - started, 0.5)  # the slow call is abandoned, not waited for
        self.assertEqual((result.query, result.failure, result.used_original, result.steps), ("原文", "timeout", True, []))
        self.assertEqual(TIMEOUT_SECONDS, 1.5)

    def test_model_errors_and_unusable_output_fall_back_to_the_original(self) -> None:
        failing = ScriptedChat(chunks=["x"], seen_messages=[], error=RuntimeError("boom"))
        self.assertEqual(rewrite("原文", [], self.config(), failing).failure, "error")
        for reply in ("不是 JSON", "[1, 2]", "{broken"):
            result = rewrite("原文", [], self.config(), ScriptedChat(chunks=[reply], seen_messages=[]))
            self.assertEqual((result.query, result.failure), ("原文", "invalid"), reply)

    def test_json_in_a_code_fence_or_surrounded_by_text_is_accepted(self) -> None:
        for reply in ('```json\n{"normalized": "如何退货"}\n```', '好的：{"normalized": "如何退货"} 以上。'):
            result = rewrite("咋退货", [], self.config(coreference={"enabled": False}), ScriptedChat(chunks=[reply], seen_messages=[]))
            self.assertEqual(result.query, "如何退货", reply)

    def test_nothing_to_do_makes_no_call(self) -> None:
        llm = judge(normalized="x")
        self.assertEqual(rewrite("  ", [], self.config(), llm).query, "  ")
        off = self.config(coreference={"enabled": False}, normalize=False)
        self.assertEqual(rewrite("问题", [], off, llm).steps, [])
        self.assertEqual(llm.seen_messages, [])

    def test_oc_config_defaults_and_bounds(self) -> None:
        config = OCConfig().rewrite
        self.assertEqual((config.enabled, config.only_low_confidence, config.confidence_threshold, config.coreference.rounds, config.normalize, config.decompose.enabled, config.translate.language), (False, True, 0.7, 3, True, False, "English"))
        for bad in ({"confidence_threshold": 0.2}, {"confidence_threshold": 0.96}, {"coreference": {"rounds": 0}}, {"coreference": {"rounds": 11}}, {"decompose": {"max_subquestions": 1}},
                    {"decompose": {"max_subquestions": 7}}, {"translate": {"language": "Klingon"}}, {"custom_prompt": {"enabled": True, "text": "x" * 2001}}, {"bogus": 1}):
            with self.assertRaises(ValueError, msg=bad):
                RewriteConfig.model_validate(bad)
        OCConfig.model_validate({"rewrite": {"custom_prompt": {"enabled": True, "text": "x" * 2000}}})


class RewriteRuntimeTests(test_runtime.RuntimeTests):
    def setUp(self) -> None:
        super().setUp()
        self.headers = self.login("admin@example.com")
        with self.session_factory.begin() as db:
            db.add(Industry(code="retail", name_zh="零售", name_en="Retail"))
            glossary = IndustryGlossary(name="零售电商", description="", industry_code="retail")
            db.add(glossary)
            db.flush()
            db.add(PlatformTerm(glossary_id=glossary.id, term="SKU", term_key="sku", aliases=["库存单位", "商品编码"], definition="最小库存管理单元"))
            db.add(TenantGlossarySettings(tenant_id=self.ids["tenant"], glossary_ids=[str(glossary.id)]))

    def configure(self, **oc) -> None:
        with self.session_factory.begin() as db:
            assistant = db.get(Assistant, self.ids["assistant"])
            assistant.draft_config = {**assistant.draft_config, "oc": oc}

    def ask(self, message: str, *, rewrite_llm=None, llm=None):
        llm = llm or ScriptedChat(chunks=["好的"], seen_messages=[])
        retriever = RecordingRetriever()
        run_id = self.start_run(self.headers, message).json()["run_id"]
        from beework.runtime.executor import execute_run

        with self.session_factory() as db:
            execute_run(db, UUID(run_id), llm_factory=lambda _r, _d: llm, retriever=retriever, rewrite_llm_factory=(lambda _r, _d: rewrite_llm) if rewrite_llm else None)
        return run_id, llm, retriever

    def stage_event(self, run_id: str, stage: str = "glossary_rewrite") -> dict:
        return next(e.data for e in self.events(run_id) if e.type == "step" and e.data["name"] == stage and e.data["status"] == "completed")

    def system_prompt(self, llm: ScriptedChat) -> str:
        return llm.seen_messages[0][0].content

    # -- glossary -----------------------------------------------------------------

    def test_a_mentioned_term_expands_the_retrieval_query_and_its_definition_reaches_the_prompt(self) -> None:
        run_id, llm, retriever = self.ask("这个 SKU 咋退货？")
        self.assertEqual(retriever.calls[0]["query"], "这个 SKU 咋退货？ 库存单位 商品编码")
        self.assertIn("# 术语", self.system_prompt(llm))
        self.assertIn("- SKU（别名：库存单位、商品编码）：最小库存管理单元", self.system_prompt(llm))
        event = self.stage_event(run_id)
        self.assertEqual((event["synonyms"], event["matched_terms"]), (["库存单位", "商品编码"], ["SKU"]))
        self.assertNotIn("rewritten", event)  # no rewrite happened
        self.assertEqual(self.run_row(run_id).output["query"]["matched_terms"], ["SKU"])
        with self.session_factory() as db:  # the conversation keeps the original question (OC-051)
            self.assertEqual(db.scalar(select(Message.content).where(Message.run_id == UUID(run_id), Message.role == "user")), "这个 SKU 咋退货？")

    def test_without_a_matching_term_the_query_and_prompt_are_untouched(self) -> None:
        run_id, llm, retriever = self.ask("你好")
        self.assertEqual(retriever.calls[0]["query"], "你好")
        self.assertNotIn("# 术语", self.system_prompt(llm))
        self.assertNotIn("synonyms", self.stage_event(run_id))

    def test_assistant_terms_override_and_hide_and_the_tenant_layer_counts(self) -> None:
        with self.session_factory() as db:
            sku_id = str(db.scalar(select(PlatformTerm.id)))
        with self.session_factory.begin() as db:
            db.add(TenantTerm(tenant_id=self.ids["tenant"], term="会员号", term_key="会员号", aliases=["member"], definition="会员编号"))
        self.configure(glossary={"terms": [{"id": "a1", "from_id": sku_id, "term": "SKU", "aliases": ["货号"], "definition": "助手的定义"}]})
        _, llm, retriever = self.ask("SKU 怎么查，会员号呢")
        self.assertEqual(retriever.calls[0]["query"], "SKU 怎么查，会员号呢 货号 member")  # the assistant's alias replaced the inherited ones
        self.assertIn("助手的定义", self.system_prompt(llm))
        self.assertIn("- 会员号（别名：member）：会员编号", self.system_prompt(llm))
        self.configure(glossary={"terms": [{"id": "h1", "from_id": sku_id, "hidden": True, "term": "SKU"}]})
        _, llm, retriever = self.ask("SKU 怎么查")
        self.assertEqual(retriever.calls[0]["query"], "SKU 怎么查")  # hidden at the assistant layer
        self.configure(glossary={"use_tenant": False, "terms": []})
        _, llm, retriever = self.ask("SKU 怎么查")
        self.assertEqual(retriever.calls[0]["query"], "SKU 怎么查")  # not inheriting the tenant glossary at all

    def test_the_glossary_is_read_when_the_run_starts_so_platform_edits_apply_at_once(self) -> None:
        _, _, retriever = self.ask("SKU")
        self.assertIn("库存单位", retriever.calls[0]["query"])
        with self.session_factory.begin() as db:
            db.scalar(select(PlatformTerm)).aliases = ["新别名"]
        _, _, retriever = self.ask("SKU")
        self.assertEqual(retriever.calls[0]["query"], "SKU 新别名")

    # -- rewriting ----------------------------------------------------------------

    def test_with_rewriting_off_the_question_goes_on_unchanged(self) -> None:
        rewrite_llm = judge(normalized="不应该被调用")
        _, _, retriever = self.ask("咋退货", rewrite_llm=rewrite_llm)
        self.assertEqual((retriever.calls[0]["query"], rewrite_llm.seen_messages), ("咋退货", []))

    def test_low_confidence_only_rewriting_is_left_to_routing(self) -> None:
        self.configure(rewrite={"enabled": True})  # only_low_confidence defaults to on: T-058 decides when to rewrite
        rewrite_llm = judge(normalized="如何退货")
        _, _, retriever = self.ask("咋退货", rewrite_llm=rewrite_llm)
        self.assertEqual((retriever.calls[0]["query"], rewrite_llm.seen_messages), ("咋退货", []))

    def test_an_always_on_rewrite_changes_the_query_not_the_conversation(self) -> None:
        self.configure(rewrite={"enabled": True, "only_low_confidence": False})
        rewrite_llm = judge(coreference="SKU 咋退货", normalized="SKU 如何退货")
        run_id, llm, retriever = self.ask("这个咋退货", rewrite_llm=rewrite_llm)
        self.assertEqual(retriever.calls[0]["query"], "SKU 如何退货 库存单位 商品编码")  # rewritten, then expanded
        event = self.stage_event(run_id)
        self.assertEqual((event["rewritten"], [s["step"] for s in event["rewrite_steps"]]), ("SKU 如何退货", ["coreference", "normalize"]))
        with self.session_factory() as db:
            self.assertEqual(db.scalar(select(Message.content).where(Message.run_id == UUID(run_id), Message.role == "user")), "这个咋退货")
        self.assertEqual(self.run_row(run_id).output["query"]["rewritten"], "SKU 如何退货")

    def test_a_failing_or_slow_rewrite_uses_the_original_question_and_the_run_still_answers(self) -> None:
        self.configure(rewrite={"enabled": True, "only_low_confidence": False})
        broken = ScriptedChat(chunks=["x"], seen_messages=[], error=RuntimeError("gateway down"))
        run_id, _llm, retriever = self.ask("咋退货", rewrite_llm=broken)
        self.assertEqual(retriever.calls[0]["query"], "咋退货")
        self.assertEqual((self.run_row(run_id).status, self.stage_event(run_id)["rewrite_failure"]), ("succeeded", "error"))
        garbage = ScriptedChat(chunks=["不是 JSON"], seen_messages=[])
        run_id, _llm, retriever = self.ask("咋退货", rewrite_llm=garbage)
        self.assertEqual((retriever.calls[0]["query"], self.stage_event(run_id)["rewrite_failure"]), ("咋退货", "invalid"))

    def test_a_missing_gateway_key_only_costs_the_rewrite(self) -> None:
        self.configure(rewrite={"enabled": True, "only_low_confidence": False})
        run_id, _llm, retriever = self.ask("咋退货")  # no rewrite_llm_factory: the real gateway model cannot be built in tests
        self.assertEqual((self.run_row(run_id).status, retriever.calls[0]["query"]), ("succeeded", "咋退货"))
        self.assertEqual(self.stage_event(run_id)["rewrite_failure"], "error")

    def test_a_blocked_input_skips_the_glossary_and_rewrite(self) -> None:
        from beework.models.governance import PlatformPiiRule

        with self.session_factory.begin() as db:
            db.add(PlatformPiiRule(name="禁词", match_type="keyword", pattern="违禁", stage="input", action="block", enabled=True))
        self.configure(rewrite={"enabled": True, "only_low_confidence": False})
        rewrite_llm = judge(normalized="x")
        _, llm, retriever = self.ask("违禁 SKU", rewrite_llm=rewrite_llm)
        self.assertEqual((rewrite_llm.seen_messages, retriever.calls, llm.seen_messages), ([], [], []))

    # -- API ----------------------------------------------------------------------

    def rewrite_test(self, body: dict, headers=None, assistant_id=None):
        from unittest import mock

        from beework.api import assistant_oc_glossary as api

        llm = body.pop("_llm", None)
        with mock.patch.object(api, "tenant_gateway_key", return_value="sk-test"), mock.patch.object(api, "REWRITE_MODEL_FACTORY", (lambda *_a: llm) if llm else None):
            return self.client.post(f"/api/v1/assistants/{assistant_id or self.ids['assistant']}/oc/rewrite-test", json=body, headers=headers or self.headers)

    def test_rewrite_test_returns_only_the_changed_steps_for_the_prd_example(self) -> None:
        config = {"enabled": True, "only_low_confidence": False}
        llm = judge(coreference="这个 SKU 咋退货？", normalized="这个 SKU 如何退货？")
        response = self.rewrite_test({"question": "这个 SKU 咋退货？", "rewrite": config, "_llm": llm})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual([s["step"] for s in body["steps"]], ["normalize", "alias_expansion"])  # coreference changed nothing
        self.assertEqual((body["query"], body["synonyms"], body["failure"]), ("这个 SKU 如何退货？", ["库存单位", "商品编码"], None))
        self.assertEqual(body["steps"][1]["after"], "这个 SKU 如何退货？ 库存单位 商品编码")

    def test_rewrite_test_with_rewriting_off_still_shows_alias_expansion_and_calls_no_model(self) -> None:
        body = self.rewrite_test({"question": "SKU 怎么退"}).json()  # no model factory: a call would fail
        self.assertEqual(([s["step"] for s in body["steps"]], body["query"]), (["alias_expansion"], "SKU 怎么退"))

    def test_rewrite_test_failure_reports_it_and_keeps_the_question(self) -> None:
        broken = ScriptedChat(chunks=["x"], seen_messages=[], error=RuntimeError("down"))
        body = self.rewrite_test({"question": "咋退货", "rewrite": {"enabled": True}, "_llm": broken}).json()
        self.assertEqual((body["failure"], body["query"], body["steps"]), ("error", "咋退货", []))

    def test_rewrite_test_permissions_validation_and_tenant_isolation(self) -> None:
        self.assertEqual(self.rewrite_test({"question": ""}).status_code, 422)
        self.assertEqual(self.rewrite_test({"question": "x", "rewrite": {"confidence_threshold": 0.1}}).status_code, 422)
        viewer = self.login("viewer@example.com")  # logging in replaces the session cookie: each role is exercised last
        self.assertEqual(self.rewrite_test({"question": "x"}, headers=viewer).status_code, 403)
        other = self.login("other@example.com", self.ids["other"])
        self.assertEqual(self.rewrite_test({"question": "x"}, headers=other).status_code, 404)

    def test_saving_a_rewrite_model_that_is_not_enabled_is_rejected(self) -> None:
        url = f"/api/v1/assistants/{self.ids['assistant']}/oc"
        state = self.client.get(url, headers=self.headers).json()
        oc = {**state["oc"], "rewrite": {**state["oc"]["rewrite"], "model_id": "00000000-0000-0000-0000-000000000001"}}
        response = self.client.put(url, json={"oc": oc, "row_version": state["row_version"]}, headers=self.headers)
        self.assertEqual((response.status_code, response.json()["code"]), (422, "rewrite.model_unavailable"))

    def test_glossary_preview_lists_effective_terms_with_labels(self) -> None:
        url = f"/api/v1/assistants/{self.ids['assistant']}/oc/glossary/preview"
        body = self.client.post(url, json={"glossary": {}}, headers=self.headers).json()
        self.assertEqual([(i["term"], i["label"]) for i in body["items"]], [("SKU", "industry_inherited")])
        self.assertEqual((body["chain"]["industries"], body["chain"]["effective"]), (["零售"], 1))
        sku = body["items"][0]["key"]
        edited = self.client.post(url, json={"glossary": {"terms": [{"id": "a", "from_id": sku, "term": "SKU", "aliases": ["货号"]}]}}, headers=self.headers).json()
        self.assertEqual((edited["items"][0]["label"], edited["items"][0]["restorable"]), ("assistant_modified", True))
        hidden = self.client.post(url, json={"glossary": {"terms": [{"id": "h", "from_id": sku, "hidden": True, "term": "SKU"}]}}, headers=self.headers).json()
        self.assertEqual((hidden["items"], [h["term"] for h in hidden["hidden"]]), ([], ["SKU"]))
        self.assertEqual(self.client.post(url, json={"glossary": {}}, headers=self.login("other@example.com", self.ids["other"])).status_code, 404)


# Reuse the harness only: the inherited tests already run in test_runtime and must not run a second time here.
for _name in dir(test_runtime.RuntimeTests):
    if _name.startswith("test_") and _name not in RewriteRuntimeTests.__dict__:
        setattr(RewriteRuntimeTests, _name, None)

if __name__ == "__main__":
    unittest.main()
