"""T-140: AI portal — application management, listing rules and the visibility function."""

import unittest
from uuid import UUID, uuid4

from sqlalchemy import select

import test_assistants
from beework import portal_service
from beework.capabilities import MOUNT_RESOLVERS, MountItem
from beework.models import AuditEvent, Membership, Organization, User
from beework.models.assistants import Assistant, AssistantVersion
from beework.models.portal import PortalApp
from beework.rbac import BUILTIN_PERMISSIONS


class PortalAppTests(test_assistants.AssistantTests):
    def setUp(self) -> None:
        super().setUp()
        with self.session_factory.begin() as db:
            account = User(email="operator@example.com", display_name="Operator", password_hash=test_assistants.hash_password(test_assistants.PASSWORD))
            db.add(account)
            db.flush()
            db.add(Membership(tenant_id=UUID(self.ids["tenant"]), user_id=account.id, organization_id=UUID(self.ids["root"]), role="operator"))
        self.admin = self.login("admin@example.com")

    # -- helpers ------------------------------------------------------------------

    def publish(self, assistant_id: str, config_mounts: dict | None = None) -> None:
        """Give an agent a live version, the state employees need it in (演进 PRD 10.2)."""
        with self.session_factory.begin() as db:
            assistant = db.get(Assistant, UUID(assistant_id))
            config = {**assistant.draft_config, "mounts": config_mounts or assistant.draft_config.get("mounts") or {}}
            version = AssistantVersion(
                tenant_id=assistant.tenant_id, assistant_id=assistant.id, version_seq=1, version="1.0",
                snapshot={"config": config}, change_note="", change_diff=[],
            )
            db.add(version)
            db.flush()
            assistant.live_version_id, assistant.status = version.id, "published"

    def agent(self, name: str = "Helper", published: bool = True) -> str:
        assistant_id = self.create(self.admin, name).json()["id"]
        if published:
            self.publish(assistant_id)
        return assistant_id

    def app_body(self, source_id: str | None, name: str = "员工服务助手", **extra) -> dict:
        return {"source_type": "agent", "source_id": source_id, "name": name, "description": "HR、IT 与制度问答", "category": "员工服务",
                "audience": "all", "mode": "chat", "icon": "sparkles", "featured": False, **extra}

    def add(self, source_id: str | None, name: str = "员工服务助手", headers=None, **extra):
        return self.client.post("/api/v1/portal/apps", json=self.app_body(source_id, name, **extra), headers=headers or self.admin)

    def get(self, app_id: str, headers=None) -> dict:
        response = self.client.get(f"/api/v1/portal/apps/{app_id}", headers=headers or self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def update(self, app_id: str, headers=None, **changes):
        current = self.get(app_id, headers=headers)
        body = {**{k: current[k] for k in ("source_type", "source_id", "name", "description", "category", "audience", "mode", "icon", "featured")},
                "organization_ids": current["organization_ids"], "row_version": current["row_version"], **changes}
        return self.client.put(f"/api/v1/portal/apps/{app_id}", json=body, headers=headers or self.admin)

    def action(self, app_id: str, name: str, headers=None, body=None):
        return self.client.post(f"/api/v1/portal/apps/{app_id}/{name}", json=body, headers=headers or self.admin)

    def audits(self, action: str) -> list[AuditEvent]:
        with self.session_factory() as db:
            return list(db.scalars(select(AuditEvent).where(AuditEvent.action == action)))

    def visible_to(self, email: str) -> list[str]:
        with self.session_factory() as db:
            user = db.scalar(select(User).where(User.email == email))
            membership = db.scalar(select(Membership).where(Membership.user_id == user.id, Membership.tenant_id == UUID(self.ids["tenant"])))
            return [app.name for app in portal_service.visible_apps(db, membership)]

    # -- sources --------------------------------------------------------------------

    def test_only_published_agents_are_offered_as_sources(self) -> None:
        live = self.agent("Live agent")
        self.agent("Draft agent", published=False)
        disabled = self.agent("Disabled agent")
        self.set_status(disabled, "disabled")
        items = self.client.get("/api/v1/portal/app-sources", headers=self.admin).json()["items"]
        self.assertEqual([(i["source_type"], i["source_id"], i["name"], i["in_use"]) for i in items], [("agent", live, "Live agent", False)])
        self.add(live)
        self.assertTrue(self.client.get("/api/v1/portal/app-sources", headers=self.admin).json()["items"][0]["in_use"])
        self.assertEqual(self.client.get("/api/v1/portal/app-sources?q=zzz", headers=self.admin).json()["items"], [])

    # -- create ---------------------------------------------------------------------

    def test_adding_an_app_makes_a_draft_with_the_resolved_source_and_is_audited(self) -> None:
        agent = self.agent("Insurance agent")
        response = self.add(agent, featured=True)
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertEqual((data["status"], data["source_state"], data["source_name"], data["featured"], data["category"]),
                         ("draft", "available", "Insurance agent", True, "员工服务"))
        self.assertEqual(len(self.audits("portal_app.created")), 1)
        self.assertEqual(self.audits("portal_app.created")[0].category, "assistants")

    def test_the_source_must_exist_and_be_available(self) -> None:
        self.assertEqual(self.add(None).json()["code"], "portal.source_required")
        self.assertEqual(self.add(str(uuid4())).status_code, 404)
        draft = self.agent("Draft", published=False)
        unavailable = self.add(draft)
        self.assertEqual((unavailable.status_code, unavailable.json()["code"]), (422, "portal.source_unavailable"))
        mine = self.agent("Mine")
        stranger = self.login("other@example.com", self.ids["other"])
        self.assertEqual(self.add(mine, headers=stranger).status_code, 404)  # another tenant's agent is invisible
        self.admin = self.login("admin@example.com")
        unknown_type = self.client.post("/api/v1/portal/apps", json={**self.app_body(mine), "source_type": "chatflow_connection"}, headers=self.admin)
        self.assertEqual(unknown_type.status_code, 404)  # no resolver for connections until T-075

    def test_fields_are_validated(self) -> None:
        agent = self.agent()
        self.assertEqual(self.add(agent, name="  ").status_code, 400)
        self.assertEqual(self.add(agent, name="x" * 51).status_code, 400)
        self.assertEqual(self.add(agent, description="d" * 201).status_code, 400)
        self.assertEqual(self.add(agent, category="c" * 25).status_code, 400)
        self.assertEqual(self.add(agent, audience="everyone").status_code, 422)
        self.assertEqual(self.add(agent, mode="voice").status_code, 422)
        form = self.add(agent, mode="form")  # an agent answers in chat; form needs a workflow source
        self.assertEqual((form.status_code, form.json()["code"]), (422, "portal.mode_invalid"))
        self.assertEqual(self.add(agent, category="自定义分类").json()["category"], "自定义分类")  # custom values are kept

    def test_names_are_unique_within_the_tenant(self) -> None:
        first, second = self.agent("One"), self.agent("Two")
        self.assertEqual(self.add(first, name="Assistant").status_code, 201)
        self.assertEqual(self.add(second, name="assistant").json()["code"], "portal.name_taken")

    def test_audience_organizations_must_belong_to_the_tenant(self) -> None:
        agent = self.agent()
        ok = self.add(agent, audience="orgs", organization_ids=[self.ids["sales"], self.ids["support"]])
        self.assertEqual(ok.status_code, 201, ok.text)
        self.assertEqual(sorted(ok.json()["organization_names"]), ["Sales", "Support"])
        foreign = self.add(self.agent("Second"), name="Other", audience="orgs", organization_ids=[str(uuid4())])
        self.assertEqual(foreign.status_code, 404)
        flipped = self.update(ok.json()["id"], audience="all", organization_ids=[])
        self.assertEqual((flipped.json()["audience"], flipped.json()["organization_ids"]), ("all", []))

    # -- update ---------------------------------------------------------------------

    def test_update_edits_fields_and_checks_the_row_version(self) -> None:
        agent = self.agent()
        app_id = self.add(agent).json()["id"]
        stale = self.get(app_id)["row_version"]
        data = self.update(app_id, name="新名称", featured=True).json()
        self.assertEqual((data["name"], data["featured"], data["row_version"]), ("新名称", True, stale + 1))
        body = {**self.app_body(agent), "organization_ids": [], "row_version": stale}
        conflict = self.client.put(f"/api/v1/portal/apps/{app_id}", json=body, headers=self.admin)
        self.assertEqual((conflict.status_code, conflict.json()["code"]), (409, "conflict.stale"))
        missing = self.client.put(f"/api/v1/portal/apps/{app_id}", json=self.app_body(agent), headers=self.admin)
        self.assertEqual(missing.status_code, 422)

    def test_a_listed_app_cannot_change_its_source_but_a_draft_can(self) -> None:
        first, second = self.agent("First"), self.agent("Second")
        app_id = self.add(first).json()["id"]
        self.assertEqual(self.update(app_id, source_id=second).json()["source_id"], second)  # draft: repointing is fine
        self.assertEqual(self.action(app_id, "list").status_code, 200)
        locked = self.update(app_id, source_id=first)
        self.assertEqual((locked.status_code, locked.json()["code"]), (409, "portal.source_locked"))
        self.assertEqual(self.update(app_id, name="Renamed").status_code, 200)  # everything else stays editable
        self.assertEqual(self.get(app_id)["source_id"], second)
        self.action(app_id, "delist")
        self.assertEqual(self.update(app_id, source_id=first).status_code, 200)  # delisted: allowed again
        self.assertEqual(self.update(app_id, source_id=self.agent("Draft", published=False)).json()["code"], "portal.source_unavailable")

    # -- listing --------------------------------------------------------------------

    def test_listing_makes_the_app_visible_and_delisting_hides_it_at_once(self) -> None:
        app_id = self.add(self.agent()).json()["id"]
        self.assertEqual(self.visible_to("builder@example.com"), [])  # a draft is invisible
        listed = self.action(app_id, "list").json()
        self.assertEqual((listed["status"], listed["listed_at"] is not None), ("listed", True))
        self.assertEqual(self.visible_to("builder@example.com"), ["员工服务助手"])
        self.assertEqual(self.action(app_id, "list").json()["code"], "portal.already_listed")
        delisted = self.action(app_id, "delist", body={"reason": "  改版  "}).json()
        self.assertEqual((delisted["status"], delisted["delisted_reason"]), ("delisted", "改版"))
        self.assertEqual(self.visible_to("builder@example.com"), [])
        self.assertEqual(self.action(app_id, "delist").json()["code"], "portal.not_listed")
        self.assertEqual(self.action(app_id, "list").status_code, 200)  # back on the shelf
        self.assertEqual(self.get(app_id)["delisted_reason"], None)
        self.assertEqual((len(self.audits("portal_app.listed")), len(self.audits("portal_app.delisted"))), (2, 1))

    def test_each_listing_check_reports_its_reason(self) -> None:
        agent = self.agent()
        app_id = self.add(agent).json()["id"]
        with self.session_factory.begin() as db:  # the agent loses its live version
            db.get(Assistant, UUID(agent)).status = "disabled"
        blocked = self.action(app_id, "list")
        self.assertEqual((blocked.status_code, blocked.json()["code"]), (422, "portal.listing_blocked"))
        self.assertIn("来源未发布", blocked.json()["detail"])
        self.set_status(agent, "published")
        with self.session_factory.begin() as db:  # a legacy row without a source
            app = db.get(PortalApp, UUID(app_id))
            app.source_id = None
        self.assertIn("来源未设置", self.action(app_id, "list").json()["detail"])
        with self.session_factory.begin() as db:
            app = db.get(PortalApp, UUID(app_id))
            app.source_id, app.audience = UUID(agent), "orgs"  # orgs without any organization
        self.assertIn("可见范围", self.action(app_id, "list").json()["detail"])

    def test_a_source_with_a_broken_capability_reference_cannot_be_listed(self) -> None:
        gone = uuid4()
        MOUNT_RESOLVERS["skill"] = lambda db, tenant, ids: {}  # nothing exists: every mounted skill is deleted
        self.addCleanup(lambda: MOUNT_RESOLVERS.pop("skill", None))
        agent = self.create(self.admin, "Broken").json()["id"]
        self.publish(agent, {"skill": [str(gone)]})
        app_id = self.add(agent).json()["id"]
        blocked = self.action(app_id, "list")
        self.assertEqual(blocked.status_code, 422)
        self.assertIn("失效的能力引用", blocked.json()["detail"])
        MOUNT_RESOLVERS["skill"] = lambda db, tenant, ids: {i: MountItem(i, "skill", "Ok", "available") for i in ids}
        self.assertEqual(self.action(app_id, "list").status_code, 200)

    def test_a_source_that_goes_away_hides_the_app_from_employees_but_keeps_the_configuration(self) -> None:
        agent = self.agent()
        app_id = self.add(agent).json()["id"]
        self.action(app_id, "list")
        self.assertEqual(self.visible_to("builder@example.com"), ["员工服务助手"])
        self.set_status(agent, "disabled")
        self.assertEqual(self.visible_to("builder@example.com"), [])
        data = self.get(app_id)
        self.assertEqual((data["status"], data["source_state"], data["name"]), ("listed", "unavailable", "员工服务助手"))

    def test_legacy_apps_without_a_source_load_without_errors(self) -> None:
        with self.session_factory.begin() as db:
            app = PortalApp(tenant_id=UUID(self.ids["tenant"]), source_type="agent", source_id=None, name="Legacy", category="自定义", audience="all", status="listed")
            db.add(app)
            db.flush()
            app_id = str(app.id)
        data = self.get(app_id)
        self.assertEqual((data["source_state"], data["source_name"], data["category"]), ("unset", None, "自定义"))
        self.assertEqual([a["name"] for a in self.client.get("/api/v1/portal/apps", headers=self.admin).json()], ["Legacy"])
        self.assertEqual(self.visible_to("builder@example.com"), [])  # no source, nothing for employees to call

    # -- visibility -----------------------------------------------------------------

    def test_visibility_follows_the_audience(self) -> None:
        agents = [self.agent(f"A{i}") for i in range(4)]
        everyone = self.add(agents[0], name="All staff").json()["id"]
        sales = self.add(agents[1], name="Sales only", audience="orgs", organization_ids=[self.ids["sales"]]).json()["id"]
        whole = self.add(agents[2], name="Whole company tree", audience="orgs", organization_ids=[self.ids["root"]]).json()["id"]
        admins = self.add(agents[3], name="Admins", audience="admins").json()["id"]
        for app_id in (everyone, sales, whole, admins):
            self.assertEqual(self.action(app_id, "list").status_code, 200)
        self.assertEqual(self.visible_to("admin@example.com"), ["Admins", "All staff", "Sales only", "Whole company tree"])
        self.assertEqual(self.visible_to("builder@example.com"), ["All staff", "Whole company tree"])  # root org: not inside Sales
        self.assertEqual(self.visible_to("sales@example.com"), ["All staff", "Sales only", "Whole company tree"])  # Sales sits below root
        self.assertEqual(self.visible_to("viewer@example.com"), ["All staff", "Whole company tree"])

    def test_featured_apps_come_first(self) -> None:
        first, second = self.agent("One"), self.agent("Two")
        a = self.add(first, name="Alpha").json()["id"]
        b = self.add(second, name="Beta", featured=True).json()["id"]
        self.action(a, "list")
        self.action(b, "list")
        self.assertEqual(self.visible_to("builder@example.com"), ["Beta", "Alpha"])

    def test_other_tenants_never_see_an_app(self) -> None:
        app_id = self.add(self.agent()).json()["id"]
        self.action(app_id, "list")
        with self.session_factory() as db:
            outsider = db.scalar(select(Membership).join(User).where(User.email == "other@example.com"))
            self.assertEqual(portal_service.visible_apps(db, outsider), [])

    # -- delete ---------------------------------------------------------------------

    def test_delete_needs_a_delisted_app_and_is_soft(self) -> None:
        app_id = self.add(self.agent()).json()["id"]
        self.action(app_id, "list")
        blocked = self.client.delete(f"/api/v1/portal/apps/{app_id}", headers=self.admin)
        self.assertEqual((blocked.status_code, blocked.json()["code"]), (409, "portal.listed"))
        self.action(app_id, "delist")
        self.assertEqual(self.client.delete(f"/api/v1/portal/apps/{app_id}", headers=self.admin).status_code, 204)
        self.assertEqual(self.client.get(f"/api/v1/portal/apps/{app_id}", headers=self.admin).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/portal/apps", headers=self.admin).json(), [])
        with self.session_factory() as db:
            self.assertIsNotNone(db.get(PortalApp, UUID(app_id)).deleted_at)
        self.assertEqual(len(self.audits("portal_app.deleted")), 1)

    # -- what it means for the source ------------------------------------------------

    def test_an_agent_behind_a_listed_app_cannot_be_deleted_and_shows_the_app_as_a_dependency(self) -> None:
        agent = self.agent("Behind app")
        app_id = self.add(agent).json()["id"]
        deps = self.client.get(f"/api/v1/assistants/{agent}/dependencies", headers=self.admin).json()["items"]
        self.assertEqual([(d["kind"], d["name"], d["status"]) for d in deps], [("portal_app", "员工服务助手", "draft")])
        self.action(app_id, "list")
        self.set_status(agent, "disabled")
        blocked = self.client.delete(f"/api/v1/assistants/{agent}", headers=self.admin)
        self.assertEqual((blocked.status_code, blocked.json()["code"]), (409, "assistant.delete_blocked"))
        self.assertIn("门户应用", blocked.json()["detail"])
        self.action(app_id, "delist")
        self.assertEqual(self.client.delete(f"/api/v1/assistants/{agent}", headers=self.admin).status_code, 204)

    # -- summary, permissions, isolation ------------------------------------------------

    def test_summary_counts_per_status_and_never_invents_usage_numbers(self) -> None:
        first, second, third = self.agent("A"), self.agent("B"), self.agent("C")
        a = self.add(first, name="A app").json()["id"]
        b = self.add(second, name="B app").json()["id"]
        self.add(third, name="C app")
        self.action(a, "list")
        self.action(b, "list")
        self.action(b, "delist")
        summary = self.client.get("/api/v1/portal/summary", headers=self.admin).json()
        self.assertEqual(summary, {"listed": 1, "draft": 1, "delisted": 1, "monthly_active_employees": None, "monthly_calls": None})

    def test_builtin_roles_get_the_portal_permissions_of_the_matrix(self) -> None:
        for role, actions in {"tenant_admin": {"view", "edit", "publish"}, "builder": {"view", "edit"}, "operator": {"view"},
                              "viewer": {"view"}, "auditor": {"view"}}.items():
            self.assertEqual({p.split(":")[1] for p in BUILTIN_PERMISSIONS[role] if p.startswith("portal:")}, actions, role)

    def test_roles_without_permission_get_403(self) -> None:
        agent = self.agent()
        app_id = self.add(agent).json()["id"]
        row_version = self.get(app_id)["row_version"]
        body = {**self.app_body(agent), "organization_ids": [], "row_version": row_version}
        for email in ("viewer@example.com", "operator@example.com"):
            headers = self.login(email)
            self.assertEqual(self.client.get("/api/v1/portal/apps", headers=headers).status_code, 200)
            self.assertEqual(self.client.get("/api/v1/portal/summary", headers=headers).status_code, 200)
            self.assertEqual(self.client.get("/api/v1/portal/app-sources", headers=headers).status_code, 403)
            self.assertEqual(self.add(agent, name="Nope", headers=headers).status_code, 403)
            self.assertEqual(self.client.put(f"/api/v1/portal/apps/{app_id}", json=body, headers=headers).status_code, 403)
            self.assertEqual(self.action(app_id, "list", headers=headers).status_code, 403)
            self.assertEqual(self.action(app_id, "delist", headers=headers).status_code, 403)
            self.assertEqual(self.client.delete(f"/api/v1/portal/apps/{app_id}", headers=headers).status_code, 403)
        self.assertEqual(self.get(app_id, headers=self.login("viewer@example.com"))["can_edit"], False)

    def test_a_builder_can_edit_but_not_list(self) -> None:
        agent = self.agent()
        app_id = self.add(agent).json()["id"]
        builder = self.login("builder@example.com")
        self.assertEqual(self.update(app_id, headers=builder, name="Edited by builder").status_code, 200)
        self.assertEqual(self.action(app_id, "list", headers=builder).status_code, 403)
        self.assertEqual(self.action(app_id, "list", headers=self.login("admin@example.com")).status_code, 200)

    def test_other_tenants_cannot_see_or_change_an_app(self) -> None:
        agent = self.agent()
        app_id = self.add(agent).json()["id"]
        row_version = self.get(app_id)["row_version"]
        stranger = self.login("other@example.com", self.ids["other"])
        self.assertEqual(self.client.get("/api/v1/portal/apps", headers=stranger).json(), [])
        self.assertEqual(self.client.get("/api/v1/portal/summary", headers=stranger).json()["draft"], 0)
        body = {**self.app_body(agent), "organization_ids": [], "row_version": row_version}
        for method, path, payload in [
            ("get", f"/api/v1/portal/apps/{app_id}", None), ("put", f"/api/v1/portal/apps/{app_id}", body),
            ("post", f"/api/v1/portal/apps/{app_id}/list", None), ("post", f"/api/v1/portal/apps/{app_id}/delist", None),
            ("delete", f"/api/v1/portal/apps/{app_id}", None),
        ]:
            response = getattr(self.client, method)(path, headers=stranger, **({"json": payload} if payload is not None else {}))
            self.assertEqual(response.status_code, 404, f"{method} {path}")

    def test_writes_require_the_csrf_token(self) -> None:
        agent = self.agent()
        headers = {"X-Tenant-ID": self.ids["tenant"]}
        self.assertEqual(self.client.post("/api/v1/portal/apps", json=self.app_body(agent), headers=headers).status_code, 403)

    def test_the_organization_tree_used_for_visibility_is_the_tenants(self) -> None:
        with self.session_factory() as db:
            names = {o.name for o in db.scalars(select(Organization).where(Organization.tenant_id == UUID(self.ids["tenant"])))}
        self.assertEqual(names, {"Acme", "Sales", "Support"})


for _name in [n for n in dir(test_assistants.AssistantTests) if n.startswith("test_")]:
    setattr(PortalAppTests, _name, None)  # inherited fixtures only; the assistant tests run in their own module


if __name__ == "__main__":
    unittest.main()
