"""T-143: employee portal conversations and projects — real runtime, privacy, features, availability."""

import json
import unittest
from uuid import UUID

from sqlalchemy import select

import test_open_api
from beework import entitlements
from beework.models import AuditEvent, Membership, Organization, Tenant, User
from beework.models.assistants import Assistant
from beework.models.conversations import Conversation, EndUser
from beework.models.runtime import Run
from beework.security import hash_password

PASSWORD = test_open_api.PASSWORD
SETTINGS = ("portal_name", "input_placeholder", "default_audience", "answer_style", "nav_search", "nav_projects", "nav_app_library",
            "nav_recent", "nav_pinned", "file_upload", "voice_input", "profile_edit", "personal_memory", "task_notifications", "delisted_history")


class PortalChatTests(test_open_api.OpenApiTests):
    def setUp(self) -> None:
        super().setUp()
        with self.session_factory.begin() as db:
            root = db.scalar(select(Organization).where(Organization.tenant_id == self.ids["tenant"]))
            for name in ("emp1", "emp2"):
                account = User(email=f"{name}@example.com", display_name=name, password_hash=hash_password(PASSWORD))
                db.add(account)
                db.flush()
                db.add(Membership(tenant_id=self.ids["tenant"], user_id=account.id, organization_id=root.id, role="employee"))
                self.ids[name] = account.id
        self.admin = self.login("admin")
        created = self.client.post(
            "/api/v1/portal/apps",
            json={"source_type": "agent", "source_id": self.assistant_id, "name": "员工助手", "description": "问答", "category": "员工服务",
                  "audience": "all", "mode": "chat", "icon": "sparkles", "featured": False},
            headers=self.admin,
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.app_id = created.json()["id"]
        self.assertEqual(self.client.post(f"/api/v1/portal/apps/{self.app_id}/list", json={}, headers=self.admin).status_code, 200)

    # -- helpers --------------------------------------------------------------------------------

    def emp(self, name: str = "emp1") -> dict:
        return self.login(name)

    def send(self, headers, message="你好", **extra):
        body = {"app_id": self.app_id, "message": message, "stream": False, **extra}
        return self.client.post("/api/v1/portal/me/chat", json=body, headers=headers)

    def conversations(self, headers, **params) -> dict:
        response = self.client.get("/api/v1/portal/me/conversations", params=params, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def patch(self, headers, conversation_id, **body):
        return self.client.patch(f"/api/v1/portal/me/conversations/{conversation_id}", json=body, headers=headers)

    def settings(self, **changes) -> None:
        admin = self.login("admin")
        current = self.client.get("/api/v1/portal/settings", headers=admin).json()
        body = {**{k: current[k] for k in SETTINGS}, "row_version": current["row_version"], **changes}
        self.assertEqual(self.client.put("/api/v1/portal/settings", json=body, headers=admin).status_code, 200)

    def set_app(self, **values) -> None:
        from beework.models.portal import PortalApp

        with self.session_factory.begin() as db:
            app = db.get(PortalApp, UUID(self.app_id))
            for key, value in values.items():
                setattr(app, key, value)

    # -- the real runtime ---------------------------------------------------------------------------

    def test_a_message_runs_the_live_version_and_is_recorded_as_a_portal_conversation(self) -> None:
        headers = self.emp()
        reply = self.send(headers, "你好")
        self.assertEqual(reply.status_code, 200, reply.text)
        body = reply.json()
        self.assertEqual(body["message"]["content"], "[You are helpful.] echo: 你好")
        with self.session_factory() as db:
            conversation = db.get(Conversation, UUID(body["conversation_id"]))
            run = db.get(Run, UUID(body["run_id"]))
            end_user = db.get(EndUser, conversation.end_user_id)
            self.assertEqual((conversation.source, conversation.environment, run.source), ("portal", "production", "portal"))
            self.assertEqual((end_user.channel_type, end_user.external_id), ("portal", str(self.ids["emp1"])))  # bound to the member
            self.assertEqual((conversation.title, str(conversation.portal_app_id)), ("你好", self.app_id))
            self.assertIsNotNone(run.version_id)  # a frozen version, not the draft

    def test_the_draft_never_answers_employees(self) -> None:
        with self.session_factory.begin() as db:
            assistant = db.get(Assistant, UUID(self.assistant_id))
            assistant.draft_config = {**assistant.draft_config, "prompt": "DRAFT PROMPT"}
        self.assertEqual(self.send(self.emp()).json()["message"]["content"], "[You are helpful.] echo: 你好")

    def test_streaming_relays_start_delta_and_done(self) -> None:
        headers = self.emp()
        response = self.client.post("/api/v1/portal/me/chat", json={"app_id": self.app_id, "message": "hi", "stream": True}, headers=headers)
        self.assertEqual((response.status_code, response.headers["content-type"].split(";")[0]), (200, "text/event-stream"))
        frames = [chunk for chunk in response.text.split("\n\n") if chunk.startswith("event:")]
        names = [frame.split("\n")[0].removeprefix("event: ") for frame in frames]
        self.assertEqual((names[0], names[-1]), ("start", "done"))
        self.assertIn("delta", names)
        done = json.loads(frames[-1].split("data: ", 1)[1])
        self.assertEqual(done["message"]["content"], "[You are helpful.] echo: hi")

    def test_follow_up_messages_join_the_same_conversation(self) -> None:
        headers = self.emp()
        first = self.send(headers, "第一条").json()
        second = self.send(headers, "第二条", conversation_id=first["conversation_id"])
        self.assertEqual((second.status_code, second.json()["conversation_id"]), (200, first["conversation_id"]))
        messages = self.client.get(f"/api/v1/portal/me/conversations/{first['conversation_id']}/messages", headers=headers).json()
        self.assertEqual([(m["role"], m["content"]) for m in messages["messages"]][::2], [("user", "第一条"), ("user", "第二条")])
        self.assertEqual(len(messages["messages"]), 4)
        self.assertEqual(messages["conversation"]["app_name"], "员工助手")

    def test_usage_counts_the_portal_source(self) -> None:
        self.send(self.emp())
        with self.session_factory() as db:
            from beework.usage import month_conversations

            self.assertEqual(month_conversations(db, self.ids["tenant"]), 1)  # portal is billable, unlike debug / test

    # -- privacy -----------------------------------------------------------------------------------

    def test_nobody_else_can_read_change_or_delete_an_employees_conversation(self) -> None:
        conversation = self.send(self.emp("emp1")).json()["conversation_id"]
        other = self.emp("emp2")
        self.assertEqual(self.conversations(other)["items"], [])
        self.assertEqual(self.client.get(f"/api/v1/portal/me/conversations/{conversation}/messages", headers=other).status_code, 404)
        self.assertEqual(self.patch(other, conversation, title="x").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/v1/portal/me/conversations/{conversation}", headers=other).status_code, 404)
        self.assertEqual(self.send(other, "hi", conversation_id=conversation).status_code, 404)

    def test_administrators_cannot_see_portal_conversations(self) -> None:
        conversation = self.send(self.emp()).json()["conversation_id"]
        admin = self.login("admin")
        listed = self.client.get(f"/api/v1/assistants/{self.assistant_id}/conversations", headers=admin).json()
        self.assertEqual(listed["total"], 0)
        self.assertEqual(self.client.get(f"/api/v1/conversations/{conversation}", headers=admin).status_code, 404)
        # as a portal user the admin has their own, separate history
        self.assertEqual(self.conversations(admin)["items"], [])

    def test_another_tenant_cannot_reach_the_portal_here(self) -> None:
        foreign = {**self.login("foreign", self.ids["other"]), "X-Tenant-ID": str(self.ids["tenant"])}
        self.assertEqual(self.client.get("/api/v1/portal/me/conversations", headers=foreign).status_code, 403)
        self.assertEqual(self.send(foreign).status_code, 403)

    # -- managing conversations --------------------------------------------------------------------------

    def test_rename_pin_search_and_delete(self) -> None:
        headers = self.emp()
        first = self.send(headers, "alpha").json()["conversation_id"]
        second = self.send(headers, "beta").json()["conversation_id"]
        self.assertEqual([c["id"] for c in self.conversations(headers)["items"]], [second, first])  # most recent first
        pinned = self.patch(headers, first, pinned=True)
        self.assertEqual((pinned.status_code, pinned.json()["pinned"]), (200, True))
        self.assertEqual([c["id"] for c in self.conversations(headers)["items"]], [first, second])  # pinned first
        self.assertEqual(self.patch(headers, first, title="  周报  ").json()["title"], "周报")
        self.assertEqual([c["title"] for c in self.conversations(headers, q="周")["items"]], ["周报"])
        self.assertEqual(self.patch(headers, first, title="  ").status_code, 422)
        self.assertEqual(self.patch(headers, first, pinned=False).json()["pinned"], False)
        deleted = self.client.delete(f"/api/v1/portal/me/conversations/{first}", headers=headers)
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual([c["id"] for c in self.conversations(headers)["items"]], [second])
        self.assertEqual(self.client.get(f"/api/v1/portal/me/conversations/{first}/messages", headers=headers).status_code, 404)
        self.assertEqual(self.send(headers, "again", conversation_id=first).status_code, 404)
        with self.session_factory() as db:
            self.assertIsNotNone(db.get(Conversation, UUID(first)).deleted_at)  # soft delete: retention still decides the purge
            self.assertTrue(db.scalar(select(AuditEvent).where(AuditEvent.action == "portal_conversation.deleted")))

    # -- projects ------------------------------------------------------------------------------------------

    def project(self, headers, name="销售周报", **extra):
        return self.client.post("/api/v1/portal/me/projects", json={"name": name, "description": "d", **extra}, headers=headers)

    def test_projects_are_private_and_conversations_move_in_and_out(self) -> None:
        headers = self.emp()
        created = self.project(headers)
        self.assertEqual(created.status_code, 201, created.text)
        project = created.json()["id"]
        inside = self.send(headers, "in project", project_id=project).json()["conversation_id"]  # a new conversation inside the project
        outside = self.send(headers, "elsewhere").json()["conversation_id"]
        detail = self.client.get(f"/api/v1/portal/me/projects/{project}", headers=headers).json()
        self.assertEqual((detail["conversation_count"], [c["id"] for c in detail["conversations"]]), (1, [inside]))
        self.assertEqual(self.patch(headers, outside, project_id=project).json()["project_id"], project)  # move in
        self.assertEqual([c["id"] for c in self.conversations(headers, project_id=project)["items"]], [outside, inside])
        self.assertIsNone(self.patch(headers, inside, clear_project=True).json()["project_id"])  # move out
        renamed = self.client.patch(f"/api/v1/portal/me/projects/{project}", json={"name": "新名称", "description": ""}, headers=headers)
        self.assertEqual(renamed.json()["name"], "新名称")
        other = self.emp("emp2")
        self.assertEqual(self.client.get("/api/v1/portal/me/projects", headers=other).json(), [])
        for method, path in (("GET", f"/{project}"), ("PATCH", f"/{project}"), ("DELETE", f"/{project}")):
            response = self.client.request(method, f"/api/v1/portal/me/projects{path}", json={"name": "x"} if method == "PATCH" else None, headers=other)
            self.assertEqual(response.status_code, 404, method)
        self.assertEqual(self.send(other, "x", project_id=project).status_code, 404)
        own_conv = self.send(other, "mine").json()["conversation_id"]
        self.assertEqual(self.patch(other, own_conv, project_id=project).status_code, 404)

    def test_deleting_a_project_keeps_its_conversations(self) -> None:
        headers = self.emp()
        project = self.project(headers).json()["id"]
        conversation = self.send(headers, "keep me", project_id=project).json()["conversation_id"]
        self.assertEqual(self.client.delete(f"/api/v1/portal/me/projects/{project}", headers=headers).status_code, 204)
        kept = self.conversations(headers)["items"]
        self.assertEqual([(c["id"], c["project_id"]) for c in kept], [(conversation, None)])
        self.assertEqual(self.client.get(f"/api/v1/portal/me/projects/{project}", headers=headers).status_code, 404)
        actions = {e.action for e in self.session_factory().scalars(select(AuditEvent))}
        self.assertTrue({"portal_project.created", "portal_project.deleted"} <= actions)

    def test_project_validation(self) -> None:
        headers = self.emp()
        self.assertEqual(self.project(headers, "   ").status_code, 422)
        self.assertEqual(self.project(headers, "x" * 61).status_code, 422)
        self.assertEqual(self.client.post("/api/v1/portal/me/projects", json={"name": "ok"}, headers={k: v for k, v in headers.items() if k != "X-CSRF-Token"}).status_code, 403)

    # -- administrator switches ----------------------------------------------------------------------------------

    def test_switched_off_features_answer_403(self) -> None:
        headers = self.emp()
        conversation = self.send(headers, "hello").json()["conversation_id"]
        project = self.project(headers).json()["id"]
        self.settings(nav_projects=False, nav_pinned=False, nav_search=False)
        headers = self.emp()
        self.assertEqual(self.client.get("/api/v1/portal/me/projects", headers=headers).status_code, 403)
        self.assertEqual(self.project(headers).status_code, 403)
        self.assertEqual(self.client.get(f"/api/v1/portal/me/projects/{project}", headers=headers).status_code, 403)
        self.assertEqual(self.patch(headers, conversation, pinned=True).status_code, 403)
        self.assertEqual(self.patch(headers, conversation, project_id=project).status_code, 403)
        self.assertEqual(self.send(headers, "x", project_id=project).status_code, 403)
        self.assertEqual(self.client.get("/api/v1/portal/me/conversations", params={"q": "h"}, headers=headers).json()["code"], "portal.feature_disabled")
        self.assertEqual(self.patch(headers, conversation, title="still fine").status_code, 200)  # renaming is not switchable

    # -- app availability ------------------------------------------------------------------------------------------

    def test_a_delisted_app_refuses_new_messages_and_history_follows_the_setting(self) -> None:
        headers = self.emp()
        conversation = self.send(headers, "before").json()["conversation_id"]
        self.assertEqual(self.client.post(f"/api/v1/portal/apps/{self.app_id}/delist", json={"reason": "停用"}, headers=self.login("admin")).status_code, 200, "delist")
        headers = self.emp()
        gone = self.send(headers, "after", conversation_id=conversation)
        self.assertEqual((gone.status_code, gone.json()["code"]), (410, "portal.app_unavailable"))
        self.assertEqual(self.send(headers, "new").status_code, 410)
        item = self.conversations(headers)["items"][0]
        self.assertTrue(item["readonly"])  # delisted_history defaults to read-only
        self.assertEqual(self.client.get(f"/api/v1/portal/me/conversations/{conversation}/messages", headers=headers).status_code, 200)
        self.settings(delisted_history="hidden")
        headers = self.emp()
        self.assertEqual(self.conversations(headers)["items"], [])
        self.assertEqual(self.client.get(f"/api/v1/portal/me/conversations/{conversation}/messages", headers=headers).status_code, 404)

    def test_an_unavailable_source_is_a_410_too(self) -> None:
        with self.session_factory.begin() as db:
            db.get(Assistant, UUID(self.assistant_id)).status = "disabled"
        response = self.send(self.emp())
        self.assertEqual((response.status_code, response.json()["code"]), (410, "portal.app_unavailable"))

    def test_apps_out_of_scope_or_not_listed_look_missing(self) -> None:
        self.set_app(status="draft")
        self.assertEqual(self.send(self.emp()).status_code, 404)
        self.set_app(status="listed", audience="admins")
        self.assertEqual(self.send(self.emp()).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/portal/me/apps", headers=self.emp()).json(), [])
        self.set_app(audience="all")
        self.assertEqual(self.send(self.emp(), app_id=str(UUID(int=5))).status_code, 404)

    def test_the_app_picker_lists_visible_apps_without_internals(self) -> None:
        apps = self.client.get("/api/v1/portal/me/apps", headers=self.emp()).json()
        self.assertEqual([(a["name"], a["category"]) for a in apps], [("员工助手", "员工服务")])
        self.assertTrue({"source_type", "source_id", "status"}.isdisjoint(apps[0]))

    def test_conversation_continuation_must_use_the_same_app(self) -> None:
        conversation = self.send(self.emp()).json()["conversation_id"]
        with self.session_factory.begin() as db:
            from beework.models.portal import PortalApp

            other = PortalApp(tenant_id=self.ids["tenant"], source_type="agent", source_id=UUID(self.assistant_id), name="Other", status="listed")
            db.add(other)
            db.flush()
            other_id = str(other.id)
        self.assertEqual(self.send(self.emp(), conversation_id=conversation, app_id=other_id).status_code, 404)

    # -- seats, validation ---------------------------------------------------------------------------------------------

    def test_every_endpoint_needs_a_portal_seat(self) -> None:
        with self.session_factory.begin() as db:
            tenant = db.get(Tenant, self.ids["tenant"])
            entitlements.change_subscription(db, tenant, tenant.plan, portal_mode="named", portal_seats=5, portal_overage="hard")
        headers = self.emp()
        calls = (
            ("GET", "/api/v1/portal/me/apps", None), ("GET", "/api/v1/portal/me/conversations", None),
            ("POST", "/api/v1/portal/me/chat", {"app_id": self.app_id, "message": "hi", "stream": False}),
            ("GET", f"/api/v1/portal/me/conversations/{UUID(int=1)}/messages", None),
            ("PATCH", f"/api/v1/portal/me/conversations/{UUID(int=1)}", {"pinned": True}),
            ("DELETE", f"/api/v1/portal/me/conversations/{UUID(int=1)}", None),
            ("GET", "/api/v1/portal/me/projects", None), ("POST", "/api/v1/portal/me/projects", {"name": "x"}),
        )
        for method, path, body in calls:
            response = self.client.request(method, path, json=body, headers=headers)
            self.assertEqual((response.status_code, response.json().get("code")), (403, "portal.seat_required"), path)

    def test_message_validation(self) -> None:
        headers = self.emp()
        self.assertEqual(self.send(headers, "   ").status_code, 422)
        self.assertEqual(self.send(headers, "").status_code, 422)
        no_csrf = {k: v for k, v in headers.items() if k != "X-CSRF-Token"}
        self.assertEqual(self.send(no_csrf).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.send(headers).status_code, 401)


for _name in [n for n in dir(test_open_api.OpenApiTests) if n.startswith("test_")]:
    setattr(PortalChatTests, _name, None)

if __name__ == "__main__":
    unittest.main()
