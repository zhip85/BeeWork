"""T-145: the employee's own profile, personal memory, notification switch and password."""

import json
import unittest
from unittest import mock
from uuid import UUID

from sqlalchemy import select

import test_portal_chat
from beework import entitlements, portal_profile_service
from beework import memory as mem
from beework.models import AuditEvent, Tenant, User
from beework.models.conversations import EndUser
from beework.models.memory import EndUserMemory
from beework.models.notifications import (
    InAppNotification,  # noqa: F401  (keeps the model registered)
)
from beework.security import verify_password
from test_runtime import ScriptedChat

PASSWORD = test_portal_chat.PASSWORD
NEW_PASSWORD = "an entirely new password 456"


class PortalProfileTests(test_portal_chat.PortalChatTests):
    def profile(self, headers):
        return self.client.get("/api/v1/portal/me/profile", headers=headers)

    def save(self, headers, **changes):
        current = self.profile(headers).json()
        body = {"display_name": current["display_name"], "job_title": current["job_title"], "default_app_id": current["default_app_id"], **changes}
        return self.client.put("/api/v1/portal/me/profile", json=body, headers=headers)

    def audits(self, action: str) -> list[AuditEvent]:
        with self.session_factory() as db:
            return list(db.scalars(select(AuditEvent).where(AuditEvent.action == action)))

    def remember(self, headers, count: int = 2) -> UUID:
        """A real portal conversation, then memory extracted from it by the ordinary extraction service."""
        conversation = self.send(headers, "我喜欢简短的回答").json()["conversation_id"]
        payload = {"user_memory": [{"type": "preference", "text": f"偏好 {i}"} for i in range(count)], "profile": [], "session_summary": "聊了回答风格"}
        with self.session_factory() as db:
            mem.extract_conversation_memory(db, UUID(conversation), llm_factory=lambda *_: ScriptedChat(chunks=[json.dumps(payload, ensure_ascii=False)]))
        return UUID(conversation)

    def stored(self, user: str = "emp1") -> list[EndUserMemory]:
        with self.session_factory() as db:
            end_user = db.scalar(select(EndUser).where(EndUser.channel_type == "portal", EndUser.external_id == str(self.ids[user])))
            return [] if end_user is None else list(db.scalars(select(EndUserMemory).where(EndUserMemory.end_user_id == end_user.id)))

    # -- profile ------------------------------------------------------------------------------------

    def test_the_profile_shows_identity_department_and_the_defaults(self) -> None:
        body = self.profile(self.emp()).json()
        self.assertEqual((body["display_name"], body["email"], body["department"], body["job_title"]), ("emp1", "emp1@example.com", "Acme", ""))
        self.assertEqual((body["can_edit"], body["memory_enabled"], body["notifications_enabled"], body["default_app_id"]), (True, True, True, None))
        self.assertEqual(body["login_method"], "邮箱与密码（本地账号）")

    def test_an_employee_edits_their_own_profile_and_it_is_audited(self) -> None:
        headers = self.emp()
        saved = self.save(headers, display_name="  小明  ", job_title="销售经理", default_app_id=self.app_id)
        self.assertEqual(saved.status_code, 200, saved.text)
        body = saved.json()
        self.assertEqual((body["display_name"], body["job_title"], body["default_app_id"], body["default_app_name"]), ("小明", "销售经理", self.app_id, "员工助手"))
        self.assertEqual(self.profile(self.emp("emp2")).json()["display_name"], "emp2")  # nobody else changed
        self.assertEqual(self.profile(self.emp("emp2")).json()["job_title"], "")
        self.assertTrue(self.audits("portal_profile.updated"))
        self.assertEqual(self.save(self.emp(), display_name="   ").status_code, 422)
        self.assertEqual(self.save(self.emp(), job_title="x" * 61).status_code, 422)

    def test_the_default_app_must_be_visible_and_stops_counting_when_it_is_not(self) -> None:
        admins_only = self.add_app("仅管理员", "管理", audience="admins")
        self.assertEqual(self.save(self.emp(), default_app_id=admins_only).status_code, 404)
        self.assertEqual(self.save(self.emp(), default_app_id=str(UUID(int=4))).status_code, 404)
        self.assertEqual(self.save(self.emp(), default_app_id=self.app_id).status_code, 200)
        self.client.post(f"/api/v1/portal/apps/{self.app_id}/delist", json={"reason": "x"}, headers=self.login("admin"))
        self.assertIsNone(self.profile(self.emp()).json()["default_app_id"])  # delisted: reads as no default

    def test_switching_profile_editing_off_makes_it_read_only(self) -> None:
        self.settings(profile_edit=False)
        headers = self.emp()
        self.assertEqual(self.profile(headers).json()["can_edit"], False)
        denied = self.client.put("/api/v1/portal/me/profile", json={"display_name": "x"}, headers=headers)
        self.assertEqual((denied.status_code, denied.json()["code"]), (403, "portal.feature_disabled"))

    # -- personal memory ----------------------------------------------------------------------------------

    def test_the_employee_sees_only_their_own_memory_and_can_delete_all_of_it(self) -> None:
        headers = self.emp("emp1")
        self.remember(headers)
        self.remember(self.emp("emp2"), 1)
        mine = self.client.get("/api/v1/portal/me/memory", headers=self.emp("emp1")).json()
        self.assertTrue(mine["enabled"])
        self.assertEqual(sorted(m["content"] for m in mine["items"] if m["kind"] == "memory"), ["偏好 0", "偏好 1"])
        self.assertEqual({m["kind"] for m in mine["items"]}, {"memory", "session"})
        theirs = self.client.get("/api/v1/portal/me/memory", headers=self.emp("emp2")).json()
        self.assertEqual(len([m for m in theirs["items"] if m["kind"] == "memory"]), 1)
        deleted = self.client.delete("/api/v1/portal/me/memory", headers=self.emp("emp1"))
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(self.stored("emp1"), [])
        self.assertEqual(len(self.stored("emp2")), 2)  # the other employee's memory is untouched
        self.assertTrue(self.audits("memory.user_deleted"))
        self.assertEqual(self.client.delete("/api/v1/portal/me/memory", headers=self.emp("emp1")).status_code, 204)  # nothing left: still fine

    def injected(self) -> list[int]:
        """How often the runtime asked for memory to inject during one portal message."""
        calls: list[int] = []

        def fake(*args, **kwargs):
            calls.append(1)
            return []

        with mock.patch("beework.runtime.executor.load_memory_context", fake):
            self.assertEqual(self.send(self.emp(), "你好").status_code, 200)
        return calls

    def test_memory_is_injected_until_either_switch_is_off(self) -> None:
        self.assertTrue(self.injected())
        self.assertEqual(self.client.put("/api/v1/portal/me/memory/settings", json={"enabled": False}, headers=self.emp()).json()["enabled"], False)
        self.assertEqual(self.injected(), [])  # the employee's switch stops reading
        self.client.put("/api/v1/portal/me/memory/settings", json={"enabled": True}, headers=self.emp())
        self.assertTrue(self.injected())
        self.settings(personal_memory=False)
        self.assertEqual(self.injected(), [])  # the administrator's switch stops it too
        self.assertTrue(self.audits("portal_profile.memory_switched"))

    def test_nothing_is_written_while_memory_is_off(self) -> None:
        self.client.put("/api/v1/portal/me/memory/settings", json={"enabled": False}, headers=self.emp())
        self.remember(self.emp())
        self.assertEqual(self.stored(), [])
        self.client.put("/api/v1/portal/me/memory/settings", json={"enabled": True}, headers=self.emp())
        self.remember(self.emp())
        self.assertTrue(self.stored())
        self.settings(personal_memory=False)
        before = len(self.stored())
        self.remember(self.emp())
        self.assertEqual(len(self.stored()), before)  # the administrator's switch: the server writes nothing

    def test_switches_apply_only_to_portal_end_users(self) -> None:
        with self.session_factory() as db:
            console_user = EndUser(tenant_id=self.ids["tenant"], channel_type="api", external_id="visitor", name="v")
            db.add(console_user)
            db.commit()
            self.settings(personal_memory=False)
            self.assertTrue(portal_profile_service.memory_allowed_for_end_user(db, self.ids["tenant"], console_user.id))
            self.assertTrue(portal_profile_service.memory_allowed_for_end_user(db, self.ids["tenant"], None))

    def test_memory_endpoints_follow_the_administrators_switch(self) -> None:
        self.settings(personal_memory=False)
        headers = self.emp()
        self.assertEqual(self.profile(headers).json()["memory_available"], False)
        for method, path, body in (("GET", "/memory", None), ("PUT", "/memory/settings", {"enabled": False}), ("DELETE", "/memory", None)):
            response = self.client.request(method, f"/api/v1/portal/me{path}", json=body, headers=headers)
            self.assertEqual((response.status_code, response.json()["code"]), (403, "portal.feature_disabled"), path)

    # -- notifications --------------------------------------------------------------------------------------

    def test_the_task_notification_switch(self) -> None:
        headers = self.emp()
        off = self.client.put("/api/v1/portal/me/notifications", json={"enabled": False}, headers=headers)
        self.assertEqual((off.status_code, off.json()["notifications_enabled"]), (200, False))
        self.assertEqual(self.profile(self.emp("emp2")).json()["notifications_enabled"], True)  # per employee
        self.assertTrue(self.audits("portal_profile.notifications_switched"))
        self.settings(task_notifications=False)
        denied = self.client.put("/api/v1/portal/me/notifications", json={"enabled": True}, headers=self.emp())
        self.assertEqual((denied.status_code, denied.json()["code"]), (403, "portal.feature_disabled"))

    # -- password -------------------------------------------------------------------------------------------

    def change_password(self, headers, current=PASSWORD, new=NEW_PASSWORD):
        return self.client.post("/api/v1/portal/me/password", json={"current_password": current, "new_password": new}, headers=headers)

    def test_changing_the_password_needs_the_current_one_and_ends_other_sessions(self) -> None:
        headers = self.emp()
        wrong = self.change_password(headers, current="not the password")
        self.assertEqual((wrong.status_code, wrong.json()["code"]), (422, "auth.password_incorrect"))
        self.assertEqual(self.change_password(headers, new="short").status_code, 422)
        self.assertEqual(self.change_password(headers, new=PASSWORD).json()["code"], "auth.password_unchanged")
        # a second device signs in, then this session changes the password
        from fastapi.testclient import TestClient

        from beework.main import app

        second = TestClient(app)
        self.assertEqual(second.post("/api/v1/auth/login", json={"email": "emp1@example.com", "password": PASSWORD}).status_code, 200)
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/api/v1/auth/login", json={"email": "emp1@example.com", "password": PASSWORD}).status_code, 200)
        headers = {"X-CSRF-Token": self.client.cookies.get("beework_csrf"), "X-Tenant-ID": str(self.ids["tenant"])}
        self.assertEqual(self.change_password(headers).status_code, 204)
        self.assertEqual(self.client.get("/api/v1/portal/me/profile", headers=headers).status_code, 200)  # this session stays
        self.assertEqual(second.get("/api/v1/auth/me").status_code, 401)  # the other one ended
        with self.session_factory() as db:
            user = db.get(User, self.ids["emp1"])
            self.assertTrue(verify_password(NEW_PASSWORD, user.password_hash))
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/api/v1/auth/login", json={"email": "emp1@example.com", "password": PASSWORD}).status_code, 401)
        self.assertEqual(self.client.post("/api/v1/auth/login", json={"email": "emp1@example.com", "password": NEW_PASSWORD}).status_code, 200)
        self.assertTrue(self.audits("user.password_changed"))

    def test_repeated_wrong_current_passwords_lock_the_account_like_sign_ins(self) -> None:
        headers = self.emp()
        codes = [self.change_password(headers, current="wrong password").status_code for _ in range(8)]
        self.assertIn(423, codes)  # locked: the endpoint cannot be used to guess the password

    # -- seats, isolation ------------------------------------------------------------------------------------

    def test_every_profile_endpoint_needs_a_portal_seat(self) -> None:
        with self.session_factory.begin() as db:
            tenant = db.get(Tenant, self.ids["tenant"])
            entitlements.change_subscription(db, tenant, tenant.plan, portal_mode="named", portal_seats=5, portal_overage="hard")
        headers = self.emp()
        calls = (
            ("GET", "/profile", None), ("PUT", "/profile", {"display_name": "x"}), ("GET", "/memory", None),
            ("PUT", "/memory/settings", {"enabled": False}), ("DELETE", "/memory", None),
            ("PUT", "/notifications", {"enabled": False}), ("POST", "/password", {"current_password": PASSWORD, "new_password": NEW_PASSWORD}),
        )
        for method, path, body in calls:
            response = self.client.request(method, f"/api/v1/portal/me{path}", json=body, headers=headers)
            self.assertEqual((response.status_code, response.json().get("code")), (403, "portal.seat_required"), path)

    def test_another_tenants_member_and_csrf(self) -> None:
        foreign = {**self.login("foreign", self.ids["other"]), "X-Tenant-ID": str(self.ids["tenant"])}
        self.assertEqual(self.client.get("/api/v1/portal/me/profile", headers=foreign).status_code, 403)
        headers = self.emp()
        no_csrf = {k: v for k, v in headers.items() if k != "X-CSRF-Token"}
        self.assertEqual(self.client.put("/api/v1/portal/me/profile", json={"display_name": "x"}, headers=no_csrf).status_code, 403)
        self.assertEqual(self.client.delete("/api/v1/portal/me/memory", headers=no_csrf).status_code, 403)


for _name in [n for n in dir(test_portal_chat.PortalChatTests) if n.startswith("test_")]:
    setattr(PortalProfileTests, _name, None)

if __name__ == "__main__":
    unittest.main()
