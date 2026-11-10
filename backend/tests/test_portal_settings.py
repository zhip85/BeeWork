"""T-141: AI portal settings — storage, the employee-side config, and server-side enforcement of the switches."""

import unittest
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select

import test_assistants
from beework import portal_service
from beework.main import app as fastapi_app
from beework.models import AuditEvent
from beework.models.portal import PortalSettings

# Employee-side endpoints do not exist yet (T-143 / T-145 / T-148); these probe routes use the very dependency they
# will use, so what is tested here is the enforcement itself.
_probe = APIRouter()
for _feature in portal_service.FEATURES:
    def _make(feature: str):
        def endpoint(_: object = Depends(portal_service.require_portal_feature(feature))) -> dict:
            return {"ok": feature}
        return endpoint
    _probe.add_api_route(f"/api/v1/_probe/portal/{_feature}", _make(_feature), methods=["GET"])
fastapi_app.include_router(_probe)

DEFAULTS = {
    "portal_name": "", "input_placeholder": "", "default_audience": "all", "answer_style": "balanced",
    "nav_search": True, "nav_projects": True, "nav_app_library": True, "nav_recent": True, "nav_pinned": True,
    "file_upload": True, "voice_input": False, "profile_edit": True, "personal_memory": True, "task_notifications": True,
    "delisted_history": "readonly",
}


