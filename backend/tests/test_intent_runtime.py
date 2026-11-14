"""T-161: the route stage in the run pipeline — path A (direct result, composed result, FAQ), path B (hints), failures, the trace."""

import json
from unittest.mock import patch
from uuid import UUID

import test_rewrite
from beework.runtime.intent import Candidate, ClassifyResult, Scored, Slots
from beework.runtime.route_stage import FaqAnswer, IntentRuntime
from beework.tools.executor import ToolResult
from test_runtime import ScriptedChat

ORDER = Candidate("e-order", "api_tool", "get_order", "查询订单", "用户查订单状态", direct=True, tool="api:get_order", input_schema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]})
SHOUT = Candidate("e-shout", "mcp_tool", "shout", "原样返回", "用户要求播报", direct=True, direct_output=True, tool="mcp:shout")
FAQ_KB = Candidate("e-kb", "kb", "售后知识库", "售后制度", direct=True, source_id="kb-1", input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})
FLOW = Candidate("e-flow", "dify_chatflow", "售后流程", "多轮售后", "用户要办售后", direct=True, tool="connection:flow-1", input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})
SECRET = "13800138000"


class Fake:
    """A scripted IntentRuntime: records what the stage asked of it."""

    def __init__(self, scored=(), *, failure=None, slots=None, result=None, faq=None, candidates=(ORDER, SHOUT, FAQ_KB)) -> None:
        self.scored, self.failure = list(scored), failure
        self.slots = slots or Slots("ok", args={"id": "A1"})
        self.result = result or ToolResult("ok", "订单 A1：已发货")
        self.faq_answer = faq
        self.found = list(candidates)
        self.invoked: list[tuple[str, dict]] = []
        self.classified: list[str] = []
        self.faq_asked: list[str] = []
        self.pending = None

    def runtime(self) -> IntentRuntime:
        def classify(query, history, found):
            self.classified.append(query)
            return ClassifyResult(scored=[Scored(c.id, v) for c, v in self.scored], failure=self.failure, ms=5)

        def invoke(target, args, stream=None):
            self.invoked.append((target.name, args))
            return self.result

        return IntentRuntime(
            callable_by_id={c.id: c for c in self.found if c.direct and not c.writes}, candidates=lambda q: self.found, classify=classify,
            extract=lambda target, q, h: self.slots, invoke=invoke, faq=self._faq, pending=lambda: self.pending,
            faq_bases=[c for c in self.found if c.kind == "kb"],
        )

    def _faq(self, target, query):
        self.faq_asked.append(query)
        return self.faq_answer


