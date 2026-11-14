"""T-043: component schema parser and validator, tenant component library, platform templates, channel component selection."""

import json
import unittest
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select

import test_channels
from beework import component_service
from beework.component_schema import SchemaError, parse_component_schema, validate_component_payload
from beework.main import app
from beework.models import User
from beework.models.components import TenantComponent, WidgetTemplate
from beework.security import hash_password

SCHEMA = '{"name":"string","price?":"number","tags":"string[]","action":"{ label: string; url: string }"}'
SAMPLE = '{"name":"耳机","tags":["新品"],"action":{"label":"买","url":"https://x.example.com"}}'


class SchemaTests(unittest.TestCase):
    def check(self, schema, payload) -> list[str]:
        return validate_component_payload(parse_component_schema(schema), payload)

    def test_optional_fields_arrays_and_nested_objects(self) -> None:
        self.assertEqual(self.check(SCHEMA, json.loads(SAMPLE)), [])
        self.assertEqual(self.check(SCHEMA, {**json.loads(SAMPLE), "price": 9.5}), [])  # optional, present and right
        problems = self.check(SCHEMA, {"name": 1, "tags": ["a", 2], "action": {"url": "u"}, "price": "x"})
        self.assertEqual(sorted(problems), sorted(["name 应是 string", "tags[1] 应是 string", "action.label 缺少必填字段", "price 应是 number"]))

    def test_the_required_fields_and_the_types_are_checked(self) -> None:
        self.assertEqual(self.check('{"a":"string"}', {}), ["a 缺少必填字段"])
        self.assertEqual(self.check('{"a":"string"}', {"a": None}), ["a 不能为空"])
        self.assertEqual(self.check('{"n":"number"}', {"n": True}), ["n 应是 number"])  # a boolean is not a number
        self.assertEqual(self.check('{"n":"integer"}', {"n": 1.5}), ["n 应是 integer"])
        self.assertEqual(self.check('{"a":"string"}', {"a": "x", "extra": 1}), [])  # unknown fields are allowed
        self.assertEqual(self.check('{"steps":"{ label: string; done: boolean }[]"}', {"steps": [{"label": "a", "done": "no"}]}), ["steps[0].done 应是 boolean"])
        self.assertEqual(validate_component_payload(parse_component_schema('{"a":"string"}'), []), ["（根） 应是对象"])

    def test_json_notation_nested_object_and_list_forms(self) -> None:
        schema = {"title": "string", "rows": [{"k": "string", "v?": "number"}], "meta": {"id": "integer"}}
        self.assertEqual(self.check(schema, {"title": "t", "rows": [{"k": "a"}], "meta": {"id": 1}}), [])
        self.assertEqual(self.check(schema, {"title": "t", "rows": [{"v": 1}], "meta": {}}), ["rows[0].k 缺少必填字段", "meta.id 缺少必填字段"])

    def test_standard_json_schema_is_accepted(self) -> None:
        schema = {"type": "object", "properties": {"n": {"type": "integer"}, "k": {"type": "string", "enum": ["a", "b"]}, "xs": {"type": "array", "items": {"type": "number"}}}, "required": ["n"]}
        self.assertEqual(self.check(schema, {"n": 1, "k": "a", "xs": [1, 2.5]}), [])
        self.assertEqual(sorted(self.check(schema, {"n": 1.5, "k": "c", "xs": ["x"]})), sorted(["n 应是 integer", "k 应是 a / b 之一", "xs[0] 应是 number"]))

    def test_schemas_that_cannot_be_understood_say_where(self) -> None:
        for bad, fragment in (
            ('{"a":"strng"}', "不支持的类型"), ('{"a":"{ x string }"}', "对象字段"), ('{"a":"string[]]"}', "多余内容"), ("[1]", "JSON 对象"), ("{oops", "JSON 格式有误"),
            ('{"1a":"string"}', "字段名"), ('{"a":["string","number"]}', "只写一个元素"), ('{"type":"object","properties":{"a":{"type":"date"}}}', "不支持的 type"),
            ('{"type":"object","properties":{"a":{"type":"string"}},"required":["b"]}', "required"),
        ):
            with self.assertRaises(SchemaError, msg=bad) as caught:
                parse_component_schema(bad)
            self.assertIn(fragment, str(caught.exception))
        with self.assertRaises(SchemaError):
            parse_component_schema('{"a":"{ b: { c: { d: { e: { f: { g: { h: { i: { j: string } } } } } } } } }"}')  # too deep

    def test_the_preset_templates_validate_their_own_sample_data(self) -> None:
        for kind, base in component_service.TYPE_DEFAULTS.items():
            self.assertEqual(validate_component_payload(base["schema"], base["sample"]), [], kind)