class PortalSettingsTests(test_assistants.AssistantTests):
    def setUp(self) -> None:
        super().setUp()
        self.admin = self.login("admin@example.com")

    def settings(self, headers=None) -> dict:
        response = self.client.get("/api/v1/portal/settings", headers=headers or self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def save(self, headers=None, **changes):
        current = self.settings(headers)
        body = {**{k: current[k] for k in DEFAULTS}, "row_version": current["row_version"], **changes}
        return self.client.put("/api/v1/portal/settings", json=body, headers=headers or self.admin)

    def probe(self, feature: str, headers=None):
        return self.client.get(f"/api/v1/_probe/portal/{feature}", headers=headers or self.admin)

    def audits(self) -> list[AuditEvent]:
        with self.session_factory() as db:
            return list(db.scalars(select(AuditEvent).where(AuditEvent.action == "portal_settings.updated")))

    # -- storage --------------------------------------------------------------------

    def test_a_tenant_without_settings_gets_the_defaults_and_reading_writes_nothing(self) -> None:
        data = self.settings()
        self.assertEqual({k: data[k] for k in DEFAULTS}, DEFAULTS)
        self.assertEqual((data["row_version"], data["updated_at"], data["can_edit"]), (0, None, True))
        with self.session_factory() as db:
            self.assertEqual(db.scalars(select(PortalSettings)).all(), [])

    def test_saving_stores_everything_and_reads_back_the_same(self) -> None:
        saved = self.save(portal_name="  Acme AI  ", input_placeholder="问点什么", default_audience="admins", answer_style="detailed",
                          nav_projects=False, nav_pinned=False, voice_input=True, personal_memory=False, delisted_history="hidden")
        self.assertEqual(saved.status_code, 200, saved.text)
        data = self.settings()
        self.assertEqual((data["portal_name"], data["input_placeholder"], data["default_audience"], data["answer_style"], data["delisted_history"]),
                         ("Acme AI", "问点什么", "admins", "detailed", "hidden"))
        self.assertEqual((data["nav_projects"], data["nav_pinned"], data["voice_input"], data["personal_memory"], data["nav_search"]), (False, False, True, False, True))
        self.assertEqual((data["row_version"], data["updated_by"]), (1, "Admin"))
        self.assertEqual(self.save(nav_search=False).json()["row_version"], 2)

    def test_values_are_validated(self) -> None:
        for bad in ({"answer_style": "poetic"}, {"default_audience": "everyone"}, {"delisted_history": "deleted"},
                    {"portal_name": "x" * 51}, {"input_placeholder": "y" * 101}, {"nav_search": "maybe"}):
            self.assertEqual(self.save(**bad).status_code, 422, bad)
        self.assertEqual(self.save(portal_name="x" * 50).status_code, 200)

    def test_a_stale_or_missing_row_version_is_refused(self) -> None:
        self.save(nav_search=False)
        stale = self.client.put("/api/v1/portal/settings", json={**DEFAULTS, "row_version": 0}, headers=self.admin)
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "conflict.stale"))
        self.assertIn("Admin", stale.json()["detail"])
        self.assertEqual(self.client.put("/api/v1/portal/settings", json=DEFAULTS, headers=self.admin).status_code, 422)
        self.assertEqual(self.settings()["nav_search"], False)  # the refused saves changed nothing

    def test_a_first_save_with_a_wrong_version_is_a_conflict_too(self) -> None:
        response = self.client.put("/api/v1/portal/settings", json={**DEFAULTS, "row_version": 5}, headers=self.admin)
        self.assertEqual(response.status_code, 409)

    def test_the_audit_names_what_changed_and_never_stores_free_text(self) -> None:
        self.save(nav_projects=False, answer_style="concise", portal_name="Secret brand name", input_placeholder="hello")
        (event,) = self.audits()
        details = event.details
        self.assertEqual(details["nav_projects"], "True→False")
        self.assertEqual(details["answer_style"], "balanced→concise")
        self.assertIn("portal_name", details["changed"])
        self.assertNotIn("Secret brand name", str(details))
        self.assertNotIn("hello", str(details))
        self.assertEqual(event.category, "assistants")
        self.save()  # nothing changed: still recorded, with nothing named
        self.assertEqual(self.audits()[-1].details["changed"], "none")

    # -- the employee side ---------------------------------------------------------------

    def test_the_employee_config_has_the_brand_and_switches_but_nothing_administrative(self) -> None:
        cfg = self.client.get("/api/v1/portal/me/config", headers=self.admin).json()
        self.assertEqual(cfg["portal_name"], "Acme AI")  # “{tenant} AI” until the administrator names it
        self.assertNotIn("default_audience", cfg)
        self.assertNotIn("row_version", cfg)
        self.assertEqual((cfg["nav_projects"], cfg["voice_input"], cfg["delisted_history"], cfg["has_console"]), (True, False, "readonly", True))
        self.save(portal_name="Acme 智能门户", nav_app_library=False)
        cfg = self.client.get("/api/v1/portal/me/config", headers=self.admin).json()
        self.assertEqual((cfg["portal_name"], cfg["nav_app_library"]), ("Acme 智能门户", False))

    def test_any_member_reads_the_employee_config_and_anonymous_callers_cannot(self) -> None:
        cfg = self.client.get("/api/v1/portal/me/config", headers=self.login("viewer@example.com")).json()
        self.assertEqual(cfg["has_console"], True)  # members without console permissions (the employee role, T-142) get False
        stranger = self.login("other@example.com", self.ids["other"])
        self.assertEqual(self.client.get("/api/v1/portal/me/config", headers=stranger).json()["portal_name"], "Other AI")
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/v1/portal/me/config", headers={"X-Tenant-ID": self.ids["tenant"]}).status_code, 401)

    # -- enforcement -----------------------------------------------------------------------

    def test_every_switch_closes_its_endpoint_on_the_server(self) -> None:
        field_of = portal_service.FEATURES
        self.assertEqual(self.probe("projects").status_code, 200)
        for feature, field in field_of.items():
            current = self.settings()
            self.assertEqual(self.probe(feature).status_code, 200 if current[field] else 403, feature)
            self.save(**{field: not current[field]})
            flipped = self.probe(feature)
            self.assertEqual(flipped.status_code, 403 if current[field] else 200, feature)
            if current[field]:
                self.assertEqual(flipped.json()["code"], "portal.feature_disabled")
            self.save(**{field: current[field]})  # restore
            self.assertEqual(self.probe(feature).status_code, 200 if current[field] else 403, feature)

    def test_enforcement_is_per_tenant(self) -> None:
        self.save(nav_projects=False)
        stranger = self.login("other@example.com", self.ids["other"])
        self.assertEqual(self.probe("projects", stranger).status_code, 200)

    def test_the_helper_for_non_endpoint_code_agrees_with_the_settings(self) -> None:
        tenant = UUID(self.ids["tenant"])
        with self.session_factory() as db:
            self.assertTrue(portal_service.feature_enabled(db, tenant, "personal_memory"))
        self.save(personal_memory=False)
        with self.session_factory() as db:
            self.assertFalse(portal_service.feature_enabled(db, tenant, "personal_memory"))

    # -- permissions and isolation --------------------------------------------------------------

    def test_roles_without_edit_permission_get_403_but_can_read(self) -> None:
        current = self.settings()
        body = {**{k: current[k] for k in DEFAULTS}, "row_version": 0}
        for email in ("viewer@example.com", "builder@example.com"):
            headers = self.login(email)
            self.assertEqual(self.client.get("/api/v1/portal/settings", headers=headers).status_code, 200)
            put = self.client.put("/api/v1/portal/settings", json=body, headers=headers)
            self.assertEqual(put.status_code, 200 if email.startswith("builder") else 403, email)
        self.assertEqual(self.settings(self.login("viewer@example.com"))["can_edit"], False)

    def test_settings_are_isolated_per_tenant(self) -> None:
        self.save(nav_projects=False, portal_name="Mine")
        stranger = self.login("other@example.com", self.ids["other"])
        self.assertEqual({k: self.settings(stranger)[k] for k in DEFAULTS}, DEFAULTS)

    def test_writes_require_the_csrf_token(self) -> None:
        response = self.client.put("/api/v1/portal/settings", json={**DEFAULTS, "row_version": 0}, headers={"X-Tenant-ID": self.ids["tenant"]})
        self.assertEqual(response.status_code, 403)


for _name in [n for n in dir(test_assistants.AssistantTests) if n.startswith("test_")]:
    setattr(PortalSettingsTests, _name, None)  # inherited fixtures only; the assistant tests run in their own module


if __name__ == "__main__":
    unittest.main()
