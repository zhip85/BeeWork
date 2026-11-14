"""T-042: the four layers of channel experience — the merge function (table-driven), the layer endpoints and publishing."""

import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

import test_components
from beework import experience as rules
from beework.errors import ApiError
from beework.main import app
from beework.models.assistants import Assistant


class MergeTests(unittest.TestCase):
    PLATFORM = {"primary_color": "#6558d9", "theme": "light", "radius": 12, "welcome": "平台欢迎", "entry": "bottom-right", "cards": True, "tables": True, "streaming": True}

    def merged(self, **layers):
        return rules.resolve("website", platform=self.PLATFORM, **layers)

    def test_each_layer_overrides_the_ones_above_and_reports_its_source(self) -> None:
        cases = [
            ({}, {"primary_color": "platform", "welcome": "platform", "radius": "platform"}),
            ({"tenant": {"primary_color": "#111111"}}, {"primary_color": "tenant", "welcome": "platform"}),
            ({"tenant": {"primary_color": "#111111"}, "assistant": {"primary_color": "#222222"}}, {"primary_color": "assistant"}),
            ({"tenant": {"primary_color": "#111111"}, "assistant": {"primary_color": "#222222"}, "instance": {"primary_color": "#333333"}}, {"primary_color": "instance"}),
            ({"instance": {"primary_color": "#333333"}, "tenant": {"welcome": "租户欢迎"}}, {"primary_color": "instance", "welcome": "tenant"}),
        ]
        for layers, expected in cases:
            _values, sources = self.merged(**layers)
            self.assertEqual({k: sources[k] for k in expected}, expected, layers)

    def test_a_layer_that_does_not_set_a_field_inherits_it(self) -> None:
        values, sources = self.merged(tenant={"radius": 8}, instance={"cards": False})
        self.assertEqual((values["radius"], values["cards"], values["tables"], values["primary_color"]), (8, False, True, "#6558d9"))
        self.assertEqual((sources["radius"], sources["cards"], sources["tables"]), ("tenant", "instance", "platform"))

    def test_false_is_a_value_and_empty_means_inherit(self) -> None:
        values, sources = self.merged(tenant={"cards": False}, instance={"cards": None, "welcome": ""})
        self.assertEqual((values["cards"], sources["cards"], values["welcome"], sources["welcome"]), (False, "tenant", "平台欢迎", "platform"))

    def test_the_agents_greeting_is_its_welcome_text_unless_its_channel_settings_set_one(self) -> None:
        values, sources = self.merged(tenant={"welcome": "租户欢迎"}, greeting="智能体问候")
        self.assertEqual((values["welcome"], sources["welcome"]), ("智能体问候", "assistant"))
        values, sources = self.merged(assistant={"welcome": "渠道配置里的"}, greeting="智能体问候")
        self.assertEqual(values["welcome"], "渠道配置里的")
        values, _ = self.merged(assistant={"welcome": "渠道配置里的"}, greeting="智能体问候", instance={"welcome": "渠道自己的"})
        self.assertEqual(values["welcome"], "渠道自己的")

    def test_the_open_api_has_no_colour_radius_entry_or_welcome(self) -> None:
        self.assertEqual({f.key for f in rules.fields_for("open-api")}, {"quick_replies", "cards", "tables", "forms", "streaming"})
        values, _ = rules.resolve("open-api", platform={"primary_color": "#000000", "cards": True, "streaming": True}, tenant={"radius": 8})
        self.assertEqual(values, {"cards": True, "streaming": True})  # what does not apply is never reported
        self.assertEqual([f.options for f in rules.fields_for("h5") if f.key == "entry"], [("fullscreen", "drawer")])
        self.assertEqual([f.options for f in rules.fields_for("website") if f.key == "entry"], [("bottom-right", "bottom-left")])

    def test_layer_values_are_validated_per_type(self) -> None:
        self.assertEqual(rules.validate_layer("website", {"primary_color": "#ABCDEF", "radius": 16, "theme": "dark", "cards": False, "welcome": " 你好 "}), {"primary_color": "#abcdef", "radius": 16, "theme": "dark", "cards": False, "welcome": "你好"})
        self.assertEqual(rules.validate_layer("website", {"theme": "", "radius": None, "welcome": ""}), {})  # empty = inherit
        for bad in ({"primary_color": "red"}, {"radius": 10}, {"theme": "neon"}, {"entry": "fullscreen"}, {"welcome": "字" * 201}, {"cards": "yes"}, {"unknown": 1}):
            with self.assertRaises(ApiError, msg=str(bad)) as caught:
                rules.validate_layer("website", bad)
            self.assertEqual(caught.exception.code, "experience.invalid")
        with self.assertRaises(ApiError):
            rules.validate_layer("open-api", {"primary_color": "#000000"})
        self.assertEqual(rules.validate_layer("h5", {"entry": "drawer"}), {"entry": "drawer"})
        self.assertEqual(rules.validate_layer("website", {"assistant_name": " 客服 "}, allow=("assistant_name",)), {"assistant_name": "客服"})