class ComponentBase(test_channels.ChannelBase):
    def setUp(self) -> None:
        super().setUp()
        with self.session_factory.begin() as db:
            db.add(User(email="platform@example.com", display_name="Platform", password_hash=hash_password(test_channels.PASSWORD), is_platform_admin=True))
        self.platform_client = TestClient(app)
        self.assertEqual(self.platform_client.post("/api/v1/auth/login", json={"email": "platform@example.com", "password": test_channels.PASSWORD}).status_code, 200)
        self.platform = {"X-CSRF-Token": self.platform_client.cookies.get("beework_csrf")}

    def body(self, **fields) -> dict:
        return {"name_zh": "商品卡片", "name_en": "Card", "type": "card", "trigger_tag": "Product Card", "data_schema": SCHEMA, "sample_data": SAMPLE, "status": "enabled", **fields}

    def make(self, **fields) -> dict:
        response = self.client.post("/api/v1/components", json=self.body(**fields), headers=self.admin)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def templates(self) -> list[dict]:
        response = self.client.get("/api/v1/widget-templates", headers=self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["items"]

    def put(self, component: dict, **fields):
        body = {**self.body(), **{k: component[k] for k in ("name_zh", "name_en", "type", "description", "trigger_tag", "data_schema", "sample_data", "compatibility", "status")}, **fields}
        return self.client.put(f"/api/v1/components/{component['id']}", json={**body, "row_version": component["row_version"]}, headers=self.admin)


class LibraryTests(ComponentBase):
    def test_a_new_component_normalises_the_tag_starts_at_v0_1_and_defaults_the_compatibility(self) -> None:
        component = self.make()
        self.assertEqual((component["trigger_tag"], component["version"], component["status"]), ("product_card", "0.1", "enabled"))
        self.assertEqual(component["compatibility"], {"website": "native", "h5": "native", "whatsapp": "text", "wechat": "text", "open-api": "json"})
        self.assertEqual(self.client.post("/api/v1/components", json=self.body(trigger_tag="product_card"), headers=self.admin).status_code, 409)  # the tag is unique per tenant

    def test_the_definition_is_validated_when_saved(self) -> None:
        for fields, code in (
            ({"data_schema": '{"a":"strng"}'}, "component.schema_invalid"), ({"sample_data": "{oops"}, "component.sample_invalid"),
            ({"sample_data": '{"name":1}'}, "component.sample_invalid"), ({"compatibility": {"telegram": "native"}}, "component.compatibility_invalid"),
            ({"trigger_tag": "   "}, "component.tag_invalid"),
        ):
            response = self.client.post("/api/v1/components", json=self.body(**fields), headers=self.admin)
            self.assertEqual((response.status_code, response.json()["code"]), (422, code), fields)

    def test_saving_bumps_the_minor_version_only_when_the_contract_changed(self) -> None:
        component = self.make()
        renamed = self.put(component, name_zh="新名字", description="改了描述").json()
        self.assertEqual((renamed["version"], renamed["name_zh"]), ("0.1", "新名字"))  # a name is not the contract
        changed = self.put(renamed, data_schema='{"name":"string","tags":"string[]","action":"{ label: string; url: string }"}').json()
        self.assertEqual(changed["version"], "0.2")
        retagged = self.put(changed, trigger_tag="card_two").json()
        self.assertEqual((retagged["version"], retagged["trigger_tag"]), ("0.3", "card_two"))
        stale = self.client.put(f"/api/v1/components/{component['id']}", json={**self.body(), "row_version": 1}, headers=self.admin)
        self.assertEqual(stale.status_code, 409)

    def test_the_library_is_per_tenant_and_needs_the_right_permission(self) -> None:
        mine = self.make()
        self.admin = self.login("admin")
        viewer = self.login("viewer")
        self.assertEqual(self.client.get("/api/v1/components", headers=viewer).status_code, 200)  # a viewer may look
        self.assertEqual(self.client.post("/api/v1/components", json=self.body(trigger_tag="other"), headers=viewer).status_code, 403)
        foreign = self.login("foreign", self.ids["other"])
        self.assertEqual(self.client.get("/api/v1/components", headers=foreign).json()["items"], [])
        self.assertEqual(self.client.get(f"/api/v1/components/{mine['id']}", headers=foreign).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/v1/components/{mine['id']}", headers=foreign).status_code, 404)
        other = self.client.post("/api/v1/components", json=self.body(), headers=foreign)  # the same tag in another tenant is fine
        self.assertEqual(other.status_code, 201)

    def test_the_selectable_list_hides_disabled_and_incompatible_components(self) -> None:
        ok = self.make(trigger_tag="ok")
        self.make(trigger_tag="off_wechat", compatibility={"wechat": "off"})
        self.make(trigger_tag="stopped", status="disabled")
        names = lambda **p: sorted(i["trigger_tag"] for i in self.client.get("/api/v1/components", params=p, headers=self.admin).json()["items"])  # noqa: E731
        self.assertEqual(names(), ["off_wechat", "ok", "stopped"])
        self.assertEqual(names(selectable="true", channel_type="wechat"), ["ok"])
        self.assertEqual(names(selectable="true", channel_type="website"), ["off_wechat", "ok"])
        self.assertEqual(ok["version"], "0.1")


class TemplateTests(ComponentBase):
    def test_four_presets_are_installed_with_the_prd_versions_and_matrix(self) -> None:
        items = {t["trigger_tag"]: t for t in self.templates()}
        self.assertEqual({k: v["version"] for k, v in items.items()}, {"product_card": "1.2", "product_comparison": "1.0", "order_tracking": "2.1", "appointment_picker": "1.4"})
        self.assertEqual(items["appointment_picker"]["compatibility"], {"website": "native", "h5": "native", "whatsapp": "text", "wechat": "off", "open-api": "json"})
        self.assertEqual(len(self.templates()), 4)  # installing twice adds nothing

    def test_copying_a_template_records_its_origin_and_a_second_copy_gets_a_new_tag(self) -> None:
        card = next(t for t in self.templates() if t["trigger_tag"] == "product_card")
        first = self.client.post(f"/api/v1/components/from-template/{card['id']}", headers=self.admin).json()
        second = self.client.post(f"/api/v1/components/from-template/{card['id']}", headers=self.admin).json()
        self.assertEqual((first["template_id"], first["template_version"], first["status"], first["version"], first["trigger_tag"]), (card["id"], "1.2", "enabled", "1.2", "product_card"))
        self.assertEqual(second["trigger_tag"], "product_card_2")
        self.assertEqual(next(t for t in self.templates() if t["id"] == card["id"])["copies"], 2)

    def test_a_deprecated_or_draft_template_cannot_be_copied_and_tenants_do_not_see_it(self) -> None:
        card = next(t for t in self.templates() if t["trigger_tag"] == "product_card")
        full = next(t for t in self.platform_client.get("/api/v1/platform/widget-templates", headers=self.platform).json()["items"] if t["id"] == card["id"])
        body = {k: full[k] for k in ("name_zh", "name_en", "type", "description", "trigger_tag", "data_schema", "sample_data", "compatibility", "version")}
        self.assertEqual(self.platform_client.put(f"/api/v1/platform/widget-templates/{card['id']}", json={**body, "status": "deprecated"}, headers=self.platform).status_code, 200)
        self.assertNotIn(card["id"], [t["id"] for t in self.templates()])
        refused = self.client.post(f"/api/v1/components/from-template/{card['id']}", headers=self.admin)
        self.assertEqual((refused.status_code, refused.json()["code"]), (409, "component.template_unavailable"))

    def test_the_platform_must_raise_the_version_when_the_contract_changes(self) -> None:
        card = next(t for t in self.platform_client.get("/api/v1/platform/widget-templates", headers=self.platform).json()["items"] if t["trigger_tag"] == "product_card")
        body = {k: card[k] for k in ("name_zh", "name_en", "type", "description", "trigger_tag", "data_schema", "sample_data", "compatibility", "version", "status")}
        url = f"/api/v1/platform/widget-templates/{card['id']}"
        self.assertEqual(self.platform_client.put(url, json={**body, "description": "只改描述"}, headers=self.platform).status_code, 200)  # no version change needed
        contract = {**body, "compatibility": {**body["compatibility"], "wechat": "off"}}
        refused = self.platform_client.put(url, json=contract, headers=self.platform)
        self.assertEqual((refused.status_code, refused.json()["code"]), (422, "component.version_required"))
        self.assertEqual(self.platform_client.put(url, json={**contract, "version": "1.3"}, headers=self.platform).status_code, 200)

    def test_a_template_with_copies_can_only_be_deprecated_and_tenants_cannot_edit_templates(self) -> None:
        card = next(t for t in self.templates() if t["trigger_tag"] == "product_card")
        self.client.post(f"/api/v1/components/from-template/{card['id']}", headers=self.admin)
        refused = self.platform_client.delete(f"/api/v1/platform/widget-templates/{card['id']}", headers=self.platform)
        self.assertEqual((refused.status_code, refused.json()["code"]), (409, "component.template_has_copies"))
        self.assertEqual(self.client.put(f"/api/v1/platform/widget-templates/{card['id']}", json={}, headers=self.admin).status_code, 403)
        fresh = self.platform_client.post("/api/v1/platform/widget-templates", json={**self.body(), "version": "0.1", "status": "draft", "trigger_tag": "fresh_one"}, headers=self.platform)
        self.assertEqual((fresh.status_code, fresh.json()["status"]), (201, "draft"))
        self.assertEqual(self.platform_client.delete(f"/api/v1/platform/widget-templates/{fresh.json()['id']}", headers=self.platform).status_code, 204)

    def test_syncing_shows_the_difference_and_keeps_the_tenants_name_and_description(self) -> None:
        card = next(t for t in self.templates() if t["trigger_tag"] == "product_card")
        mine = self.client.post(f"/api/v1/components/from-template/{card['id']}", headers=self.admin).json()
        mine = self.put(mine, name_zh="我的卡片", description="自己的描述").json()
        self.assertIsNone(mine["template_latest"])
        full = next(t for t in self.platform_client.get("/api/v1/platform/widget-templates", headers=self.platform).json()["items"] if t["id"] == card["id"])
        body = {k: full[k] for k in ("name_zh", "name_en", "type", "description", "trigger_tag", "data_schema", "sample_data", "compatibility", "status")}
        newer = {**body, "data_schema": '{"name":"string","tags":"string[]","action":"{ label: string; url: string }"}', "sample_data": SAMPLE, "version": "1.3"}
        self.assertEqual(self.platform_client.put(f"/api/v1/platform/widget-templates/{card['id']}", json=newer, headers=self.platform).status_code, 200)
        shown = self.client.get(f"/api/v1/components/{mine['id']}", headers=self.admin).json()
        self.assertEqual(shown["template_latest"], "1.3")  # “模板已更新 v1.3”
        preview = self.client.get(f"/api/v1/components/{mine['id']}/sync-preview", headers=self.admin).json()
        self.assertEqual((preview["to_version"], [c["field"] for c in preview["changes"]]), ("1.3", ["数据架构", "模拟数据", "版本"]))
        synced = self.client.post(f"/api/v1/components/{mine['id']}/sync", headers=self.admin).json()
        self.assertEqual((synced["name_zh"], synced["description"], synced["version"], synced["template_version"], synced["template_latest"], synced["data_schema"]), ("我的卡片", "自己的描述", "1.3", "1.3", None, newer["data_schema"]))
        self.assertEqual(self.client.post(f"/api/v1/components/{self.make(trigger_tag='scratch')['id']}/sync", headers=self.admin).status_code, 409)  # not a copy

    def test_new_tenants_get_the_first_three_templates(self) -> None:
        with self.session_factory.begin() as db:
            copies = component_service.provision_tenant_components(db, self.ids["other"])
            self.assertEqual([c.trigger_tag for c in copies], ["product_card", "product_comparison", "order_tracking"])
        with self.session_factory() as db:
            self.assertEqual(len(db.scalars(select(TenantComponent).where(TenantComponent.tenant_id == self.ids["other"])).all()), 3)
            self.assertEqual(len(db.scalars(select(WidgetTemplate)).all()), 4)


class ChannelComponentTests(ComponentBase):
    def channel(self, component_ids: list[str], **fields):
        body = {"type": "website", "name": "Site", "assistant_id": self.assistant_id, "environment": "production", "allowed_origins": ["https://www.example.com"], "component_ids": component_ids, **fields}
        return self.client.post("/api/v1/channels", json=body, headers=self.admin)

    def update(self, channel: dict, **fields):
        body = {"type": "website", "name": channel["name"], "assistant_id": self.assistant_id, "allowed_origins": channel["allowed_origins"], "component_ids": channel["component_ids"], "row_version": channel["row_version"], **fields}
        return self.client.put(f"/api/v1/channels/{channel['id']}", json=body, headers=self.admin)

    def test_only_usable_components_of_the_tenant_can_be_selected(self) -> None:
        good = self.make(trigger_tag="good")
        stopped = self.make(trigger_tag="stopped", status="disabled")
        closed = self.make(trigger_tag="closed", compatibility={"website": "off"})
        foreign = self.login("foreign", self.ids["other"])
        theirs = self.client.post("/api/v1/components", json=self.body(), headers=foreign).json()
        self.admin = self.login("admin")
        for bad in (stopped, closed, theirs, {"id": "00000000-0000-0000-0000-000000000001"}):
            response = self.channel([bad["id"]])
            self.assertEqual((response.status_code, response.json()["code"]), (422, "channel.component_invalid"), bad["id"])
        self.assertEqual(self.channel([good["id"]]).json()["component_ids"], [good["id"]])

    def test_the_publish_dialog_lists_the_diff_the_prompt_and_a_real_trial_run(self) -> None:
        card, bad = self.make(trigger_tag="card_one", name_en="Card one"), self.make(trigger_tag="wechat_text", compatibility={"website": "json"})
        channel = self.channel([card["id"], bad["id"]]).json()
        preview = self.client.get(f"/api/v1/channels/{channel['id']}/publish-preview", headers=self.admin).json()
        self.assertEqual(sorted((c["trigger_tag"], c["mode"]) for c in preview["components"]), [("card_one", "native"), ("wechat_text", "json")])
        self.assertEqual(sorted(c["trigger_tag"] for c in preview["components_diff"]["added"]), ["card_one", "wechat_text"])
        prompt = preview["injection_prompt"]
        self.assertIn("<card_one> 商品卡片", prompt)
        self.assertIn("<wechat_text> 商品卡片 (JSON payload)", prompt)
        self.assertIn("每轮最多输出 2 个组件", prompt)
        self.assertIn('"name":"string"', prompt)  # the schema as one compact line
        self.assertTrue(all(t["ok"] for t in preview["trial"]))
        self.assertEqual(len(preview["trial"]), 2)

    def test_a_trial_run_reports_sample_data_that_no_longer_fits(self) -> None:
        item = self.make(trigger_tag="drifting")
        channel = self.channel([item["id"]]).json()
        with self.session_factory.begin() as db:
            db.get(TenantComponent, UUID(item["id"])).sample_data = '{"name": 5}'  # a drifted record
        trial = self.client.get(f"/api/v1/channels/{channel['id']}/publish-preview", headers=self.admin).json()["trial"][0]
        self.assertFalse(trial["ok"])
        self.assertIn("name 应是 string", trial["problems"])

    def test_the_snapshot_keeps_the_published_definition_and_the_list_shows_unpublished_changes(self) -> None:
        item = self.make(trigger_tag="frozen")
        channel = self.channel([item["id"]]).json()
        published = self.client.post(f"/api/v1/channels/{channel['id']}/publish", json={}, headers=self.admin).json()
        self.assertEqual(([c["trigger_tag"] for c in published["published_components"]], published["has_unpublished_changes"]), (["frozen"], False))
        edited = self.put(item, data_schema='{"name":"string","tags":"string[]","action":"{ label: string; url: string }"}', trigger_tag="renamed_tag").json()
        self.assertEqual(edited["version"], "0.2")
        shown = self.client.get(f"/api/v1/channels/{channel['id']}", headers=self.admin).json()
        self.assertTrue(shown["has_unpublished_changes"])  # CHN-025
        self.assertEqual([(c["trigger_tag"], c["version"]) for c in shown["published_components"]], [("frozen", "0.1")])  # still the old definition
        preview = self.client.get(f"/api/v1/channels/{channel['id']}/publish-preview", headers=self.admin).json()
        self.assertEqual([c["field"] for c in preview["changes"] if c["section"] == "components"], ["商品卡片"])
        republished = self.client.post(f"/api/v1/channels/{channel['id']}/publish", json={}, headers=self.admin).json()
        self.assertEqual(([c["trigger_tag"] for c in republished["published_components"]], republished["has_unpublished_changes"]), (["renamed_tag"], False))

    def test_a_component_in_a_published_snapshot_cannot_be_deleted(self) -> None:
        item = self.make(trigger_tag="used")
        channel = self.channel([item["id"]]).json()
        self.assertEqual(self.client.get(f"/api/v1/components/{item['id']}", headers=self.admin).json()["used_by"], [])  # selected, not published yet
        self.client.post(f"/api/v1/channels/{channel['id']}/publish", json={}, headers=self.admin)
        shown = self.client.get(f"/api/v1/components/{item['id']}", headers=self.admin).json()
        self.assertEqual((shown["used_by"], shown["can_delete"]), (["Site"], False))
        refused = self.client.delete(f"/api/v1/components/{item['id']}", headers=self.admin)
        self.assertEqual((refused.status_code, refused.json()["code"]), (409, "component.in_use"))
        self.assertEqual(self.update(self.client.get(f"/api/v1/channels/{channel['id']}", headers=self.admin).json(), component_ids=[]).status_code, 200)
        self.client.post(f"/api/v1/channels/{channel['id']}/publish", json={}, headers=self.admin)  # published again without it
        self.assertEqual(self.client.delete(f"/api/v1/components/{item['id']}", headers=self.admin).status_code, 204)

    def test_publishing_is_blocked_when_a_selected_component_was_stopped_after_selection(self) -> None:
        item = self.make(trigger_tag="later_stopped")
        channel = self.channel([item["id"]]).json()
        self.put(item, status="disabled")
        refused = self.client.post(f"/api/v1/channels/{channel['id']}/publish", json={}, headers=self.admin)
        self.assertEqual((refused.status_code, refused.json()["code"]), (409, "channel.publish_blocked"))
        self.assertIn("已停用", refused.json()["detail"])
        self.assertTrue(any("已停用" in p for p in self.client.get(f"/api/v1/channels/{channel['id']}/publish-preview", headers=self.admin).json()["problems"]))

    def test_the_prompt_without_components_and_in_english(self) -> None:
        self.assertEqual(component_service.injection_prompt("网站", []), "# 输出格式\n渠道：网站\n当前渠道无可用组件，请使用纯文本回复。")
        item = {"name_zh": "卡片", "name_en": "Card", "trigger_tag": "t", "schema": '{"a":"string"}', "mode": "text"}
        english = component_service.injection_prompt("Website", [item], lang="en", limit=3)
        self.assertIn("- <t> Card (shown as text on this channel) schema: {\"a\":\"string\"}", english)
        self.assertIn("at most 3 components", english)
        self.assertNotIn("<t>", component_service.injection_prompt("x", [{**item, "mode": "off"}]))  # a closed component is never injected


if __name__ == "__main__":
    unittest.main()
