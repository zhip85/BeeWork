"""T-060: agents, chatflows and workflows — CRUD, draft / available configs, checks, references and cycles."""

import unittest
from uuid import UUID

from sqlalchemy import select

import test_assistants
from beework.models import AuditEvent, Membership, User
from beework.models.agents import Agent
from beework.models.assistants import Assistant
from beework.security import hash_password


class AgentTests(test_assistants.AssistantTests):
    def setUp(self) -> None:
        super().setUp()
        with self.session_factory.begin() as db:
            account = User(email="operator@example.com", display_name="Operator", password_hash=hash_password(test_assistants.PASSWORD))
            db.add(account)
            db.flush()
            root = self.ids["root"]
            db.add(Membership(tenant_id=UUID(self.ids["tenant"]), user_id=account.id, organization_id=UUID(root), role="operator"))
        self.admin = self.login("admin@example.com")

    # -- helpers ------------------------------------------------------------------

    def make(self, kind: str = "agent", name: str = "Support", headers=None, **extra):
        return self.client.post(
            "/api/v1/agents", json={"kind": kind, "name": name, **extra}, headers=headers or self.admin
        )

    def get(self, agent_id: str, headers=None) -> dict:
        response = self.client.get(f"/api/v1/agents/{agent_id}", headers=headers or self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def save(self, agent_id: str, config: dict | None = None, headers=None, **fields):
        current = self.get(agent_id)
        body = {"row_version": current["row_version"], **fields}
        if config is not None:
            body["config"] = {**current["config"], **config}
        return self.client.put(f"/api/v1/agents/{agent_id}", json=body, headers=headers or self.admin)

    def action(self, agent_id: str, name: str, headers=None, body=None):
        return self.client.post(f"/api/v1/agents/{agent_id}/{name}", json=body, headers=headers or self.admin)

    def ready_agent(self, name: str = "Ready") -> str:
        agent_id = self.make(name=name).json()["id"]
        self.assertEqual(self.save(agent_id, {"instructions": "Help politely."}).status_code, 200)
        self.assertEqual(self.action(agent_id, "make-available").status_code, 200)
        return agent_id

    def audits(self, action: str) -> list[AuditEvent]:
        with self.session_factory() as db:
            return list(db.scalars(select(AuditEvent).where(AuditEvent.action == action)))

    # -- create -------------------------------------------------------------------

    def test_create_an_agent_uses_the_default_model_and_is_audited(self) -> None:
        response = self.make(description="  Handles tickets  ")
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertEqual((data["status"], data["kind"], data["icon"], data["description"]), ("draft", "agent", "brain", "Handles tickets"))
        self.assertEqual(data["config"]["model_id"], self.ids["chat"])
        self.assertEqual(data["model_name"], "Chat One")
        self.assertEqual(data["config"]["dsl_version"], 1)
        self.assertIsNone(data["available_config"])
        self.assertEqual(len(self.audits("agent.created")), 1)

    def test_chatflow_and_workflow_get_the_preset_nodes(self) -> None:
        chatflow = self.make("chatflow", "Policy").json()
        self.assertEqual([(n["type"], n["name"]) for n in chatflow["config"]["nodes"]], [("classify", "意图识别"), ("agent", "答复 Agent"), ("note", "注释")])
        self.assertEqual(chatflow["icon"], "chat")
        self.assertEqual(chatflow["node_count"], 3)
        self.assertEqual(chatflow["config"]["chat_settings"]["history_turns"], 10)
        workflow = self.make("workflow", "Refund").json()
        self.assertEqual([n["type"] for n in workflow["config"]["nodes"]], ["extract", "agent", "review"])
        self.assertEqual(workflow["icon"], "flow")
        self.assertTrue(workflow["config"]["triggers"]["api"]["enabled"])

    def test_a_team_is_an_agent_with_team_settings(self) -> None:
        team = self.make(name="Crew", is_team=True).json()
        self.assertTrue(team["is_team"])
        self.assertEqual((team["icon"], team["config"]["team"]["mode"]), ("team", "router"))
        self.assertEqual(self.make("workflow", "Crew flow", is_team=True).status_code, 422)

    def test_create_validates_names_and_descriptions(self) -> None:
        self.assertEqual(self.make(name="   ").status_code, 400)
        self.assertEqual(self.make(name="x" * 51).status_code, 400)
        self.assertEqual(self.make(description="d" * 201).status_code, 400)
        self.assertEqual(self.make(kind="robot").status_code, 422)
        self.assertEqual(self.make(icon="rocket").status_code, 422)
        self.assertEqual(self.make(name="x" * 50).status_code, 201)

    def test_a_name_is_unique_per_type_but_may_repeat_across_types(self) -> None:
        self.assertEqual(self.make(name="Refund").status_code, 201)
        duplicate = self.make(name="refund")
        self.assertEqual((duplicate.status_code, duplicate.json()["code"]), (409, "agent.name_taken"))
        self.assertEqual(self.make("workflow", "Refund").status_code, 201)
        self.assertEqual(self.make("chatflow", "Refund").status_code, 201)

    def test_a_deleted_name_can_be_reused(self) -> None:
        agent_id = self.make(name="Once").json()["id"]
        self.assertEqual(self.client.delete(f"/api/v1/agents/{agent_id}", headers=self.admin).status_code, 204)
        self.assertEqual(self.make(name="Once").status_code, 201)

    # -- list ---------------------------------------------------------------------

    def test_list_filters_by_kind_and_search_and_counts_per_kind(self) -> None:
        self.make(name="Alpha")
        self.make(name="Beta")
        self.make("workflow", "Alpha flow")
        listing = self.client.get("/api/v1/agents?kind=agent", headers=self.admin).json()
        self.assertEqual((listing["total"], sorted(i["name"] for i in listing["items"])), (2, ["Alpha", "Beta"]))
        found = self.client.get("/api/v1/agents?q=ALPHA", headers=self.admin).json()
        self.assertEqual(sorted(i["name"] for i in found["items"]), ["Alpha", "Alpha flow"])
        counts = self.client.get("/api/v1/agents/summary", headers=self.admin).json()
        self.assertEqual(counts, {"agent": 2, "chatflow": 0, "workflow": 1})
        self.assertEqual(self.client.get("/api/v1/agents?limit=101", headers=self.admin).status_code, 422)
        self.assertEqual(self.client.get("/api/v1/agents?offset=50", headers=self.admin).json()["items"], [])

    # -- saving drafts ------------------------------------------------------------

    def test_save_draft_updates_fields_and_bumps_the_row_version(self) -> None:
        created = self.make().json()
        response = self.save(created["id"], {"instructions": "Be kind", "purpose": "Tickets"}, name="Support 2", icon="team")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual((data["name"], data["icon"], data["row_version"]), ("Support 2", "team", created["row_version"] + 1))
        self.assertEqual((data["config"]["instructions"], data["status"]), ("Be kind", "draft"))
        self.assertEqual(len(self.audits("agent.draft_saved")), 1)

    def test_a_stale_row_version_is_a_conflict_and_a_missing_one_is_rejected(self) -> None:
        agent_id = self.make().json()["id"]
        stale = self.get(agent_id)["row_version"]
        self.assertEqual(self.save(agent_id, {"purpose": "a"}).status_code, 200)
        response = self.client.put(f"/api/v1/agents/{agent_id}", json={"name": "X", "row_version": stale}, headers=self.admin)
        self.assertEqual((response.status_code, response.json()["code"]), (409, "conflict.stale"))
        self.assertEqual(self.client.put(f"/api/v1/agents/{agent_id}", json={"name": "X"}, headers=self.admin).status_code, 422)

    def test_a_renamed_agent_cannot_take_a_used_name(self) -> None:
        self.make(name="Taken")
        agent_id = self.make(name="Free").json()["id"]
        self.assertEqual(self.save(agent_id, name="taken").status_code, 409)
        self.assertEqual(self.save(agent_id, name="Free").status_code, 200)  # its own name is fine

    def test_invalid_config_shapes_are_rejected(self) -> None:
        agent_id = self.make().json()["id"]
        self.assertEqual(self.save(agent_id, {"instructions": "x" * 8001}).status_code, 422)
        self.assertEqual(self.save(agent_id, {"memory_enabled": "maybe"}).status_code, 422)
        self.assertEqual(self.save(agent_id, {"dsl_version": 2}).status_code, 422)

    def test_the_model_must_be_enabled_for_the_tenant(self) -> None:
        agent_id = self.make().json()["id"]
        response = self.save(agent_id, {"model_id": self.ids["embedding"]})
        self.assertEqual((response.status_code, response.json()["code"]), (422, "agent.model_unavailable"))
        self.assertEqual(self.save(agent_id, {"model_id": self.ids["chat"]}).status_code, 200)

    def test_a_corrupted_config_is_flagged_and_never_overwritten(self) -> None:
        agent_id = self.make().json()["id"]
        with self.session_factory.begin() as db:
            db.get(Agent, UUID(agent_id)).draft_config = {"dsl_version": 99, "garbage": True}
        data = self.get(agent_id)
        self.assertTrue(data["config_corrupted"])
        self.assertIsNone(data["config"])
        listed = self.client.get("/api/v1/agents", headers=self.admin).json()["items"][0]
        self.assertTrue(listed["config_corrupted"])
        response = self.client.put(
            f"/api/v1/agents/{agent_id}", json={"config": {"instructions": "fresh"}, "row_version": data["row_version"]}, headers=self.admin
        )
        self.assertEqual((response.status_code, response.json()["code"]), (409, "agent.config_corrupted"))
        self.assertEqual(self.client.get(f"/api/v1/agents/{agent_id}/checks", headers=self.admin).status_code, 409)
        self.assertEqual(self.action(agent_id, "make-available").status_code, 409)
        with self.session_factory() as db:
            self.assertEqual(db.get(Agent, UUID(agent_id)).draft_config, {"dsl_version": 99, "garbage": True})
        # a rename does not touch the stored configuration
        self.assertEqual(self.client.put(f"/api/v1/agents/{agent_id}", json={"name": "Renamed", "row_version": data["row_version"]}, headers=self.admin).status_code, 200)

    # -- checks -------------------------------------------------------------------

    def issues(self, agent_id: str) -> list[str]:
        return [i["message"] for i in self.client.get(f"/api/v1/agents/{agent_id}/checks", headers=self.admin).json()["issues"]]

    def test_an_agent_needs_instructions(self) -> None:
        agent_id = self.make().json()["id"]
        self.assertEqual(self.issues(agent_id), ["请填写指令"])
        self.save(agent_id, {"instructions": "Go"})
        self.assertEqual(self.issues(agent_id), [])

    def test_chatflow_rules(self) -> None:
        agent_id = self.make("chatflow", "Flow").json()["id"]
        self.assertEqual(self.issues(agent_id), ["请填写开场白", "请填写兜底回复"])
        settings = self.get(agent_id)["config"]["chat_settings"]
        self.save(agent_id, {"chat_settings": {**settings, "opening": "Hi", "fallback": "Sorry", "history_turns": 51}})
        self.assertEqual(self.issues(agent_id), ["历史轮数需在 1–50 之间"])
        self.save(agent_id, {"chat_settings": {**settings, "opening": "Hi", "fallback": "Sorry", "history_turns": 50}})
        self.assertEqual(self.issues(agent_id), [])
        self.save(agent_id, {"nodes": [{"id": "n1", "type": "note", "name": "just a note"}]})
        self.assertEqual(self.issues(agent_id), ["请添加执行节点"])

    def test_workflow_rules(self) -> None:
        agent_id = self.make("workflow", "Refund").json()["id"]
        self.assertEqual(self.issues(agent_id), ["请至少定义一个输出字段", "工具名需为小写下划线格式"])
        triggers = self.get(agent_id)["config"]["triggers"]

        def apply(**change) -> list[str]:
            config = {
                "outputs": [{"name": "result"}], "inputs": [{"name": "order_id"}],
                "triggers": {**triggers, "tool": {**triggers["tool"], "name": "refund_tool"}}, **change,
            }
            self.save(agent_id, config)
            return self.issues(agent_id)

        self.assertEqual(apply(), [])
        self.assertEqual(apply(inputs=[{"name": "Order"}]), ["请修正输入输出字段名"])
        self.assertEqual(apply(inputs=[{"name": "a"}, {"name": "a"}]), ["请修正输入输出字段名"])
        self.assertEqual(apply(inputs=[{"name": f"f{i}"} for i in range(40)]), ["字段过多"])
        off = {"api": {"enabled": False}, "schedule": {"enabled": False}, "tool": {"enabled": False, "name": ""}}
        self.assertEqual(apply(triggers=off), ["请至少开启一种触发方式"])
        bad_cron = {**off, "schedule": {"enabled": True, "cron": "* * *", "timezone": "UTC"}}
        self.assertEqual(apply(triggers=bad_cron), ["定时触发需填写 5 段 cron 表达式"])
        self.assertEqual(apply(triggers={**off, "tool": {"enabled": True, "name": "Bad-Name"}}), ["工具名需为小写下划线格式"])

    def test_a_team_needs_two_members_and_a_lead_among_them(self) -> None:
        one, two = self.ready_agent("One"), self.ready_agent("Two")
        team = self.make(name="Crew", is_team=True).json()["id"]
        self.save(team, {"instructions": "Coordinate", "resources": {"agents": [one]}})
        self.assertEqual(self.issues(team), ["协作组至少连接 2 个智能体"])
        self.save(team, {"resources": {"agents": [one, two]}, "team": {"mode": "parallel", "lead_agent_id": str(UUID(int=7))}})
        self.assertEqual(self.issues(team), ["主控智能体需在已连接的智能体中"])
        self.save(team, {"team": {"mode": "parallel", "lead_agent_id": one}})
        self.assertEqual(self.issues(team), [])

    # -- draft versus available ----------------------------------------------------

    def test_make_available_copies_the_draft_and_later_drafts_leave_it_alone(self) -> None:
        agent_id = self.ready_agent()
        first = self.get(agent_id)
        self.assertEqual((first["status"], first["available_config"]["instructions"]), ("available", "Help politely."))
        self.assertIsNotNone(first["available_at"])
        self.assertFalse(first["has_draft_changes"])
        changed = self.save(agent_id, {"instructions": "A new draft"}).json()
        self.assertEqual(changed["status"], "available")  # callers keep using the available version (RUL-002)
        self.assertTrue(changed["has_draft_changes"])
        self.assertEqual(changed["config"]["instructions"], "A new draft")
        self.assertEqual(changed["available_config"]["instructions"], "Help politely.")
        self.assertEqual(self.action(agent_id, "make-available").status_code, 200)
        self.assertEqual(self.get(agent_id)["available_config"]["instructions"], "A new draft")
        self.assertEqual(len(self.audits("agent.made_available")), 2)

    def test_an_agent_failing_the_checks_cannot_be_made_available(self) -> None:
        agent_id = self.make().json()["id"]
        response = self.action(agent_id, "make-available")
        self.assertEqual((response.status_code, response.json()["code"]), (422, "agent.checks_failed"))
        self.assertIn("请填写指令", response.json()["detail"])
        self.assertEqual(self.get(agent_id)["status"], "draft")

    def test_an_invalid_resource_reference_blocks_making_available(self) -> None:
        agent_id = self.make().json()["id"]
        self.save(agent_id, {"instructions": "Go", "resources": {"tools": [str(UUID(int=5))]}})
        self.assertEqual(self.issues(agent_id), ["请处理失效的资源引用"])
        self.assertEqual(self.action(agent_id, "make-available").status_code, 422)

    def test_connected_agents_must_exist_and_be_available(self) -> None:
        draft_agent = self.make(name="Draft only").json()["id"]
        user = self.make(name="User").json()["id"]
        self.save(user, {"instructions": "Go", "resources": {"agents": [draft_agent]}})
        self.assertEqual(self.issues(user), ["请处理失效的资源引用"])
        ready = self.ready_agent("Ready one")
        self.save(user, {"resources": {"agents": [ready]}})
        self.assertEqual(self.issues(user), [])
        self.client.delete(f"/api/v1/agents/{ready}", headers=self.admin)
        self.assertEqual(self.issues(user), ["请处理失效的资源引用"])

    def test_a_flow_node_target_must_be_available(self) -> None:
        flow = self.make("workflow", "Flow").json()["id"]
        target = self.make(name="Target").json()["id"]
        node = {"id": "n9", "type": "agent", "name": "Call", "target_id": target}
        self.save(flow, {"nodes": [node], "outputs": [{"name": "r"}], "triggers": {"tool": {"enabled": False}}})
        self.assertEqual(self.issues(flow), ["请处理失效的资源引用"])

    # -- disable / enable / delete ------------------------------------------------

    def test_disable_and_enable_follow_the_state_machine(self) -> None:
        agent_id = self.make().json()["id"]
        self.assertEqual(self.action(agent_id, "disable").status_code, 409)  # draft cannot be disabled
        self.assertEqual(self.action(agent_id, "enable").status_code, 409)
        self.save(agent_id, {"instructions": "Go"})
        self.action(agent_id, "make-available")
        self.assertEqual(self.action(agent_id, "disable").json()["status"], "disabled")
        self.assertEqual(self.action(agent_id, "disable").status_code, 409)
        self.assertEqual(self.action(agent_id, "make-available").status_code, 409)
        self.assertEqual(self.save(agent_id, {"purpose": "still editable"}).json()["status"], "disabled")
        self.assertEqual(self.action(agent_id, "enable").json()["status"], "available")
        self.assertEqual((len(self.audits("agent.disabled")), len(self.audits("agent.enabled"))), (1, 1))

    def test_delete_is_soft_audited_and_hides_the_agent(self) -> None:
        agent_id = self.make().json()["id"]
        self.assertEqual(self.client.delete(f"/api/v1/agents/{agent_id}", headers=self.admin).status_code, 204)
        self.assertEqual(self.client.get(f"/api/v1/agents/{agent_id}", headers=self.admin).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/agents", headers=self.admin).json()["total"], 0)
        self.assertEqual(self.client.delete(f"/api/v1/agents/{agent_id}", headers=self.admin).status_code, 404)
        with self.session_factory() as db:
            self.assertIsNotNone(db.get(Agent, UUID(agent_id)).deleted_at)
        self.assertEqual(len(self.audits("agent.deleted")), 1)

    # -- references and cycles -----------------------------------------------------

    def test_references_list_assistants_and_agents_and_flag_the_mount_state(self) -> None:
        agent_id = self.ready_agent("Shared")
        other = self.make(name="Other").json()["id"]
        self.save(other, {"resources": {"agents": [agent_id]}})
        assistant = self.create(self.admin, "Mounting").json()["id"]
        with self.session_factory.begin() as db:
            row = db.get(Assistant, UUID(assistant))
            row.draft_config = {**row.draft_config, "mounts": {"agent": [agent_id]}}
        references = self.client.get(f"/api/v1/agents/{agent_id}/references", headers=self.admin).json()
        self.assertEqual(sorted((r["kind"], r["name"]) for r in references["items"]), [("agent", "Other"), ("assistant", "Mounting")])
        self.assertEqual(self.get(agent_id)["reference_count"], 2)
        mounts = self.client.get(f"/api/v1/assistants/{assistant}/mounts", headers=self.admin).json()
        self.assertEqual([(i["name"], i["state"]) for g in mounts["groups"] for i in g["items"]], [("Shared", "available")])
        self.action(agent_id, "disable")
        mounts = self.client.get(f"/api/v1/assistants/{assistant}/mounts", headers=self.admin).json()
        self.assertEqual([i["state"] for g in mounts["groups"] for i in g["items"]], ["disabled"])
        self.client.delete(f"/api/v1/agents/{agent_id}", headers=self.admin)
        mounts = self.client.get(f"/api/v1/assistants/{assistant}/mounts", headers=self.admin).json()
        self.assertEqual([i["state"] for g in mounts["groups"] for i in g["items"]], ["deleted"])

    def test_an_agent_cannot_connect_to_itself(self) -> None:
        agent_id = self.make().json()["id"]
        response = self.save(agent_id, {"resources": {"agents": [agent_id]}})
        self.assertEqual((response.status_code, response.json()["code"]), (422, "agent.self_reference"))
        flow = self.make("workflow", "Flow").json()["id"]
        node = {"id": "n9", "type": "copilot", "name": "Self", "target_id": flow}
        self.assertEqual(self.save(flow, {"nodes": [node]}).json()["code"], "agent.self_reference")

    def test_a_loop_between_agents_and_workflows_is_rejected(self) -> None:
        a, b, c = (self.make(name=n).json()["id"] for n in ("A", "B", "C"))
        flow = self.make("workflow", "W").json()["id"]
        self.assertEqual(self.save(a, {"resources": {"agents": [b]}}).status_code, 200)
        self.assertEqual(self.save(b, {"resources": {"agents": [c, flow]}}).status_code, 200)
        node = {"id": "n9", "type": "agent", "name": "Back", "target_id": a}
        loop = self.save(flow, {"nodes": [node]})
        self.assertEqual((loop.status_code, loop.json()["code"]), (422, "agent.cycle"))
        loop = self.save(c, {"resources": {"agents": [a]}})
        self.assertEqual((loop.status_code, loop.json()["code"]), (422, "agent.cycle"))
        self.assertEqual(self.save(c, {"resources": {"agents": []}}).status_code, 200)  # unrelated saves keep working
        self.assertEqual(self.get(a)["config"]["resources"]["agents"], [b])

    # -- permissions and isolation -------------------------------------------------

    def test_roles_without_write_permission_get_403(self) -> None:
        agent_id = self.make().json()["id"]
        row_version = self.get(agent_id)["row_version"]
        readers = [self.login("viewer@example.com"), self.login("operator@example.com")]
        for headers in readers:
            self.assertEqual(self.client.get("/api/v1/agents", headers=headers).status_code, 200)
            self.assertEqual(self.client.get(f"/api/v1/agents/{agent_id}", headers=headers).status_code, 200)
            self.assertEqual(self.make(name="Nope", headers=headers).status_code, 403)
            put = self.client.put(f"/api/v1/agents/{agent_id}", json={"name": "N", "row_version": row_version}, headers=headers)
            self.assertEqual(put.status_code, 403)
            for name in ("make-available", "disable", "enable"):
                self.assertEqual(self.action(agent_id, name, headers=headers).status_code, 403, name)
            self.assertEqual(self.client.delete(f"/api/v1/agents/{agent_id}", headers=headers).status_code, 403)
        data = self.get(agent_id, headers=readers[1])
        self.assertEqual((data["can_edit"], data["can_publish"]), (False, False))
        self.assertTrue(self.get(agent_id, headers=self.login("admin@example.com"))["can_edit"])

    def test_a_builder_may_manage_agents(self) -> None:
        headers = self.login("builder@example.com")
        agent_id = self.make(headers=headers).json()["id"]
        self.assertEqual(self.save(agent_id, {"instructions": "Go"}, headers=headers).status_code, 200)
        self.assertEqual(self.action(agent_id, "make-available", headers=headers).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/v1/agents/{agent_id}", headers=headers).status_code, 204)

    def test_other_tenants_cannot_see_or_change_an_agent(self) -> None:
        agent_id = self.make().json()["id"]
        row_version = self.get(agent_id)["row_version"]
        stranger = self.login("other@example.com", self.ids["other"])
        self.assertEqual(self.client.get("/api/v1/agents", headers=stranger).json()["total"], 0)
        for method, path, body in [
            ("get", f"/api/v1/agents/{agent_id}", None),
            ("get", f"/api/v1/agents/{agent_id}/checks", None),
            ("get", f"/api/v1/agents/{agent_id}/references", None),
            ("put", f"/api/v1/agents/{agent_id}", {"name": "Hijack", "row_version": row_version}),
            ("post", f"/api/v1/agents/{agent_id}/make-available", None),
            ("post", f"/api/v1/agents/{agent_id}/disable", None),
            ("post", f"/api/v1/agents/{agent_id}/enable", None),
            ("delete", f"/api/v1/agents/{agent_id}", None),
        ]:
            response = getattr(self.client, method)(path, headers=stranger, **({"json": body} if body is not None else {}))
            self.assertEqual(response.status_code, 404, f"{method} {path}")
        self.assertEqual(self.get(agent_id, headers=self.login("admin@example.com"))["name"], "Support")

    def test_names_are_unique_per_tenant_only(self) -> None:
        self.make(name="Shared name")
        stranger = self.login("other@example.com", self.ids["other"])
        self.assertEqual(self.make(name="Shared name", headers=stranger).status_code, 201)

    def test_writes_require_the_csrf_token(self) -> None:
        headers = {"X-Tenant-ID": self.ids["tenant"]}
        self.assertEqual(self.client.post("/api/v1/agents", json={"kind": "agent", "name": "NoCsrf"}, headers=headers).status_code, 403)

    def test_agent_actions_are_audited_in_the_assistants_category(self) -> None:
        self.make()
        self.assertEqual(self.audits("agent.created")[0].category, "assistants")


for _name in [n for n in dir(test_assistants.AssistantTests) if n.startswith("test_")]:
    setattr(AgentTests, _name, None)  # inherited fixtures only; the assistant tests run in their own module


if __name__ == "__main__":
    unittest.main()
