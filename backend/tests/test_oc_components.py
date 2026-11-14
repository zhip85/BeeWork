"""T-059: OC components — the parser and the modes, the run pipeline, the injection preview, and the events users send back."""

import json
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from sqlalchemy import select

import test_components
import test_rewrite
from beework.models.conversations import Message
from beework.models.runtime import Run
from beework.runtime import executor
from beework.runtime.components_stage import ComponentRuntime, apply_components
from beework.runtime.oc import ComponentsConfig, OCConfig
from beework.schemas.channels import WidgetChatRequest
from beework.schemas.open_api import ChatRequest
from test_runtime import ScriptedChat

CARD = {
    "id": str(uuid4()), "name_zh": "商品卡片", "name_en": "Card", "type": "card", "trigger_tag": "product_card", "version": "1.2", "mode": "native",
    "schema": json.dumps({"name": "string", "price": "string", "stock?": "number"}), "sample_data": "{}",
}
TRACK = {**CARD, "id": str(uuid4()), "name_zh": "订单追踪", "type": "timeline", "trigger_tag": "order_tracking", "mode": "text", "schema": json.dumps({"title": "string", "status": "string"})}
RAW = {**CARD, "id": str(uuid4()), "name_zh": "原始", "trigger_tag": "raw_data", "mode": "json"}
CLOSED = {**CARD, "id": str(uuid4()), "trigger_tag": "closed_one", "mode": "off"}
GOOD = '<product_card>{"name":"耳机","price":"¥99"}</product_card>'


def runtime(*items: dict) -> ComponentRuntime:
    return ComponentRuntime(components=list(items), channel_name="官网")


def apply(text: str, *items: dict, **config):
    return apply_components(text, runtime(*items), ComponentsConfig(**config))


class ApplyTests(unittest.TestCase):
    def test_a_native_component_becomes_a_widget_and_the_block_leaves_the_text(self) -> None:
        text, widgets, info, failures = apply(f"为您推荐：\n{GOOD}\n还有问题吗？", CARD)
        self.assertEqual(text, "为您推荐：\n\n还有问题吗？")
        self.assertEqual([(w["tag"], w["type"], w["mode"], w["data"]["name"]) for w in widgets], [("product_card", "card", "native", "耳机")])
        self.assertEqual((info, failures), ([{"tag": "product_card", "mode": "native", "ok": True, "problems": [], "kept": True}], []))

    def test_json_mode_returns_widgets_text_mode_writes_text_and_off_is_dropped(self) -> None:
        reply = f'{GOOD}<order_tracking>{{"title":"A001","status":"运输中"}}</order_tracking><raw_data>{{"name":"x","price":"1"}}</raw_data><closed_one>{{"name":"x","price":"1"}}</closed_one>'
        text, widgets, info, _ = apply(reply, CARD, TRACK, RAW, CLOSED, max_per_turn=5)
        self.assertEqual(text, "A001\nstatus：运输中")  # the text component, through the default template
        self.assertEqual([(w["tag"], w["mode"]) for w in widgets], [("product_card", "native"), ("raw_data", "json")])
        closed = next(r for r in info if r["tag"] == "closed_one")
        self.assertEqual((closed["ok"], closed["problems"]), (False, ["该渠道已关闭此组件"]))

    def test_the_text_template_is_used_for_text_mode(self) -> None:
        text, widgets, *_ = apply('<order_tracking>{"title":"A001","status":"运输中"}</order_tracking>', TRACK, text_template="【{{title}}】{{fields}}")
        self.assertEqual((text, widgets), ("【A001】status：运输中", []))

    def test_components_over_the_limit_are_dropped(self) -> None:
        text, widgets, info, _ = apply(GOOD * 3, CARD, max_per_turn=2)
        self.assertEqual(len(widgets), 2)
        self.assertEqual([r["ok"] for r in info], [True, True, False])
        self.assertEqual(text, "")

    def test_invalid_json_and_schema_mismatches_follow_the_policy(self) -> None:
        wrong = '<product_card>{"name":"耳机","price":99}</product_card>'  # price must be a string
        broken = "<product_card>{not json</product_card>"
        text, widgets, info, failures = apply(f"A{wrong}B", CARD)  # default: degrade to text, the content survives
        self.assertEqual((text, widgets), ("A耳机\nprice：99B".replace("A耳机", "A耳机"), []))
        self.assertEqual(info[0]["problems"], ["price 应是 string"])
        self.assertEqual(failures, ["<product_card>：price 应是 string"])
        self.assertEqual(apply(f"A{broken}B", CARD)[0], "AB")  # unreadable JSON has nothing to show
        self.assertEqual(apply(f"A{wrong}B", CARD, on_invalid="drop")[0], "AB")
        _, _, _, retry_failures = apply(f"A{wrong}B", CARD, on_invalid="retry")  # retry: the stage asks the model; the problems are handed back
        self.assertEqual(retry_failures, failures)

    def test_text_outside_known_tags_is_untouched(self) -> None:
        reply = "价格是 <b>99</b>，见 <unknown_tag>{}</unknown_tag>"
        self.assertEqual(apply(reply, CARD)[:2], (reply, []))

    def test_the_components_config_is_clamped_and_has_defaults(self) -> None:
        self.assertEqual((ComponentsConfig().enabled, ComponentsConfig().max_per_turn, ComponentsConfig().on_invalid), (True, 2, "text"))
        self.assertEqual((ComponentsConfig(max_per_turn=99).max_per_turn, ComponentsConfig(max_per_turn=0).max_per_turn), (5, 1))
        self.assertEqual(ComponentsConfig(text_template="  ").text_template, "{{title}}\n{{fields}}")
        self.assertEqual(OCConfig.model_validate({}).components.max_per_turn, 2)  # an assistant without the section still has it