class ExperienceApiTests(test_components.ComponentBase):
    def channel(self, **fields):
        body = {"type": "website", "name": "Site", "assistant_id": self.assistant_id, "environment": "production", "allowed_origins": ["https://www.example.com"], **fields}
        response = self.client.post("/api/v1/channels", json=body, headers=self.admin)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def view(self, **params):
        response = self.client.get("/api/v1/experience/website", params=params, headers=self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def put(self, path: str, values: dict, **extra):
        return self.client.put(path, json={"values": values, **extra}, headers=self.admin)

    def test_the_editor_payload_has_the_fields_the_layers_and_the_merge(self) -> None:
        view = self.view()
        self.assertEqual(view["channel_type_name"], "网站")
        self.assertEqual({f["key"] for f in view["fields"]}, {"primary_color", "theme", "radius", "entry", "welcome", "quick_replies", "cards", "tables", "forms", "streaming"})
        self.assertEqual((view["values"]["primary_color"], view["sources"]["primary_color"], view["layers"]["tenant"]), ("#6558d9", "platform", {}))
        self.assertEqual(self.client.get("/api/v1/experience/telegram", headers=self.admin).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/experience/h5", params={"channel_id": self.channel()["id"]}, headers=self.admin).status_code, 409)  # wrong type

    def test_the_tenant_agent_and_channel_layers_stack(self) -> None:
        self.assertEqual(self.put("/api/v1/channel-types/website/tenant-experience", {"primary_color": "#111111", "radius": 8, "cards": False}).status_code, 200)
        agent = self.put(f"/api/v1/assistants/{self.assistant_id}/channel-experience/website", {"primary_color": "#222222", "welcome": "助手欢迎"})
        self.assertEqual(agent.status_code, 200, agent.text)
        channel = self.channel(experience={"primary_color": "#333333"})
        view = self.view(channel_id=channel["id"])
        self.assertEqual(
            {k: (view["values"][k], view["sources"][k]) for k in ("primary_color", "radius", "cards", "welcome", "tables")},
            {"primary_color": ("#333333", "instance"), "radius": (8, "tenant"), "cards": (False, "tenant"), "welcome": ("助手欢迎", "assistant"), "tables": (True, "platform")},
        )
        shown = self.client.get(f"/api/v1/channels/{channel['id']}", headers=self.admin).json()
        self.assertEqual((shown["effective_experience"]["radius"], shown["experience_sources"]["radius"]), (8, "tenant"))

    def test_restoring_inheritance_removes_the_override(self) -> None:
        self.put("/api/v1/channel-types/website/tenant-experience", {"radius": 8})
        saved = self.put("/api/v1/channel-types/website/tenant-experience", {"radius": None, "primary_color": "#111111"}).json()
        self.assertEqual((saved["layers"]["tenant"], saved["sources"]["radius"]), ({"primary_color": "#111111"}, "platform"))
        self.assertEqual(self.put("/api/v1/channel-types/website/tenant-experience", {}).json()["layers"]["tenant"], {})
        agent = self.put(f"/api/v1/assistants/{self.assistant_id}/channel-experience/website", {"radius": 16}).json()
        self.assertEqual(agent["layers"]["assistant"], {"radius": 16})
        self.assertEqual(self.put(f"/api/v1/assistants/{self.assistant_id}/channel-experience/website", {}).json()["layers"]["assistant"], {})

    def test_invalid_values_and_fields_of_other_types_are_refused(self) -> None:
        for bad in ({"radius": 10}, {"primary_color": "blue"}, {"entry": "fullscreen"}, {"nope": 1}):
            self.assertEqual(self.put("/api/v1/channel-types/website/tenant-experience", bad).status_code, 422, bad)
        self.assertEqual(self.put("/api/v1/channel-types/open-api/tenant-experience", {"primary_color": "#000000"}).status_code, 422)
        self.assertEqual(self.put("/api/v1/channel-types/telegram/tenant-experience", {}).status_code, 404)
        self.assertEqual(self.client.post("/api/v1/channels", json={"type": "open-api", "name": "API", "assistant_id": self.assistant_id, "environment": "production", "experience": {"welcome": "hi"}}, headers=self.admin).status_code, 422)

    def test_a_published_channel_keeps_its_frozen_experience_until_it_is_published_again(self) -> None:
        channel = self.channel()
        live = self.client.post(f"/api/v1/channels/{channel['id']}/publish", json={}, headers=self.admin).json()
        self.assertFalse(live["has_unpublished_changes"])
        self.put("/api/v1/channel-types/website/tenant-experience", {"primary_color": "#123456", "radius": 16})
        shown = self.client.get(f"/api/v1/channels/{channel['id']}", headers=self.admin).json()
        self.assertTrue(shown["has_unpublished_changes"])  # the draft would freeze other values than the live ones
        self.assertEqual(shown["effective_experience"]["primary_color"], "#123456")
        with self.session_factory() as db:
            from beework.models.channels import ChannelInstance

            frozen = db.scalar(select(ChannelInstance.published_snapshot).where(ChannelInstance.site_key == channel["site_key"]))["experience"]
        self.assertEqual((frozen["primary_color"], frozen["radius"]), ("#6558d9", 12))  # visitors still get what was published
        self.client.post(f"/api/v1/channels/{channel['id']}/publish", json={}, headers=self.admin)
        again = self.client.get(f"/api/v1/channels/{channel['id']}", headers=self.admin).json()
        self.assertFalse(again["has_unpublished_changes"])
        with self.session_factory() as db:
            frozen = db.scalar(select(ChannelInstance.published_snapshot).where(ChannelInstance.site_key == channel["site_key"]))["experience"]
        self.assertEqual((frozen["primary_color"], frozen["radius"], frozen["theme"], frozen["cards"]), ("#123456", 16, "light", True))  # every applicable setting is frozen

    def test_the_channel_experience_action_writes_only_the_channel_layer_with_a_version_check(self) -> None:
        channel = self.channel()
        saved = self.put(f"/api/v1/channels/{channel['id']}/experience", {"welcome": "渠道欢迎", "streaming": False}, row_version=channel["row_version"])
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["layers"]["instance"], {"welcome": "渠道欢迎", "streaming": False})
        stale = self.put(f"/api/v1/channels/{channel['id']}/experience", {"welcome": "x"}, row_version=channel["row_version"])
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.put(f"/api/v1/channels/{channel['id']}/experience", {"radius": 10}).status_code, 422)

    def test_switching_the_type_of_a_draft_channel_clears_its_overrides(self) -> None:
        channel = self.channel(experience={"primary_color": "#333333", "welcome": "x"})
        switched = self.client.put(f"/api/v1/channels/{channel['id']}", headers=self.admin, json={"type": "h5", "name": "Site", "assistant_id": self.assistant_id, "config": {"h5_path": "/a"}, "allowed_origins": ["https://m.example.com"], "row_version": channel["row_version"]})
        self.assertEqual((switched.status_code, switched.json()["experience"]), (200, {}))

    def test_the_platform_default_is_validated_and_only_reaches_what_nothing_overrides(self) -> None:
        platform = TestClient(app)
        self.assertEqual(platform.post("/api/v1/auth/login", json={"email": "platform@example.com", "password": "correct horse battery"}).status_code, 200)
        headers = {"X-CSRF-Token": platform.cookies.get("beework_csrf")}
        self.assertEqual(platform.patch("/api/v1/platform/channel-types/website", json={"default_experience": {"radius": 10}}, headers=headers).status_code, 422)
        self.put("/api/v1/channel-types/website/tenant-experience", {"primary_color": "#111111"})
        ok = platform.patch("/api/v1/platform/channel-types/website", json={"default_experience": {"primary_color": "#999999", "radius": 16}}, headers=headers)
        self.assertEqual(ok.status_code, 200, ok.text)
        view = self.view()
        self.assertEqual((view["values"]["primary_color"], view["sources"]["primary_color"]), ("#111111", "tenant"))  # the tenant's own colour stands
        self.assertEqual((view["values"]["radius"], view["sources"]["radius"]), (16, "platform"))
        self.assertEqual(view["layers"]["platform"]["radius"], 16)

    def test_permissions_and_tenant_isolation(self) -> None:
        viewer = self.login("viewer")
        self.assertEqual(self.client.get("/api/v1/experience/website", headers=viewer).status_code, 200)
        self.assertEqual(self.put_as(viewer, "/api/v1/channel-types/website/tenant-experience", {"radius": 8}), 403)
        self.admin = self.login("admin")
        mine = self.channel()
        self.assertEqual(self.put("/api/v1/channel-types/website/tenant-experience", {"radius": 8}).status_code, 200)
        foreign = self.login("foreign", self.ids["other"])
        self.assertEqual(self.client.get("/api/v1/experience/website", params={"channel_id": mine["id"]}, headers=foreign).status_code, 404)
        self.assertEqual(self.client.put(f"/api/v1/channels/{mine['id']}/experience", json={"values": {}}, headers=foreign).status_code, 404)
        self.assertEqual(self.client.put(f"/api/v1/assistants/{self.assistant_id}/channel-experience/website", json={"values": {}}, headers=foreign).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/experience/website", headers=foreign).json()["layers"]["tenant"], {})  # another tenant's default is not visible

    def put_as(self, headers: dict, path: str, values: dict) -> int:
        return self.client.put(path, json={"values": values}, headers=headers).status_code

    def test_editing_the_agent_elsewhere_does_not_wipe_its_channel_settings(self) -> None:
        self.put(f"/api/v1/assistants/{self.assistant_id}/channel-experience/website", {"radius": 16})
        current = self.client.get(f"/api/v1/assistants/{self.assistant_id}", headers=self.admin).json()
        body = {"name": current["name"], "description": "改了描述", "organization_id": current["organization_id"], "category": current["category"], "visibility": current["visibility"], "config": {k: v for k, v in current["config"].items() if k != "channel_experience"}, "row_version": current["row_version"]}
        saved = self.client.put(f"/api/v1/assistants/{self.assistant_id}", json=body, headers=self.admin)
        self.assertEqual(saved.status_code, 200, saved.text)
        with self.session_factory() as db:
            self.assertEqual(db.get(Assistant, __import__("uuid").UUID(self.assistant_id)).draft_config["channel_experience"], {"website": {"radius": 16}})


if __name__ == "__main__":
    unittest.main()
