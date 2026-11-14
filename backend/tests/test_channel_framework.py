"""T-041: channel types, connection config, adapters, publish history and health."""

import unittest
from datetime import UTC, datetime
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select

import test_channels
from beework import channel_adapters, channel_health
from beework.main import app
from beework.models import AuditEvent, User
from beework.models.channels import ChannelInstance, ChannelSnapshot, ChannelType
from beework.models.notifications import InAppNotification
from beework.security import hash_password


class FrameworkTests(test_channels.ChannelBase):
    def setUp(self) -> None:
        super().setUp()
        with self.session_factory.begin() as db:
            db.add(User(email="platform@example.com", display_name="Platform", password_hash=hash_password(test_channels.PASSWORD), is_platform_admin=True))
        self.platform_client = TestClient(app)  # its own cookie jar: logging in as someone else must not end the admin's session
        self.assertEqual(self.platform_client.post("/api/v1/auth/login", json={"email": "platform@example.com", "password": test_channels.PASSWORD}).status_code, 200)
        self.platform = {"X-CSRF-Token": self.platform_client.cookies.get("beework_csrf")}

    # -- helpers ------------------------------------------------------------------------------------------------

    def make(self, headers: dict | None = None, **fields):
        body = {"name": "Channel", "assistant_id": self.assistant_id, "environment": "production", "type": "website", "allowed_origins": [], **fields}
        return self.client.post("/api/v1/channels", json=body, headers=headers or self.admin)

    def created(self, **fields) -> dict:
        response = self.make(**fields)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def publish(self, channel_id: str):
        return self.client.post(f"/api/v1/channels/{channel_id}/publish", json={}, headers=self.admin)

    def stored(self, channel_id: str) -> ChannelInstance:
        with self.session_factory() as db:
            return db.get(ChannelInstance, UUID(channel_id))

    # -- types ---------------------------------------------------------------------------------------------------

    def test_the_catalog_lists_five_types_and_the_plan_decides_which_can_be_created(self) -> None:
        pro = self.client.get("/api/v1/channel-types", headers=self.admin).json()["items"]
        self.assertEqual([t["code"] for t in pro], ["website", "h5", "whatsapp", "wechat", "open-api"])
        self.assertTrue(all(t["available"] for t in pro))
        self.assertEqual(next(t for t in pro if t["code"] == "website")["capabilities"], ["Markdown", "表格", "卡片", "表单", "流式"])
        foreign = self.login("foreign", self.ids["other"])  # the standard plan: widget and API only (CHN-051)
        items = {t["code"]: t for t in self.client.get("/api/v1/channel-types", headers=foreign).json()["items"]}
        self.assertEqual((items["website"]["available"], items["whatsapp"]["unavailable_reason"], items["open-api"]["available"]), (True, "plan", True))

    def test_a_type_outside_the_plan_cannot_be_created(self) -> None:
        foreign = self.login("foreign", self.ids["other"])
        assistant = self.make_assistant(foreign, "Theirs", "p")
        refused = self.make(headers=foreign, type="wechat", assistant_id=assistant, config={"app_id": "wx1"}, secrets={"app_secret": "s3cret"})
        self.assertEqual((refused.status_code, refused.json()["code"]), (403, "channel.type_not_in_plan"))
        self.assertEqual(self.make(headers=foreign, type="open-api", assistant_id=assistant).status_code, 201)

    def test_the_platform_can_disable_a_type_for_new_channels_only(self) -> None:
        existing = self.created(type="open-api", name="Old")
        off = self.platform_client.patch("/api/v1/platform/channel-types/open-api", json={"enabled": False, "version": "1.1"}, headers=self.platform)
        self.assertEqual((off.status_code, off.json()["enabled"], off.json()["version"]), (200, False, "1.1"))
        refused = self.make(type="open-api", name="New")
        self.assertEqual((refused.status_code, refused.json()["code"]), (403, "channel.type_disabled"))
        listed = {t["code"]: t for t in self.client.get("/api/v1/channel-types", headers=self.admin).json()["items"]}
        self.assertEqual((listed["open-api"]["available"], listed["open-api"]["unavailable_reason"]), (False, "disabled"))
        self.assertEqual(self.client.get(f"/api/v1/channels/{existing['id']}", headers=self.admin).status_code, 200)  # the instance is untouched
        self.assertEqual(self.client.put(f"/api/v1/channels/{existing['id']}", json={"type": "open-api", "name": "Old2", "assistant_id": self.assistant_id, "row_version": existing["row_version"]}, headers=self.admin).status_code, 200)
        self.platform_client.patch("/api/v1/platform/channel-types/open-api", json={"enabled": True}, headers=self.platform)
        self.assertEqual(self.make(type="open-api", name="New").status_code, 201)
        with self.session_factory() as db:
            self.assertEqual(db.scalar(select(AuditEvent.action).where(AuditEvent.action == "channel_type.updated").limit(1)), "channel_type.updated")

    def test_only_platform_administrators_edit_types_and_unknown_types_are_refused(self) -> None:
        self.assertEqual(self.client.patch("/api/v1/platform/channel-types/website", json={"enabled": False}, headers=self.admin).status_code, 403)
        self.assertEqual(self.client.get("/api/v1/platform/channel-types", headers=self.admin).status_code, 403)
        self.assertEqual(self.platform_client.patch("/api/v1/platform/channel-types/telegram", json={"enabled": False}, headers=self.platform).status_code, 404)
        self.assertEqual(self.make(type="telegram").status_code, 422)
        self.assertEqual(len(self.platform_client.get("/api/v1/platform/channel-types", headers=self.platform).json()["items"]), 5)

    # -- connection config ---------------------------------------------------------------------------------

    def test_secrets_are_encrypted_masked_and_kept_when_left_blank(self) -> None:
        channel = self.created(type="wechat", config={"app_id": "wx123"}, secrets={"app_secret": "super-secret-9f3a"})
        self.assertEqual((channel["config"], channel["secret_masks"]), ({"app_id": "wx123"}, {"app_secret": "••••9f3a"}))
        self.assertNotIn("super-secret", str(channel))
        self.assertNotIn("super-secret", str(self.stored(channel["id"]).secret_config))  # not stored in clear
        update = lambda secrets: self.client.put(  # noqa: E731
            f"/api/v1/channels/{channel['id']}", headers=self.admin,
            json={"type": "wechat", "name": "Channel", "assistant_id": self.assistant_id, "config": {"app_id": "wx999"}, "secrets": secrets, "row_version": self.client.get(f"/api/v1/channels/{channel['id']}", headers=self.admin).json()["row_version"]},
        )
        kept = update({"app_secret": ""}).json()
        self.assertEqual((kept["config"]["app_id"], kept["secret_masks"]), ("wx999", {"app_secret": "••••9f3a"}))
        self.assertEqual(update({"app_secret": "another-0001"}).json()["secret_masks"], {"app_secret": "••••0001"})

    def test_only_the_parameters_of_the_type_are_accepted(self) -> None:
        self.assertEqual(self.make(type="wechat", config={"phone_number_id": "1"}).status_code, 422)
        self.assertEqual(self.make(type="wechat", config={"app_secret": "x"}).status_code, 422)  # a secret is not a plain parameter
        self.assertEqual(self.make(type="wechat", secrets={"app_id": "x"}).status_code, 422)
        self.assertEqual(self.make(type="open-api", allowed_origins=["https://a.example.com"]).status_code, 422)  # no origins on this type
        self.assertEqual(self.make(type="h5", allowed_origins=["not a url"]).status_code, 422)

    def test_the_type_cannot_change_after_publishing_but_a_draft_can_switch_and_resets(self) -> None:
        draft = self.created(type="wechat", config={"app_id": "wx1"}, secrets={"app_secret": "abcdefgh"})
        switched = self.client.put(f"/api/v1/channels/{draft['id']}", headers=self.admin, json={"type": "open-api", "name": "Channel", "assistant_id": self.assistant_id, "row_version": draft["row_version"]})
        self.assertEqual((switched.status_code, switched.json()["type"], switched.json()["config"], switched.json()["secret_masks"]), (200, "open-api", {}, {}))
        live = self.publish(draft["id"]).json()
        locked = self.client.put(f"/api/v1/channels/{draft['id']}", headers=self.admin, json={"type": "website", "name": "Channel", "assistant_id": self.assistant_id, "allowed_origins": ["https://a.example.com"], "row_version": live["row_version"]})
        self.assertEqual((locked.status_code, locked.json()["code"]), (409, "channel.type_locked"))

    # -- publish -----------------------------------------------------------------------------------------------

    def test_publishing_needs_the_connection_to_be_complete_for_the_type(self) -> None:
        incomplete = self.created(type="whatsapp", config={"phone_number_id": "123"})
        refused = self.publish(incomplete["id"])
        self.assertEqual((refused.status_code, refused.json()["code"]), (422, "channel.incomplete"))
        self.assertIn("Access token", refused.json()["detail"])
        ready = self.created(type="whatsapp", name="WA", config={"phone_number_id": "123"}, secrets={"access_token": "EAAB-token-1234"})
        published = self.publish(ready["id"])
        self.assertEqual((published.status_code, published.json()["status"], published.json()["integration"]["kind"]), (200, "active", "webhook"))
        self.assertIsNone(published.json()["embed_code"])  # only the website type has a script tag

    def test_the_integration_snippet_depends_on_the_type(self) -> None:
        kinds = {}
        for code, extra in (
            ("website", {"allowed_origins": ["https://www.example.com"]}), ("h5", {"config": {"h5_path": "/h5/help"}, "allowed_origins": ["https://m.example.com"]}),
            ("open-api", {"config": {"base_path": "/v1"}}),
        ):
            channel = self.created(type=code, name=code, **extra)
            self.assertIsNone(channel["integration"])  # nothing before publishing
            published = self.publish(channel["id"])
            self.assertEqual(published.status_code, 200, published.text)
            kinds[code] = published.json()["integration"]
        self.assertEqual({k: v["kind"] for k, v in kinds.items()}, {"website": "script", "h5": "link", "open-api": "curl"})
        self.assertIn("data-site-key", kinds["website"]["code"])
        self.assertTrue(kinds["h5"]["code"].endswith("/h5/acme/h5/help"))  # the tenant's namespace, then the path (T-044)
        self.assertIn("Bearer", kinds["open-api"]["code"])

    def test_every_publish_keeps_an_immutable_history_row_and_the_draft_shows_unpublished_changes(self) -> None:
        channel = self.created(allowed_origins=["https://www.example.com"])
        first = self.publish(channel["id"]).json()
        self.assertEqual((first["published_version"], first["has_unpublished_changes"]), ("v1.0", False))
        edited = self.client.put(f"/api/v1/channels/{channel['id']}", headers=self.admin, json={"type": "website", "name": "Channel", "assistant_id": self.assistant_id, "allowed_origins": ["https://www.example.com", "https://b.example.com"], "row_version": first["row_version"]}).json()
        self.assertTrue(edited["has_unpublished_changes"])
        with self.session_factory() as db:
            before = [(s.version_seq, s.snapshot["allowed_origins"]) for s in db.scalars(select(ChannelSnapshot).where(ChannelSnapshot.channel_id == UUID(channel["id"])))]
        self.assertEqual(before, [(1, ["https://www.example.com"])])  # the edit changed no published version
        self.publish(channel["id"])
        with self.session_factory() as db:
            rows = sorted((s.version_seq, len(s.snapshot["allowed_origins"])) for s in db.scalars(select(ChannelSnapshot).where(ChannelSnapshot.channel_id == UUID(channel["id"]))))
        self.assertEqual(rows, [(1, 1), (2, 2)])

    def test_a_connection_parameter_change_is_unpublished_until_published_again(self) -> None:
        channel = self.created(type="h5", config={"h5_path": "/a"}, allowed_origins=["https://m.example.com"])
        live = self.publish(channel["id"]).json()
        changed = self.client.put(f"/api/v1/channels/{channel['id']}", headers=self.admin, json={"type": "h5", "name": "Channel", "assistant_id": self.assistant_id, "config": {"h5_path": "/b"}, "allowed_origins": ["https://m.example.com"], "row_version": live["row_version"]}).json()
        self.assertTrue(changed["has_unpublished_changes"])
        self.assertEqual(self.stored(channel["id"]).published_snapshot["config"], {"h5_path": "/a"})
        preview = self.client.get(f"/api/v1/channels/{channel['id']}/publish-preview", headers=self.admin).json()
        self.assertEqual(([c["field"] for c in preview["changes"]], preview["version_from"], preview["version_to"], preview["first_publish"]), (["h5_path"], "v1.0", "v1.1", False))

    def test_the_publish_preview_lists_what_blocks_publishing_and_has_no_components_yet(self) -> None:
        channel = self.created(type="wechat", config={"app_id": "wx1"})
        preview = self.client.get(f"/api/v1/channels/{channel['id']}/publish-preview", headers=self.admin).json()
        self.assertTrue(preview["first_publish"])
        self.assertTrue(any("App secret" in p for p in preview["problems"]))
        self.assertEqual((preview["components"], preview["trial"], preview["injection_prompt"]), ([], [], None))
        self.assertEqual(self.client.get(f"/api/v1/channels/{channel['id']}/publish-preview", headers=self.login("viewer")).status_code, 403)
        self.assertEqual(self.client.get(f"/api/v1/channels/{channel['id']}/publish-preview", headers=self.login("foreign", self.ids["other"])).status_code in (403, 404), True)

    # -- widget & other types ---------------------------------------------------------------------------------

    def test_the_widget_only_serves_website_channels(self) -> None:
        channel = self.created(type="h5", config={"h5_path": "/a"}, allowed_origins=["https://www.example.com"])
        live = self.publish(channel["id"]).json()
        self.client.cookies.clear()
        response = self.client.post("/v1/widget/session", json={"site_key": live["site_key"]}, headers={"Origin": "https://www.example.com"})
        self.assertEqual(response.status_code, 404)  # the widget's site lookup is for website channels only

    # -- health -------------------------------------------------------------------------------------------------

    def test_three_failed_checks_mark_the_channel_abnormal_and_a_success_recovers_it(self) -> None:
        channel = self.created(allowed_origins=["https://www.example.com"])
        self.publish(channel["id"])
        with self.session_factory.begin() as db:
            row = db.get(ChannelInstance, UUID(channel["id"]))
            row.published_snapshot = {**row.published_snapshot, "allowed_origins": []}  # simulates a snapshot the adapter finds broken
        outcomes = []
        for _ in range(3):
            with self.session_factory.begin() as db:
                outcomes.append(channel_health.check_channel(db, db.get(ChannelInstance, UUID(channel["id"]))))
        self.assertEqual(outcomes, [None, None, "error"])
        with self.session_factory() as db:
            row = db.get(ChannelInstance, UUID(channel["id"]))
            self.assertEqual((row.status, row.health_failures, row.health_error), ("error", 3, "已发布的快照没有允许域名"))
            self.assertTrue(db.scalars(select(InAppNotification).where(InAppNotification.type == "channel.unhealthy")).first())
        with self.session_factory.begin() as db:
            row = db.get(ChannelInstance, UUID(channel["id"]))
            row.published_snapshot = {**row.published_snapshot, "allowed_origins": ["https://www.example.com"]}
            self.assertEqual(channel_health.check_channel(db, row, datetime.now(UTC)), "recovered")
        with self.session_factory() as db:
            row = db.get(ChannelInstance, UUID(channel["id"]))
            self.assertEqual((row.status, row.health_failures, row.health_error), ("active", 0, None))

    def test_the_health_job_skips_drafts_and_adapters_that_cannot_judge(self) -> None:
        self.created(type="wechat", name="Draft")  # a draft is never checked
        live = self.created(type="whatsapp", name="WA", config={"phone_number_id": "1"}, secrets={"access_token": "tokentoken"})
        self.publish(live["id"])
        with self.session_factory.begin() as db:
            channel_health.channel_health_job(db, {})
        with self.session_factory() as db:
            rows = {c.name: (c.status, c.health_failures, c.health_checked_at) for c in db.scalars(select(ChannelInstance))}
        self.assertEqual(rows["WA"], ("active", 0, None))  # the generic adapter has nothing to call

    # -- adapters ----------------------------------------------------------------------------------------------

    def test_tables_become_lists_when_the_channel_cannot_show_them(self) -> None:
        text = "订单如下：\n| 订单 | 状态 |\n|---|---|\n| A1 | 已发货 |\n| A2 | 待付款 |\n谢谢"
        adapter = channel_adapters.adapter_for("whatsapp")
        self.assertEqual(adapter.render(text, set()), "订单如下：\n- 订单：A1；状态：已发货\n- 订单：A2；状态：待付款\n谢谢")
        self.assertEqual(adapter.render(text, {"表格"}), text)

    def test_deleting_a_channel_removes_its_history(self) -> None:
        channel = self.created(allowed_origins=["https://www.example.com"])
        self.publish(channel["id"])
        self.assertEqual(self.client.delete(f"/api/v1/channels/{channel['id']}", headers=self.admin).status_code, 204)
        with self.session_factory() as db:
            self.assertEqual(db.scalars(select(ChannelSnapshot)).all(), [])
            self.assertIsNone(db.get(ChannelType, "website"))  # types are platform data, untouched by a tenant's channel


if __name__ == "__main__":
    unittest.main()