class IntentRuntimeTests(test_rewrite.RewriteRuntimeTests):
    def route(self, fake: Fake, message: str, **intent):
        self.configure(intent={"enabled": True, **intent})
        llm = ScriptedChat(chunks=["智能体的回答"], seen_messages=[])
        with patch("beework.runtime.executor._intent_runtime", return_value=fake.runtime()):
            run_id, llm, retriever = self.ask(message, llm=llm)
        return run_id, llm, retriever, self.run_row(run_id).output

    def test_a_confident_direct_output_answers_without_the_agent_model(self) -> None:
        fake = Fake([(SHOUT, 0.95)], result=ToolResult("ok", " 播报内容 "))
        run_id, llm, retriever, output = self.route(fake, "请播报")
        self.assertEqual((output["route"]["path"], output["route"]["source"], output["route"]["target"]["name"]), ("A", "classifier", "shout"))
        self.assertEqual((llm.seen_messages, retriever.calls, fake.invoked), ([], [], [("shout", {"id": "A1"})]))
        self.assertEqual(output["text"] if "text" in output else self.final_text(run_id), "播报内容")

    def final_text(self, run_id: str) -> str:
        from sqlalchemy import select

        from beework.models.conversations import Message

        with self.session_factory() as db:
            return db.scalar(select(Message.content).where(Message.run_id == UUID(run_id), Message.role == "assistant"))

    def test_a_tool_result_is_composed_by_one_model_call(self) -> None:
        run_id, llm, _, output = self.route(Fake([(ORDER, 0.93)]), "订单 A1 到哪了")
        self.assertEqual(output["route"]["path"], "A")
        self.assertEqual(len(llm.seen_messages), 1)
        prompt = self.system_prompt(llm)
        self.assertIn("# 能力返回结果", prompt)
        self.assertIn("订单 A1：已发货", prompt)
        self.assertEqual(self.final_text(run_id), "智能体的回答")

    def test_low_confidence_goes_to_the_agent_with_the_candidates_as_hints(self) -> None:
        fake = Fake([(ORDER, 0.4), (SHOUT, 0.2)])
        _, llm, _, output = self.route(fake, "帮我看看")
        self.assertEqual((output["route"]["path"], output["route"]["handoff"], fake.invoked), ("B", "low_confidence", []))
        self.assertIn("# 可能相关的能力", self.system_prompt(llm))
        self.assertIn("get_order", self.system_prompt(llm))

    def test_a_failing_classifier_never_fails_the_run(self) -> None:
        run_id, llm, _, output = self.route(Fake(failure="timeout"), "订单 A1")
        self.assertEqual((self.run_row(run_id).status, output["route"]["path"], output["route"]["handoff"], output["route"]["failure"]), ("succeeded", "B", "classifier_failed", "timeout"))
        self.assertEqual(len(llm.seen_messages), 1)

    def test_missing_arguments_and_a_failing_capability_hand_over_to_the_agent(self) -> None:
        _, llm, _, output = self.route(Fake([(ORDER, 0.95)], slots=Slots("missing", missing=["id"])), "查订单")
        self.assertEqual((output["route"]["path"], output["route"]["handoff"], output["route"]["slots"]), ("B", "slots_missing", {"status": "missing", "missing": ["id"]}))
        self.assertIn("id", self.system_prompt(llm))
        fake = Fake([(ORDER, 0.95)], result=ToolResult("error", "boom", code="tool.failed"))
        _, llm, _, output = self.route(fake, "订单 A1")
        self.assertEqual((output["route"]["path"], output["route"]["handoff"], output["route"]["capability_error"]), ("B", "capability_failed", "tool.failed"))
        self.assertEqual(len(fake.invoked), 1)

    def test_the_router_opens_the_l2_spans_and_names_the_capability_on_path_a(self) -> None:
        from contextlib import nullcontext

        from beework.runtime.tracing import TraceHandle

        names: list[str] = []
        with patch.object(TraceHandle, "span", lambda self, stage: (names.append(stage), nullcontext())[1]):
            self.route(Fake([(ORDER, 0.93)]), "订单 A1 到哪了")
        self.assertEqual([n for n in names if n.startswith(("l2.", "path."))], ["l2.recall", "l2.classify", "l2.slots", "path.a.get_order"])
        names.clear()
        with patch.object(TraceHandle, "span", lambda self, stage: (names.append(stage), nullcontext())[1]):
            self.route(Fake([(ORDER, 0.2)]), "帮我看看")  # path B: judged, nothing carried out
        self.assertEqual([n for n in names if n.startswith(("l2.", "path."))], ["l2.recall", "l2.classify"])

    def test_a_priority_rule_calls_its_target_without_the_classifier(self) -> None:
        rule = {"id": "r1", "name": "查订单", "priority": 1, "match_type": "keyword", "pattern": "订单", "target_entry_id": ORDER.id, "enabled": True, "created_at": "2026-01-01"}
        fake = Fake([(SHOUT, 0.9)])
        _, _, _, output = self.route(fake, "订单 A1 到哪了", priority_rules_enabled=True, rules=[rule])
        self.assertEqual((output["route"]["source"], output["route"]["rule_id"], fake.classified, fake.invoked[0][0]), ("rule", "r1", [], "get_order"))

    CHUNK = {"document_name": "退货", "chunk_id": "c1", "text": "Q", "score": 0.9, "kind": "faq", "kb_name": "售后知识库", "document_id": "d1"}

    def test_a_confident_faq_answers_before_any_judgement_and_otherwise_the_agent_does(self) -> None:
        fake = Fake([], faq=FaqAnswer("七天无理由退货。", self.CHUNK))
        run_id, llm, retriever, output = self.route(fake, "能退货吗")
        self.assertEqual((output["route"]["path"], output["route"]["source"], llm.seen_messages, retriever.calls), ("A", "faq", [], []))
        self.assertEqual((fake.classified, fake.faq_asked), ([], ["能退货吗"]))  # the classifier was never asked
        self.assertEqual(self.final_text(run_id), "七天无理由退货。")
        miss = Fake([(ORDER, 0.1)], faq=None)
        _, llm, _, output = self.route(miss, "能退货吗")
        self.assertEqual((output["route"]["path"], len(llm.seen_messages), len(miss.classified)), ("B", 1, 1))  # a miss goes on to the judgement

    def test_the_classifier_never_sees_a_knowledge_base_as_a_candidate(self) -> None:
        seen = []
        fake = Fake([(ORDER, 0.1)])
        real = fake.runtime()
        real.classify = lambda q, h, found: (seen.extend(c.kind for c in found), ClassifyResult(scored=[]))[1]
        with patch("beework.runtime.executor._intent_runtime", return_value=real):
            self.configure(intent={"enabled": True})
            self.ask("能退货吗", llm=ScriptedChat(chunks=["x"], seen_messages=[]))
        self.assertNotIn("kb", seen)
        self.assertIn("api_tool", seen)

    def test_a_priority_rule_and_a_waiting_flow_come_before_the_faq(self) -> None:
        rule = {"id": "r1", "name": "查订单", "priority": 1, "match_type": "keyword", "pattern": "订单", "target_entry_id": ORDER.id, "enabled": True, "created_at": "2026-01-01"}
        fake = Fake([], faq=FaqAnswer("FAQ 答案", self.CHUNK))
        _, _, _, output = self.route(fake, "订单 A1 到哪了", priority_rules_enabled=True, rules=[rule])
        self.assertEqual((output["route"]["source"], fake.faq_asked), ("rule", []))
        waiting = Fake([], faq=FaqAnswer("FAQ 答案", self.CHUNK), candidates=(FLOW, FAQ_KB), slots=Slots("ok", args={"query": "好的"}), result=ToolResult("ok", "已记录"))
        waiting.pending = FLOW
        _, _, _, output = self.route(waiting, "好的")
        self.assertEqual((output["route"]["source"], waiting.faq_asked), ("pending", []))

    def test_with_faq_direct_off_the_faq_is_not_asked(self) -> None:
        fake = Fake([(ORDER, 0.1)], faq=FaqAnswer("FAQ 答案", self.CHUNK))
        _, _, _, output = self.route(fake, "能退货吗", faq_direct=False)
        self.assertEqual((fake.faq_asked, output["route"]["path"]), ([], "B"))

    def test_a_chatflow_answer_is_the_reply_and_leaves_the_conversation_waiting_for_it(self) -> None:
        fake = Fake([(FLOW, 0.95)], slots=Slots("ok", args={"query": "我要退货"}), result=ToolResult("ok", "请提供订单号"), candidates=(FLOW,))
        run_id, llm, _, output = self.route(fake, "我要退货")
        self.assertEqual((output["route"]["path"], llm.seen_messages), ("A", []))  # the Dify answer is the reply, no composing call
        self.assertEqual(output["route"]["pending"], {"entry_id": "e-flow"})  # the next message is for the flow, if the connection says so
        self.assertEqual(self.final_text(run_id), "请提供订单号")

    def test_a_failure_after_part_of_a_streamed_answer_is_not_followed_by_a_second_answer(self) -> None:
        fake = Fake([(FLOW, 0.95)], slots=Slots("ok", args={"query": "我要退货"}), result=ToolResult("error", "cut", code="tool.stream_interrupted"), candidates=(FLOW,))
        real = fake.runtime()

        def invoke(target, args, stream=None):
            if stream is not None:
                stream("请提供")  # half an answer reached the user
            return fake.result

        real.invoke = invoke
        self.configure(intent={"enabled": True})
        llm = ScriptedChat(chunks=["不该出现"], seen_messages=[])
        with patch("beework.runtime.executor._intent_runtime", return_value=real):
            run_id, llm, _ = self.ask("我要退货", llm=llm)
        output = self.run_row(run_id).output
        self.assertEqual((llm.seen_messages, self.final_text(run_id)), ([], "请提供"))
        self.assertEqual((output["route"]["path"], output["route"]["partial"], output["route"]["capability_error"]), ("A", True, "tool.stream_interrupted"))

    def test_the_trace_carries_no_user_text_or_argument_values(self) -> None:
        fake = Fake([(ORDER, 0.95)], slots=Slots("ok", args={"id": SECRET}))
        run_id, _, _, output = self.route(fake, f"我的手机号 {SECRET}，查订单")
        self.assertNotIn(SECRET, json.dumps(output["route"], ensure_ascii=False))
        event = next(e.data for e in self.events(run_id) if e.type == "step" and e.data["name"] == "route" and e.data["status"] == "completed")
        self.assertEqual(event["route"]["path"], "A")
        self.assertNotIn(SECRET, json.dumps(event["route"], ensure_ascii=False))

    def test_decomposed_parts_answered_directly_are_merged_by_the_agent(self) -> None:
        self.configure(rewrite={"enabled": True, "only_low_confidence": False, "decompose": {"enabled": True}})
        rewrite_llm = test_rewrite.judge(coreference="x", normalized="x", sub_questions=["订单 A1 到哪了", "怎么退货"])
        fake = Fake([(ORDER, 0.95)])
        self.configure(intent={"enabled": True}, rewrite={"enabled": True, "only_low_confidence": False, "decompose": {"enabled": True}})
        with patch("beework.runtime.executor._intent_runtime", return_value=fake.runtime()):
            run_id, llm, _ = self.ask("订单 A1 到哪了，怎么退货", rewrite_llm=rewrite_llm, llm=ScriptedChat(chunks=["合并"], seen_messages=[]))
        self.assertIn("# 子问题结果", self.system_prompt(llm))
        self.assertEqual(self.run_row(run_id).output["route"]["handoff"], "decomposed")

    def test_with_judgement_off_nothing_changes(self) -> None:
        self.configure(intent={"enabled": False})
        _, llm, _ = self.ask("订单")
        self.assertNotIn("# 可能相关的能力", self.system_prompt(llm))
        self.assertFalse(self.run_row(self.ask("订单")[0]).output["route"])
