"""T-060: flow studio — Chatflows and Workflows: CRUD, draft vs. published versions, checks, references and cycles."""

import unittest
from uuid import UUID

from sqlalchemy import select

import test_assistants
from beework.models import AuditEvent, Membership, User
from beework.models.agents import Agent, FlowVersion
from beework.models.assistants import Assistant
from beework.security import hash_password


class FlowTests(test_assistants.AssistantTests):
    def setUp(self) -> None:
        super().setUp()
        with self.session_factory.begin() as db:
            account = User(email="operator@example.com", display_name="Operator", password_hash=hash_password(test_assistants.PASSWORD))
            db.add(account)
            db.flush()
            db.add(Membership(tenant_id=UUID(self.ids["tenant"]), user_id=account.id, organization_id=UUID(self.ids["root"]), role="operator"))
        self.admin = self.login("admin@example.com")

    # -- helpers ------------------------------------------------------------------

    def make(self, kind: str = "chatflow", name: str = "Policy", headers=None, **extra):
        return self.client.post("/api/v1/agents", json={"kind": kind, "name": name, **extra}, headers=headers or self.admin)

    def get(self, flow_id: str, headers=None) -> dict:
        response = self.client.get(f"/api/v1/agents/{flow_id}", headers=headers or self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def save(self, flow_id: str, config: dict | None = None, headers=None, **fields):
        current = self.get(flow_id, headers=headers)
        body = {"row_version": current["row_version"], **fields}
        if config is not None:
            body["config"] = {**current["config"], **config}
        return self.client.put(f"/api/v1/agents/{flow_id}", json=body, headers=headers or self.admin)

    def action(self, flow_id: str, name: str, headers=None, body=None):
        return self.client.post(f"/api/v1/agents/{flow_id}/{name}", json=body, headers=headers or self.admin)

    def complete(self, flow_id: str, headers=None) -> None:
        """Fill in what the checks ask for: opening / fallback for a chatflow, outputs and a tool name for a workflow."""
        current = self.get(flow_id, headers=headers)
        if current["kind"] == "chatflow":
            settings = current["config"]["chat_settings"]
            config = {"chat_settings": {**settings, "opening": "Hi", "fallback": "Sorry"}}
        else:
            triggers = current["config"]["triggers"]
            config = {"outputs": [{"name": "result"}], "triggers": {**triggers, "tool": {**triggers["tool"], "name": "run_it"}}}
        self.assertEqual(self.save(flow_id, config, headers=headers).status_code, 200)

    def ready(self, kind: str = "chatflow", name: str = "Ready") -> str:
        flow_id = self.make(kind, name).json()["id"]
        self.complete(flow_id)
        self.assertEqual(self.action(flow_id, "publish").status_code, 200)
        return flow_id

    def audits(self, action: str) -> list[AuditEvent]:
        with self.session_factory() as db:
            return list(db.scalars(select(AuditEvent).where(AuditEvent.action == action)))

    def issues(self, flow_id: str) -> list[str]:
        return [i["message"] for i in self.client.get(f"/api/v1/agents/{flow_id}/checks", headers=self.admin).json()["issues"]]

    def node(self, target_id: str, kind: str, node_type: str = "agent") -> dict:
        return {"id": "n9", "type": node_type, "name": "Call", "target_id": target_id, "target_kind": kind}

    # -- create -------------------------------------------------------------------

    def test_chatflow_and_workflow_get_the_preset_nodes_and_defaults(self) -> None:
        chatflow = self.make("chatflow", "Policy", description="  Answers  ").json()
        self.assertEqual((chatflow["status"], chatflow["icon"], chatflow["description"]), ("draft", "chat", "Answers"))
        self.assertEqual([(n["type"], n["name"]) for n in chatflow["config"]["nodes"]], [("classify", "意图识别"), ("agent", "答复智能体"), ("note", "注释")])
        self.assertEqual((chatflow["node_count"], chatflow["config"]["chat_settings"]["history_turns"]), (3, 10))
        self.assertIsNone(chatflow["published_config"])
        self.assertEqual(len(self.audits("flow.created")), 1)
        workflow = self.make("workflow", "Refund").json()
        self.assertEqual([n["type"] for n in workflow["config"]["nodes"]], ["extract", "agent", "review"])
        self.assertEqual(workflow["icon"], "flow")
        self.assertTrue(workflow["config"]["triggers"]["api"]["enabled"])

    def test_only_chatflows_and_workflows_exist(self) -> None:
        self.assertEqual(self.make("agent", "Nope").status_code, 422)  # “智能体” is the assistants module (T-067)

    def test_create_validates_names_descriptions_and_icons(self) -> None:
        self.assertEqual(self.make(name="   ").status_code, 400)
        self.assertEqual(self.make(name="x" * 51).status_code, 400)
        self.assertEqual(self.make(description="d" * 201).status_code, 400)
        self.assertEqual(self.make(icon="rocket").status_code, 422)
        self.assertEqual(self.make(name="x" * 50).status_code, 201)

    def test_a_name_is_unique_per_type_but_may_repeat_across_types(self) -> None:
        self.assertEqual(self.make(name="Refund").status_code, 201)
        duplicate = self.make(name="refund")
        self.assertEqual((duplicate.status_code, duplicate.json()["code"]), (409, "agent.name_taken"))
        self.assertEqual(self.make("workflow", "Refund").status_code, 201)

    def test_a_deleted_name_can_be_reused(self) -> None:
        flow_id = self.make(name="Once").json()["id"]
        self.assertEqual(self.client.delete(f"/api/v1/agents/{flow_id}", headers=self.admin).status_code, 204)
        self.assertEqual(self.make(name="Once").status_code, 201)

    # -- list ---------------------------------------------------------------------

    def test_list_filters_by_kind_and_search_and_counts_per_kind(self) -> None:
        self.make(name="Alpha")
        self.make(name="Beta")
        self.make("workflow", "Alpha flow")
        listing = self.client.get("/api/v1/agents?kind=chatflow", headers=self.admin).json()
        self.assertEqual((listing["total"], sorted(i["name"] for i in listing["items"])), (2, ["Alpha", "Beta"]))
        found = self.client.get("/api/v1/agents?q=ALPHA", headers=self.admin).json()
        self.assertEqual(sorted(i["name"] for i in found["items"]), ["Alpha", "Alpha flow"])
        self.assertEqual(self.client.get("/api/v1/agents/summary", headers=self.admin).json(), {"chatflow": 2, "workflow": 1})
        self.assertEqual(self.client.get("/api/v1/agents?limit=101", headers=self.admin).status_code, 422)
        self.assertEqual(self.client.get("/api/v1/agents?offset=50", headers=self.admin).json()["items"], [])

    # -- saving drafts ------------------------------------------------------------

    def test_save_draft_updates_fields_and_bumps_the_row_version(self) -> None:
        created = self.make().json()
        data = self.save(created["id"], {"nodes": []}, name="Policy 2", icon="team").json()
        self.assertEqual((data["name"], data["icon"], data["row_version"], data["status"]), ("Policy 2", "team", created["row_version"] + 1, "draft"))
        self.assertEqual(data["config"]["nodes"], [])
        self.assertEqual(len(self.audits("flow.draft_saved")), 1)

    def test_a_stale_row_version_is_a_conflict_and_a_missing_one_is_rejected(self) -> None:
        flow_id = self.make().json()["id"]
        stale = self.get(flow_id)["row_version"]
        self.assertEqual(self.save(flow_id, {"nodes": []}).status_code, 200)
        response = self.client.put(f"/api/v1/agents/{flow_id}", json={"name": "X", "row_version": stale}, headers=self.admin)
        self.assertEqual((response.status_code, response.json()["code"]), (409, "conflict.stale"))
        self.assertEqual(self.client.put(f"/api/v1/agents/{flow_id}", json={"name": "X"}, headers=self.admin).status_code, 422)

    def test_a_renamed_flow_cannot_take_a_used_name(self) -> None:
        self.make(name="Taken")
        flow_id = self.make(name="Free").json()["id"]
        self.assertEqual(self.save(flow_id, name="taken").status_code, 409)
        self.assertEqual(self.save(flow_id, name="Free").status_code, 200)

    def test_invalid_config_shapes_are_rejected(self) -> None:
        flow_id = self.make().json()["id"]
        self.assertEqual(self.save(flow_id, {"dsl_version": 2}).status_code, 422)
        self.assertEqual(self.save(flow_id, {"nodes": [{"id": "n1", "type": "telepathy"}]}).status_code, 422)
        self.assertEqual(self.save(flow_id, {"nodes": [{"id": "n1", "type": "loop", "max_iterations": 101}]}).status_code, 422)

    def test_a_corrupted_config_is_flagged_and_never_overwritten(self) -> None:
        flow_id = self.make().json()["id"]
        with self.session_factory.begin() as db:
            db.get(Agent, UUID(flow_id)).draft_config = {"dsl_version": 99, "garbage": True}
        data = self.get(flow_id)
        self.assertTrue(data["config_corrupted"])
        self.assertIsNone(data["config"])
        self.assertTrue(self.client.get("/api/v1/agents", headers=self.admin).json()["items"][0]["config_corrupted"])
        response = self.client.put(f"/api/v1/agents/{flow_id}", json={"config": {"nodes": []}, "row_version": data["row_version"]}, headers=self.admin)
        self.assertEqual((response.status_code, response.json()["code"]), (409, "agent.config_corrupted"))
        self.assertEqual(self.client.get(f"/api/v1/agents/{flow_id}/checks", headers=self.admin).status_code, 409)
        self.assertEqual(self.action(flow_id, "publish").status_code, 409)
        self.assertEqual(self.action(flow_id, "duplicate").status_code, 409)
        with self.session_factory() as db:
            self.assertEqual(db.get(Agent, UUID(flow_id)).draft_config, {"dsl_version": 99, "garbage": True})
        renamed = self.client.put(f"/api/v1/agents/{flow_id}", json={"name": "Renamed", "row_version": data["row_version"]}, headers=self.admin)
        self.assertEqual(renamed.status_code, 200)

    # -- checks -------------------------------------------------------------------

    def test_chatflow_rules(self) -> None:
        flow_id = self.make("chatflow", "Flow").json()["id"]
        self.assertEqual(self.issues(flow_id), ["请填写开场白", "请填写兜底回复"])
        settings = self.get(flow_id)["config"]["chat_settings"]
        self.save(flow_id, {"chat_settings": {**settings, "opening": "Hi", "fallback": "Sorry", "history_turns": 51}})
        self.assertEqual(self.issues(flow_id), ["历史轮数需在 1–50 之间"])
        self.save(flow_id, {"chat_settings": {**settings, "opening": "Hi", "fallback": "Sorry", "history_turns": 50}})
        self.assertEqual(self.issues(flow_id), [])
        self.save(flow_id, {"nodes": [{"id": "n1", "type": "note", "name": "just a note"}]})
        self.assertEqual(self.issues(flow_id), ["请添加执行节点"])

    def test_workflow_rules(self) -> None:
        flow_id = self.make("workflow", "Refund").json()["id"]
        self.assertEqual(self.issues(flow_id), ["请至少定义一个输出字段", "工具名需为小写下划线格式"])
        triggers = self.get(flow_id)["config"]["triggers"]

        def apply(**change) -> list[str]:
            config = {"outputs": [{"name": "result"}], "inputs": [{"name": "order_id"}],
                      "triggers": {**triggers, "tool": {**triggers["tool"], "name": "refund_tool"}}, **change}
            self.save(flow_id, config)
            return self.issues(flow_id)

        self.assertEqual(apply(), [])
        self.assertEqual(apply(inputs=[{"name": "Order"}]), ["请修正输入输出字段名"])
        self.assertEqual(apply(inputs=[{"name": "a"}, {"name": "a"}]), ["请修正输入输出字段名"])
        self.assertEqual(apply(inputs=[{"name": f"f{i}"} for i in range(40)]), ["字段过多"])
        off = {"api": {"enabled": False}, "schedule": {"enabled": False}, "tool": {"enabled": False, "name": ""}}
        self.assertEqual(apply(triggers=off), ["请至少开启一种触发方式"])
        bad_cron = {**off, "schedule": {"enabled": True, "cron": "* * *", "timezone": "UTC"}}
        self.assertEqual(apply(triggers=bad_cron), ["定时触发需填写 5 段 cron 表达式"])
        self.assertEqual(apply(triggers={**off, "tool": {"enabled": True, "name": "Bad-Name"}}), ["工具名需为小写下划线格式"])

    # -- versions -----------------------------------------------------------------

    def test_publish_freezes_an_immutable_version_and_later_drafts_leave_it_alone(self) -> None:
        flow_id = self.make().json()["id"]
        self.complete(flow_id)
        first = self.action(flow_id, "publish", body={"change_note": "  first  "}).json()
        self.assertEqual((first["status"], first["live_version"], first["versions_count"]), ("published", "v1", 1))
        self.assertEqual(first["published_config"]["chat_settings"]["opening"], "Hi")
        self.assertFalse(first["has_draft_changes"])
        settings = first["config"]["chat_settings"]
        changed = self.save(flow_id, {"chat_settings": {**settings, "opening": "Hello again"}}).json()
        self.assertEqual(changed["status"], "published")  # callers keep using v1 (RUL-002)
        self.assertTrue(changed["has_draft_changes"])
        self.assertEqual((changed["config"]["chat_settings"]["opening"], changed["published_config"]["chat_settings"]["opening"]), ("Hello again", "Hi"))
        second = self.action(flow_id, "publish", body={"change_note": "second"}).json()
        self.assertEqual((second["live_version"], second["versions_count"], second["published_config"]["chat_settings"]["opening"]), ("v2", 2, "Hello again"))
        versions = self.client.get(f"/api/v1/agents/{flow_id}/versions", headers=self.admin).json()
        self.assertEqual([(v["version"], v["change_note"], v["is_live"]) for v in versions], [("v2", "second", True), ("v1", "first", False)])
        self.assertEqual(len(self.audits("flow.published")), 2)

    def test_published_versions_cannot_be_modified(self) -> None:
        flow_id = self.ready()
        with self.session_factory() as db:
            version = db.scalar(select(FlowVersion).where(FlowVersion.flow_id == UUID(flow_id)))
            version.change_note = "tampered"
            with self.assertRaises(ValueError):
                db.flush()
            db.rollback()

    def test_a_flow_failing_the_checks_cannot_be_published(self) -> None:
        flow_id = self.make().json()["id"]
        response = self.action(flow_id, "publish")
        self.assertEqual((response.status_code, response.json()["code"]), (422, "agent.checks_failed"))
        self.assertIn("请填写开场白", response.json()["detail"])
        data = self.get(flow_id)
        self.assertEqual((data["status"], data["versions_count"]), ("draft", 0))

    def test_duplicate_makes_a_new_draft_with_a_free_name(self) -> None:
        flow_id = self.ready(name="Original")
        copy = self.action(flow_id, "duplicate")
        self.assertEqual(copy.status_code, 201, copy.text)
        data = copy.json()
        self.assertEqual((data["name"], data["status"], data["versions_count"], data["live_version"]), ("Original 副本", "draft", 0, None))
        self.assertEqual(data["config"], self.get(flow_id)["config"])
        self.assertEqual(self.action(flow_id, "duplicate").json()["name"], "Original 副本 2")
        self.assertEqual(len(self.audits("flow.duplicated")), 2)

    # -- node targets and references ----------------------------------------------

    def test_a_node_may_call_a_published_assistant_or_a_published_flow(self) -> None:
        assistant = self.create(self.admin, "Helper bot").json()["id"]
        flow = self.make("workflow", "Caller").json()["id"]
        self.complete(flow)
        self.save(flow, {"nodes": [self.node(assistant, "assistant")]})
        self.assertEqual(self.issues(flow), ["请处理失效的资源引用"])  # the assistant is still a draft
        self.assertEqual(self.get(flow)["invalid_nodes"], ["n9"])
        self.set_status(assistant, "published")
        self.assertEqual(self.issues(flow), [])
        self.assertEqual(self.get(flow)["invalid_nodes"], [])
        self.set_status(assistant, "disabled")
        self.assertEqual(self.action(flow, "publish").status_code, 422)
        target = self.ready("workflow", "Target")
        self.save(flow, {"nodes": [self.node(target, "flow", "copilot")]})
        self.assertEqual(self.issues(flow), [])
        self.client.delete(f"/api/v1/agents/{target}", headers=self.admin)
        self.assertEqual(self.get(flow)["invalid_nodes"], ["n9"])
        self.assertEqual(self.action(flow, "publish").status_code, 422)

    def test_connections_and_unknown_targets_stay_invalid_until_they_exist(self) -> None:
        flow = self.make("workflow", "Caller").json()["id"]
        self.complete(flow)
        self.save(flow, {"nodes": [self.node(str(UUID(int=3)), "connection", "connector")]})
        self.assertEqual(self.issues(flow), ["请处理失效的资源引用"])
        self.save(flow, {"nodes": [self.node(str(UUID(int=4)), "assistant")]})  # no such assistant
        self.assertEqual(self.issues(flow), ["请处理失效的资源引用"])

    def test_references_list_flows_and_registered_providers(self) -> None:
        from beework import agent_service

        target = self.ready("workflow", "Shared")
        caller = self.make("workflow", "Caller").json()["id"]
        self.save(caller, {"nodes": [self.node(target, "flow")]})
        provider = lambda db, flow: [("connection", UUID(int=9), "Conn")]  # noqa: E731 — what T-075 registers
        agent_service.REFERENCE_PROVIDERS.append(provider)
        self.addCleanup(agent_service.REFERENCE_PROVIDERS.remove, provider)
        references = self.client.get(f"/api/v1/agents/{target}/references", headers=self.admin).json()
        self.assertEqual(sorted((r["kind"], r["name"]) for r in references["items"]), [("connection", "Conn"), ("workflow", "Caller")])
        self.assertEqual(self.get(target)["reference_count"], 2)

    def test_a_flow_cannot_call_itself_and_loops_are_rejected(self) -> None:
        a, b = self.make("workflow", "A").json()["id"], self.make("workflow", "B").json()["id"]
        c = self.make("chatflow", "C").json()["id"]
        selfie = self.save(a, {"nodes": [self.node(a, "flow")]})
        self.assertEqual((selfie.status_code, selfie.json()["code"]), (422, "agent.self_reference"))
        self.assertEqual(self.save(a, {"nodes": [self.node(b, "flow")]}).status_code, 200)
        self.assertEqual(self.save(b, {"nodes": [self.node(c, "flow")]}).status_code, 200)
        loop = self.save(c, {"nodes": [self.node(a, "flow")]})
        self.assertEqual((loop.status_code, loop.json()["code"]), (422, "agent.cycle"))
        self.assertEqual(self.save(c, {"nodes": []}).status_code, 200)
        # calling an assistant that happens to share a flow's id is not a flow reference
        self.assertEqual(self.save(c, {"nodes": [self.node(a, "assistant")]}).status_code, 200)

    # -- disable / enable / delete ------------------------------------------------

    def test_disable_and_enable_follow_the_state_machine(self) -> None:
        flow_id = self.make().json()["id"]
        self.assertEqual(self.action(flow_id, "disable").status_code, 409)
        self.assertEqual(self.action(flow_id, "enable").status_code, 409)
        self.complete(flow_id)
        self.action(flow_id, "publish")
        self.assertEqual(self.action(flow_id, "disable").json()["status"], "disabled")
        self.assertEqual(self.action(flow_id, "disable").status_code, 409)
        self.assertEqual(self.action(flow_id, "publish").status_code, 409)
        self.assertEqual(self.save(flow_id, {"nodes": []}).json()["status"], "disabled")
        enabled = self.action(flow_id, "enable").json()
        self.assertEqual((enabled["status"], enabled["live_version"]), ("published", "v1"))
        self.assertEqual((len(self.audits("flow.disabled")), len(self.audits("flow.enabled"))), (1, 1))

    def test_delete_is_soft_audited_and_hides_the_flow(self) -> None:
        flow_id = self.make().json()["id"]
        self.assertEqual(self.client.delete(f"/api/v1/agents/{flow_id}", headers=self.admin).status_code, 204)
        self.assertEqual(self.client.get(f"/api/v1/agents/{flow_id}", headers=self.admin).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/agents", headers=self.admin).json()["total"], 0)
        self.assertEqual(self.client.delete(f"/api/v1/agents/{flow_id}", headers=self.admin).status_code, 404)
        with self.session_factory() as db:
            self.assertIsNotNone(db.get(Agent, UUID(flow_id)).deleted_at)
        self.assertEqual(len(self.audits("flow.deleted")), 1)

    def test_flows_are_not_mountable_directly(self) -> None:
        from beework.capabilities import MOUNT_RESOLVERS

        self.assertEqual(sorted(MOUNT_RESOLVERS), ["kb", "mcp"])  # flows are reached through flow connections (T-075)
        row = self.create(self.admin, "Mounting").json()["id"]
        with self.session_factory.begin() as db:
            assistant = db.get(Assistant, UUID(row))
            assistant.draft_config = {**assistant.draft_config, "mounts": {"chatflow": [str(UUID(int=1))]}}
        mounts = self.client.get(f"/api/v1/assistants/{row}/mounts", headers=self.admin).json()
        self.assertEqual([i["state"] for g in mounts["groups"] for i in g["items"]], ["deleted"])

    # -- permissions and isolation -------------------------------------------------

    def test_roles_without_write_permission_get_403(self) -> None:
        flow_id = self.ready()
        row_version = self.get(flow_id)["row_version"]
        readers = [self.login("viewer@example.com"), self.login("operator@example.com")]
        for headers in readers:
            self.assertEqual(self.client.get("/api/v1/agents", headers=headers).status_code, 200)
            self.assertEqual(self.client.get(f"/api/v1/agents/{flow_id}", headers=headers).status_code, 200)
            self.assertEqual(self.client.get(f"/api/v1/agents/{flow_id}/versions", headers=headers).status_code, 200)
            self.assertEqual(self.make(name="Nope", headers=headers).status_code, 403)
            put = self.client.put(f"/api/v1/agents/{flow_id}", json={"name": "N", "row_version": row_version}, headers=headers)
            self.assertEqual(put.status_code, 403)
            for name in ("publish", "disable", "enable", "duplicate"):
                self.assertEqual(self.action(flow_id, name, headers=headers).status_code, 403, name)
            self.assertEqual(self.client.delete(f"/api/v1/agents/{flow_id}", headers=headers).status_code, 403)
        data = self.get(flow_id, headers=readers[1])
        self.assertEqual((data["can_edit"], data["can_publish"]), (False, False))
        self.assertTrue(self.get(flow_id, headers=self.login("admin@example.com"))["can_edit"])

    def test_a_builder_may_manage_flows(self) -> None:
        headers = self.login("builder@example.com")
        flow_id = self.make(headers=headers).json()["id"]
        self.complete(flow_id, headers=headers)
        self.assertEqual(self.action(flow_id, "publish", headers=headers).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/v1/agents/{flow_id}", headers=headers).status_code, 204)

    def test_other_tenants_cannot_see_or_change_a_flow(self) -> None:
        flow_id = self.ready()
        row_version = self.get(flow_id)["row_version"]
        stranger = self.login("other@example.com", self.ids["other"])
        self.assertEqual(self.client.get("/api/v1/agents", headers=stranger).json()["total"], 0)
        for method, path, body in [
            ("get", f"/api/v1/agents/{flow_id}", None),
            ("get", f"/api/v1/agents/{flow_id}/checks", None),
            ("get", f"/api/v1/agents/{flow_id}/references", None),
            ("get", f"/api/v1/agents/{flow_id}/versions", None),
            ("put", f"/api/v1/agents/{flow_id}", {"name": "Hijack", "row_version": row_version}),
            ("post", f"/api/v1/agents/{flow_id}/publish", None),
            ("post", f"/api/v1/agents/{flow_id}/duplicate", None),
            ("post", f"/api/v1/agents/{flow_id}/disable", None),
            ("post", f"/api/v1/agents/{flow_id}/enable", None),
            ("delete", f"/api/v1/agents/{flow_id}", None),
        ]:
            response = getattr(self.client, method)(path, headers=stranger, **({"json": body} if body is not None else {}))
            self.assertEqual(response.status_code, 404, f"{method} {path}")
        self.assertEqual(self.get(flow_id, headers=self.login("admin@example.com"))["name"], "Ready")

    def test_names_are_unique_per_tenant_only(self) -> None:
        self.make(name="Shared name")
        stranger = self.login("other@example.com", self.ids["other"])
        self.assertEqual(self.make(name="Shared name", headers=stranger).status_code, 201)

    def test_writes_require_the_csrf_token(self) -> None:
        headers = {"X-Tenant-ID": self.ids["tenant"]}
        self.assertEqual(self.client.post("/api/v1/agents", json={"kind": "chatflow", "name": "NoCsrf"}, headers=headers).status_code, 403)

    def test_flow_actions_are_audited_in_the_agents_category(self) -> None:
        self.make()
        self.assertEqual(self.audits("flow.created")[0].category, "assistants")


for _name in [n for n in dir(test_assistants.AssistantTests) if n.startswith("test_")]:
    setattr(FlowTests, _name, None)  # inherited fixtures only; the assistant tests run in their own module


if __name__ == "__main__":
    unittest.main()
