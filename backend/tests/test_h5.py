"""T-044: the H5 channel — path rules, the served page and its iframe policy, the session, and a conversation through it."""

import unittest
from unittest.mock import patch
from uuid import UUID

from sqlalchemy import select

import test_channels
from beework.config import settings
from beework.models.conversations import Conversation

ORIGIN = "https://api.example.com"  # the address the pages are served from in these tests
EMBEDDER = "https://www.example.com"


class H5Tests(test_channels.ChannelBase):
    def setUp(self) -> None:
        super().setUp()
        patcher = patch.object(settings, "public_api_url", ORIGIN)
        patcher.start()
        self.addCleanup(patcher.stop)

    def h5(self, headers=None, **fields) -> dict:
        body = {"type": "h5", "name": "H5", "assistant_id": self.assistant_id, "environment": "production", "config": {"h5_path": "/help"}, "allowed_origins": [EMBEDDER], **fields}
        response = self.client.post("/api/v1/channels", json=body, headers=headers or self.admin)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def live(self, **fields) -> dict:
        channel = self.h5(**fields)
        published = self.client.post(f"/api/v1/channels/{channel['id']}/publish", json={}, headers=self.admin)
        self.assertEqual(published.status_code, 200, published.text)
        return published.json()

    def session(self, origin: str | None = ORIGIN, **body):
        self.client.cookies.clear()
        return self.client.post("/v1/h5/session", json={"tenant": "acme", "path": "/help", **body}, headers={"Origin": origin} if origin else {})

    # -- connection and paths ----------------------------------------------------------------------------------

    def test_the_path_is_normalised_unique_within_the_tenant_and_the_link_names_the_tenant(self) -> None:
        channel = self.h5(config={"h5_path": "/help/"})
        self.assertEqual(channel["config"], {"h5_path": "/help"})
        clash = self.client.post("/api/v1/channels", json={"type": "h5", "name": "Second", "assistant_id": self.assistant_id, "config": {"h5_path": "help"}, "allowed_origins": [EMBEDDER]}, headers=self.admin)
        self.assertEqual((clash.status_code, clash.json()["code"]), (409, "channel.h5_path_taken"))
        other = self.h5(name="Other", config={"h5_path": "/other"})  # a different path is fine, and an edit may keep its own path
        kept = self.client.put(f"/api/v1/channels/{other['id']}", json={"type": "h5", "name": "Other", "assistant_id": self.assistant_id, "config": {"h5_path": "/other"}, "allowed_origins": [EMBEDDER], "row_version": other["row_version"]}, headers=self.admin)
        self.assertEqual(kept.status_code, 200, kept.text)
        foreign = self.login("foreign", self.ids["other"])
        theirs = self.client.post("/api/v1/assistants", json={"name": "Theirs", "description": "d", "config": {"prompt": "p"}}, headers=foreign).json()["id"]
        same = self.client.post("/api/v1/channels", json={"type": "h5", "name": "Theirs", "assistant_id": theirs, "config": {"h5_path": "/help"}, "allowed_origins": [EMBEDDER]}, headers=foreign)
        self.assertEqual(same.status_code, 201, same.text)  # unique per tenant, not across tenants

    def test_the_path_and_the_embed_domains_are_checked_before_publishing(self) -> None:
        for path in ("/", "help", "/a b", "/中文"):
            channel = self.h5(config={"h5_path": path}, name=f"c{path}") if path in ("/",) else None
            if channel is not None:
                refused = self.client.post(f"/api/v1/channels/{channel['id']}/publish", json={}, headers=self.admin)
                self.assertEqual((refused.status_code, refused.json()["code"]), (422, "channel.incomplete"), path)
        none = self.h5(name="No domains", config={"h5_path": "/nod"}, allowed_origins=[])
        refused = self.client.post(f"/api/v1/channels/{none['id']}/publish", json={}, headers=self.admin)
        self.assertEqual(refused.status_code, 422)
        self.assertIn("允许域名", refused.json()["detail"])

    def test_publishing_gives_the_link_and_an_iframe_snippet(self) -> None:
        live = self.live()
        self.assertEqual((live["integration"]["kind"], live["integration"]["code"]), ("link", f"{ORIGIN}/h5/acme/help"))
        self.assertEqual(live["integration"]["embed"], f'<iframe src="{ORIGIN}/h5/acme/help" style="border:0;width:100%;height:100%" allow="clipboard-write"></iframe>')

    # -- the page --------------------------------------------------------------------------------------------

    def test_the_page_is_served_for_a_live_channel_and_only_allowed_sites_may_embed_it(self) -> None:
        self.live(allowed_origins=[EMBEDDER, "https://m.example.com"])
        self.client.cookies.clear()
        page = self.client.get("/h5/acme/help")
        self.assertEqual(page.status_code, 200)
        self.assertIn('data-h5="acme/help"', page.text)
        self.assertIn('name="viewport"', page.text)
        self.assertIn(f"{ORIGIN}/v1/widget.js", page.text)
        policy = page.headers["content-security-policy"]
        self.assertEqual(policy, f"frame-ancestors 'self' {EMBEDDER} https://m.example.com")  # nobody else may frame it
        self.assertNotIn("evil", policy)
        self.assertEqual(self.client.get("/h5/acme/help/").status_code, 200)  # a trailing slash is the same page

    def test_unknown_unpublished_disabled_and_other_tenants_pages_are_404(self) -> None:
        draft = self.h5()
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/h5/acme/help").status_code, 404)  # not published
        self.admin = self.login("admin")
        self.client.post(f"/api/v1/channels/{draft['id']}/publish", json={}, headers=self.admin)
        self.assertEqual(self.client.get("/h5/acme/help").status_code, 200)
        self.assertEqual(self.client.get("/h5/other/help").status_code, 404)  # another tenant's namespace
        self.assertEqual(self.client.get("/h5/acme/nope").status_code, 404)
        self.assertEqual(self.client.get("/h5/nobody/help").status_code, 404)
        self.admin = self.login("admin")
        self.client.post(f"/api/v1/channels/{draft['id']}/disable", headers=self.admin)
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/h5/acme/help").status_code, 404)

    def test_the_page_is_not_served_for_website_channels(self) -> None:
        site = self.client.post("/api/v1/channels", json={"type": "website", "name": "Site", "assistant_id": self.assistant_id, "allowed_origins": [EMBEDDER], "config": {}}, headers=self.admin).json()
        self.client.post(f"/api/v1/channels/{site['id']}/publish", json={}, headers=self.admin)
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/h5/acme/").status_code, 404)

    # -- session and conversation ------------------------------------------------------------------------------

    def test_the_session_carries_the_frozen_experience(self) -> None:
        self.client.put("/api/v1/channel-types/h5/tenant-experience", json={"values": {"radius": 8, "tables": False}}, headers=self.admin)
        self.live(experience={"entry": "drawer", "welcome": "你好，H5"})
        response = self.session()
        self.assertEqual(response.status_code, 200, response.text)
        config = response.json()["config"]
        self.assertEqual((config["entry"], config["radius"], config["tables"], config["welcome"], config["streaming"]), ("drawer", 8, False, "你好，H5", True))
        self.assertEqual(config["primary_color"], "#3568d4")  # the H5 platform default

    def test_the_session_only_works_from_the_pages_own_origin(self) -> None:
        self.live()
        for origin, expected in ((None, 403), (EMBEDDER, 403), ("https://evil.example.com", 403), (ORIGIN, 200)):
            response = self.session(origin)
            self.assertEqual(response.status_code, expected, (origin, response.text))
        self.assertEqual(self.session(path="/missing").status_code, 404)
        self.assertEqual(self.session(tenant="other").status_code, 404)
        self.assertEqual(self.client.post("/v1/h5/session", json={"tenant": "ACME!", "path": "no-slash"}, headers={"Origin": ORIGIN}).status_code, 422)

    def test_a_conversation_runs_through_the_h5_token_and_is_recorded_as_h5(self) -> None:
        live = self.live()
        token = self.session().json()["token"]
        self.client.cookies.clear()
        headers = {"Authorization": f"Bearer {token}", "Origin": ORIGIN}
        reply = self.client.post("/v1/widget/chat", json={"message": "你好"}, headers=headers)
        self.assertEqual(reply.status_code, 200, reply.text)
        conversation_id = reply.json()["conversation_id"]
        with self.session_factory() as db:
            conversation = db.scalar(select(Conversation).where(Conversation.id == UUID(conversation_id)))
            self.assertEqual((conversation.source, str(conversation.channel_instance_id)), ("h5", live["id"]))
        messages = self.client.get(f"/v1/widget/conversations/{conversation_id}/messages", headers=headers)
        self.assertEqual(messages.status_code, 200, messages.text)
        self.assertEqual([m["role"] for m in messages.json()["messages"]], ["user", "assistant"])
        # the token cannot be replayed from another site, and the page's origin is the only one an H5 channel accepts
        self.assertEqual(self.client.post("/v1/widget/chat", json={"message": "x"}, headers={"Authorization": f"Bearer {token}", "Origin": EMBEDDER}).status_code, 403)

    def test_a_disabled_channel_stops_answering_at_once(self) -> None:
        live = self.live()
        token = self.session().json()["token"]
        self.admin = self.login("admin")
        self.client.post(f"/api/v1/channels/{live['id']}/disable", headers=self.admin)
        self.client.cookies.clear()
        stopped = self.client.post("/v1/widget/chat", json={"message": "你好"}, headers={"Authorization": f"Bearer {token}", "Origin": ORIGIN})
        self.assertEqual((stopped.status_code, stopped.json()["error"]["code"]), (403, "channel_unavailable"))


if __name__ == "__main__":
    unittest.main()