class PipelineTests(test_rewrite.RewriteRuntimeTests):
    def ask_with(self, reply: str | list[str], *items: dict, retry_reply: str | None = None, **config):
        self.configure(components=config)
        chunks = [reply] if isinstance(reply, str) else reply
        llm = ScriptedChat(chunks=chunks, seen_messages=[])
        with patch("beework.runtime.executor._component_runtime", return_value=runtime(*items) if items else None):
            run_id, llm, _ = self.ask("推荐一款耳机", llm=llm)
        return run_id, llm

    def stored(self, run_id: str) -> tuple[Run, str]:
        with self.session_factory() as db:
            run = db.get(Run, UUID(run_id))
            return run, db.scalar(select(Message.content).where(Message.run_id == UUID(run_id), Message.role == "assistant"))

    def test_the_prompt_lists_the_channels_components_and_the_run_returns_widgets(self) -> None:
        run_id, llm = self.ask_with(f"推荐如下：{GOOD}", CARD, TRACK)
        prompt = self.system_prompt(llm)
        self.assertIn("# 输出格式", prompt)
        self.assertIn("<product_card> 商品卡片 schema:", prompt)
        self.assertIn("<order_tracking> 订单追踪（本渠道以文本降级展示）", prompt)
        self.assertIn("每轮最多输出 2 个组件", prompt)
        run, text = self.stored(run_id)
        self.assertEqual(text, "推荐如下：")
        self.assertEqual([(w["tag"], w["data"]["name"]) for w in run.output["widgets"]], [("product_card", "耳机")])
        self.assertEqual(run.output["components"], [{"tag": "product_card", "mode": "native", "ok": True, "problems": [], "kept": True}])
        done = next(e.data for e in self.events(run_id) if e.type == "done")
        self.assertEqual(done["widgets"], run.output["widgets"])
        step = self.stage_event(run_id, "components")
        self.assertEqual(step["components"]["blocks"][0]["tag"], "product_card")
        self.assertNotIn("耳机", json.dumps(step, ensure_ascii=False))  # the trace carries tags and problems, never the user's content

    def test_the_reply_is_held_back_and_sent_once_the_blocks_are_resolved(self) -> None:
        streamed: list[str] = []
        real = executor._TokenBuffer.add
        with patch.object(executor._TokenBuffer, "add", lambda self, text: (streamed.append(text), real(self, text))[1]):  # token events are purged once a run is done
            self.ask_with(["推荐：<product_card>", '{"name":"耳机",', '"price":"¥99"}</product_card>'], CARD)
        self.assertEqual(streamed, ["推荐："])  # one piece, after the block was taken out: no tag ever reached the user

    def test_a_channel_without_components_or_with_orchestration_off_injects_nothing(self) -> None:
        for items, config in (((), {}), ((CARD,), {"enabled": False}), ((CLOSED,), {})):
            run_id, llm = self.ask_with(f"好的{GOOD}", *items, **config)
            self.assertNotIn("# 输出格式", self.system_prompt(llm))
            self.assertEqual(self.stored(run_id)[1], f"好的{GOOD}")  # nothing is parsed either: the reply is plain text

    def test_the_retry_policy_asks_the_model_once_more_and_degrades_what_is_still_wrong(self) -> None:
        wrong = '<product_card>{"name":"耳机","price":99}</product_card>'
        run_id, llm = self.ask_with(f"推荐{wrong}", CARD, on_invalid="retry")
        self.assertEqual(len(llm.seen_messages), 2)  # the original answer and one retry
        feedback = llm.seen_messages[1][-1].content
        self.assertIn("price 应是 string", feedback)
        run, text = self.stored(run_id)
        self.assertEqual(run.output["components"][0]["problems"], ["price 应是 string"])  # the retry answered the same: degraded, not asked again
        self.assertEqual(text, "推荐耳机\nprice：99")
        self.assertEqual(len(llm.seen_messages), 2)

    def test_the_components_stage_is_skipped_when_the_input_was_blocked(self) -> None:
        self.configure(components={}, pii={"rules": []})
        self.assertEqual(self.ask_with("好", CARD)[0] is not None, True)


class ChannelFlowTests(test_components.ComponentBase):
    def published_channel(self, **config):
        item = self.make(trigger_tag="promo_card")
        channel = self.client.post("/api/v1/channels", json={"type": "website", "name": "Site", "assistant_id": self.assistant_id, "environment": "production", "allowed_origins": ["https://www.example.com"], "component_ids": [item["id"]]}, headers=self.admin).json()
        self.client.post(f"/api/v1/channels/{channel['id']}/publish", json={}, headers=self.admin)
        return item, channel

    def preview(self) -> dict:
        response = self.client.get(f"/api/v1/assistants/{self.assistant_id}/oc/components/preview", headers=self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_the_injection_preview_shows_each_channels_published_components_and_prompt(self) -> None:
        self.assertEqual(self.preview(), {"enabled": True, "channels": []})
        item, channel = self.published_channel()
        shown = self.preview()["channels"][0]
        self.assertEqual((shown["name"], shown["published"], [(c["trigger_tag"], c["mode"]) for c in shown["components"]]), ("Site", True, [("promo_card", "native")]))
        self.assertIn("<promo_card> 商品卡片", shown["prompt"])
        self.assertIn("每轮最多输出 2 个组件", shown["prompt"])
        # CHN-025: editing the library changes what the *next* publish freezes, not what runs now
        self.put(item, data_schema='{"name":"string","tags":"string[]","action":"{ label: string; url: string }"}', trigger_tag="renamed_card")
        self.assertEqual(self.preview()["channels"][0]["components"][0]["trigger_tag"], "promo_card")
        self.assertIn("<promo_card>", self.preview()["channels"][0]["prompt"])

    def test_the_preview_follows_the_oc_settings_and_an_unpublished_channel_injects_nothing(self) -> None:
        draft = self.client.post("/api/v1/channels", json={"type": "website", "name": "Draft", "assistant_id": self.assistant_id, "environment": "production", "allowed_origins": ["https://a.example.com"]}, headers=self.admin).json()
        self.assertEqual(draft["status"], "draft")
        shown = self.preview()["channels"][0]
        self.assertEqual((shown["published"], shown["prompt"], shown["published_version"]), (False, None, "—"))
        self.published_channel()
        oc = self.client.get(f"/api/v1/assistants/{self.assistant_id}/oc", headers=self.admin).json()
        saved = self.client.put(f"/api/v1/assistants/{self.assistant_id}/oc", json={"oc": {**oc["oc"], "components": {"enabled": True, "max_per_turn": 9, "on_invalid": "drop"}}, "row_version": oc["row_version"]}, headers=self.admin)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["oc"]["components"]["max_per_turn"], 5)  # out of range: pulled in
        self.assertTrue(any("每轮最多输出 5 个组件" in (c["prompt"] or "") for c in self.preview()["channels"]))
        summary = next(s for s in saved.json()["stages"] if s["stage"] == "components")
        self.assertEqual((summary["available"], summary["summary"]), (True, "已启用 · 每轮最多 5 个 · 失败丢弃组件 · 1 个渠道已发布组件"))

    def test_the_publish_dialog_prompt_uses_the_assistants_oc_numbers(self) -> None:
        item = self.make(trigger_tag="dialog_card")
        channel = self.client.post("/api/v1/channels", json={"type": "website", "name": "Site", "assistant_id": self.assistant_id, "environment": "production", "allowed_origins": ["https://www.example.com"], "component_ids": [item["id"]]}, headers=self.admin).json()
        oc = self.client.get(f"/api/v1/assistants/{self.assistant_id}/oc", headers=self.admin).json()
        self.client.put(f"/api/v1/assistants/{self.assistant_id}/oc", json={"oc": {**oc["oc"], "components": {"max_per_turn": 4}}, "row_version": oc["row_version"]}, headers=self.admin)
        prompt = self.client.get(f"/api/v1/channels/{channel['id']}/publish-preview", headers=self.admin).json()["injection_prompt"]
        self.assertIn("每轮最多输出 4 个组件", prompt)

    def test_an_event_from_a_component_becomes_the_next_input(self) -> None:
        request = ChatRequest.model_validate({"event": {"tag": "product_card", "action": "buy", "data": {"sku": "A1"}}, "end_user": {"id": "u1"}})
        self.assertEqual(request.message, '[用户操作] 组件 <product_card> buy：{"sku": "A1"}')
        self.assertEqual(WidgetChatRequest.model_validate({"event": {"tag": "appointment_picker", "action": "confirm", "data": {"date": "11-15"}}}).message, '[用户操作] 组件 <appointment_picker> confirm：{"date": "11-15"}')
        for bad in ({}, {"message": "  "}, {"event": {"tag": "Bad Tag", "action": "x"}}, {"event": {"tag": "ok", "action": "no way!"}}):
            with self.assertRaises(ValueError, msg=str(bad)):
                WidgetChatRequest.model_validate(bad)
        self.assertEqual(WidgetChatRequest.model_validate({"message": "你好"}).message, "你好")

    def test_an_open_api_caller_can_only_name_a_published_open_api_channel_of_that_assistant(self) -> None:
        key = self.create_key()["key"]
        draft = self.client.post("/api/v1/channels", json={"type": "open-api", "name": "API", "assistant_id": self.assistant_id, "environment": "production"}, headers=self.admin).json()
        for channel_id in (draft["id"], str(uuid4())):  # not published / unknown
            response = self.chat(key, channel_id=channel_id)
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (404, "channel_not_found"))
        self.admin = self.login("admin")
        live = self.client.post(f"/api/v1/channels/{draft['id']}/publish", json={}, headers=self.admin)
        self.assertEqual(live.status_code, 200, live.text)
        ok = self.chat(key, channel_id=draft["id"])
        self.assertEqual(ok.status_code, 200, ok.text)
        self.admin = self.login("admin")
        self.assertEqual(ok.json()["widgets"], [])  # present (and empty) for callers that name a channel; the mock model emits no component
        other = self.make_assistant(self.admin, "Other", "p")
        self.assertEqual(self.chat(key, assistant=other, channel_id=draft["id"]).status_code, 404)  # the channel is bound to another assistant


if __name__ == "__main__":
    unittest.main()
