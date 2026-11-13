"""T-066: multi-agent collaboration — one level of leaf agents: configuration rules, publish gate, the runtime tool."""

import unittest
from unittest import mock
from uuid import UUID

from langchain_core.messages import ToolMessage
from sqlalchemy import select

import test_agent_runtime
import test_open_api
from beework import agent_teams
from beework.models import Organization
from beework.models.assistants import Assistant, AssistantVersion
from beework.models.tools import ToolInvocation
from beework.runtime.dsl import AssistantDSLv1, ToolMount
from beework.tools.executor import ToolContext
from test_agent_runtime import CAP, ToolChat, tool_call


class TeamConfigTests(test_open_api.OpenApiTests):
    """The configuration rules, the check / publish gate and the dependencies tab, through the real API."""

    def setUp(self) -> None:
        super().setUp()
        self.admin = self.login("admin")

    # -- helpers ----------------------------------------------------------------------------------

    def agent(self, name: str, *, publish: bool = True) -> str:
        agent_id = self.make_assistant(self.admin, name, f"{name} prompt")
        if publish:
            self.assertIn(self.client.post(f"/api/v1/assistants/{agent_id}/publish", json={}, headers=self.admin).status_code, (200, 201))
        return agent_id

    def config_of(self, agent_id: str) -> dict:
        return self.client.get(f"/api/v1/assistants/{agent_id}", headers=self.admin).json()

    def save(self, agent_id: str, *, enabled: bool | None = None, mounts: list[str] | None = None, delegates: dict[str, str] | None = None):
        current = self.config_of(agent_id)
        config = current["config"]
        if enabled is not None:
            config["multi_agent"] = {"enabled": enabled}
        if mounts is not None:
            config["mounts"] = {**config["mounts"], "agent": mounts}
        if delegates is not None:
            config["mount_settings"] = {mid: {"tools": [], "delegate": text} for mid, text in delegates.items()}
        body = {
            "name": current["name"], "description": current["description"], "category": current["category"],
            "visibility": current["visibility"], "visible_org_ids": current["visible_org_ids"], "row_version": current["row_version"],
            "config": config,
        }
        return self.client.put(f"/api/v1/assistants/{agent_id}", json=body, headers=self.admin)

    def team(self, main: str, *leaves: str, delegate: str = "需要时交给它") -> None:
        response = self.save(main, enabled=True, mounts=list(leaves), delegates={leaf: delegate for leaf in leaves})
        self.assertEqual(response.status_code, 200, response.text)

    def checks(self, agent_id: str) -> list[dict]:
        return self.client.get(f"/api/v1/assistants/{agent_id}/checks", headers=self.admin).json()["issues"]

    def blockers(self, agent_id: str) -> set[str]:
        return {i["code"] for i in self.checks(agent_id) if i["severity"] == "blocker"}

    def publish(self, agent_id: str):
        return self.client.post(f"/api/v1/assistants/{agent_id}/publish", json={}, headers=self.admin)

    # -- the configuration rules -----------------------------------------------------------------------

    def test_collaboration_is_off_by_default_and_agents_cannot_be_mounted_without_it(self) -> None:
        main, leaf = self.agent("Main"), self.agent("Leaf")
        self.assertEqual(self.config_of(main)["config"]["multi_agent"], {"enabled": False})
        refused = self.save(main, mounts=[leaf])
        self.assertEqual((refused.status_code, refused.json()["code"]), (422, "assistant.multi_agent_disabled"))
        self.assertEqual(self.save(main, enabled=True, mounts=[leaf]).status_code, 200)
        mounts = self.client.get(f"/api/v1/assistants/{main}/mounts", headers=self.admin).json()
        agent_group = next(g for g in mounts["groups"] if g["kind"] == "agent")
        self.assertEqual([(i["name"], i["state"]) for i in agent_group["items"]], [("Leaf", "available")])
        self.assertIn("agent", mounts["mountable_kinds"])

    def test_switching_collaboration_off_while_agents_are_mounted_is_refused(self) -> None:
        main, leaf = self.agent("Main"), self.agent("Leaf")
        self.team(main, leaf)
        refused = self.save(main, enabled=False)
        self.assertEqual(refused.json()["code"], "assistant.multi_agent_disabled")
        self.assertEqual(self.save(main, enabled=False, mounts=[], delegates={}).status_code, 200)

    def test_only_published_enabled_leaf_agents_can_be_mounted(self) -> None:
        main = self.agent("Main")
        draft = self.agent("Draft agent", publish=False)
        disabled = self.agent("Disabled agent")
        self.client.post(f"/api/v1/assistants/{disabled}/disable", headers=self.admin)
        team_agent = self.agent("Team agent")
        other_leaf = self.agent("Other leaf")
        self.team(team_agent, other_leaf)  # collaborating agents are main agents, not leaves
        expectations = {draft: "尚未发布", disabled: "已停用", team_agent: "不是叶子智能体"}
        for leaf, reason in expectations.items():
            response = self.save(main, enabled=True, mounts=[leaf])
            self.assertEqual((response.status_code, response.json()["code"]), (422, "assistant.mount_unavailable"), leaf)
            self.assertIn(reason, response.json()["detail"])

    def test_an_agent_cannot_mount_itself(self) -> None:
        main = self.agent("Main")
        refused = self.save(main, enabled=True, mounts=[main])
        self.assertEqual((refused.status_code, refused.json()["code"]), (422, "assistant.agent_self_mount"))

    def test_a_mounted_agent_cannot_start_collaborating_and_the_response_names_its_main_agents(self) -> None:
        main, leaf = self.agent("Main"), self.agent("Leaf")
        self.team(main, leaf)
        refused = self.save(leaf, enabled=True)
        self.assertEqual((refused.status_code, refused.json()["code"]), (409, "assistant.multi_agent_mounted"))
        self.assertIn("Main", refused.json()["detail"])
        self.assertFalse(self.config_of(leaf)["config"]["multi_agent"]["enabled"])  # nothing was saved
        # once the main agent lets go of it, it may collaborate
        self.assertEqual(self.save(main, mounts=[], delegates={}).status_code, 200)
        self.assertEqual(self.save(leaf, enabled=True).status_code, 200)

    def test_candidates_list_only_mountable_leaves(self) -> None:
        main = self.agent("Main")
        leaf = self.agent("Leaf")
        self.agent("Unpublished", publish=False)
        types = self.client.get("/api/v1/capabilities/types", headers=self.admin).json()["items"]
        self.assertEqual(next(t for t in types if t["kind"] == "agent")["mountable"], True)
        found = self.client.get("/api/v1/capabilities/candidates", params={"kind": "agent"}, headers=self.admin).json()["items"]
        self.assertEqual({c["name"] for c in found}, {"Main", "Leaf", "Helper"})  # published leaves; the unpublished draft is not offered
        self.assertIn(leaf, {c["id"] for c in found})
        self.assertIn(main, {c["id"] for c in found})

    # -- the delegation description and the publish gate ---------------------------------------------------------

    def test_a_mounted_agent_needs_a_delegation_description_to_pass_the_check_and_publish(self) -> None:
        main, leaf = self.agent("Main"), self.agent("Leaf")
        self.assertEqual(self.save(main, enabled=True, mounts=[leaf]).status_code, 200)  # saving a draft is fine
        self.assertIn("agent.delegate_missing", self.blockers(main))
        refused = self.publish(main)
        self.assertGreaterEqual(refused.status_code, 400)
        self.assertEqual(self.save(main, delegates={leaf: "   "}).status_code, 200)
        self.assertIn("agent.delegate_missing", self.blockers(main))  # blank is missing
        self.assertEqual(self.save(main, delegates={leaf: "客户询问退货时交给它"}).status_code, 200)
        self.assertNotIn("agent.delegate_missing", self.blockers(main))
        self.assertIn(self.publish(main).status_code, (200, 201))

    def test_a_leaf_that_stops_being_valid_blocks_the_main_agents_publish(self) -> None:
        for how in ("disabled", "deleted", "not_leaf"):
            with self.subTest(how):
                main, leaf = self.agent(f"Main {how}"), self.agent(f"Leaf {how}")
                self.team(main, leaf)
                self.assertIn(self.publish(main).status_code, (200, 201))
                self.assertNotIn("agent.invalid", self.blockers(main))
                if how == "disabled":
                    self.client.post(f"/api/v1/assistants/{leaf}/disable", headers=self.admin)
                elif how == "deleted":
                    self.assertEqual(self.client.delete(f"/api/v1/assistants/{leaf}", headers=self.admin).status_code, 409)  # referenced: not deletable
                    with self.session_factory.begin() as db:  # but it can still disappear (data cleanup, another route)
                        from datetime import UTC, datetime

                        db.get(Assistant, UUID(leaf)).deleted_at = datetime.now(UTC)
                else:
                    with self.session_factory.begin() as db:  # it grew a team behind the main agent's back
                        agent = db.get(Assistant, UUID(leaf))
                        agent.draft_config = {**agent.draft_config, "multi_agent": {"enabled": True}}
                self.assertIn("agent.invalid", self.blockers(main))
                self.assertGreaterEqual(self.publish(main).status_code, 400)
                mounts = self.client.get(f"/api/v1/assistants/{main}/mounts", headers=self.admin).json()
                states = {i["state"] for g in mounts["groups"] for i in g["items"]}
                self.assertNotEqual(states, {"available"})  # the capabilities tab shows it as invalid

    def test_the_dependencies_tab_shows_which_main_agents_mount_a_leaf(self) -> None:
        main, leaf = self.agent("Main"), self.agent("Leaf")
        self.team(main, leaf)
        items = self.client.get(f"/api/v1/assistants/{leaf}/dependencies", headers=self.admin).json()["items"]
        self.assertEqual([(i["kind"], i["name"]) for i in items], [("assistant", "Main")])

    def test_the_new_field_is_part_of_the_diff_and_old_data_loads(self) -> None:
        from beework.schemas.assistants import AssistantDraftConfig

        self.assertFalse(AssistantDraftConfig.model_validate({"dsl_version": 1}).multi_agent.enabled)
        main, leaf = self.agent("Main"), self.agent("Leaf")
        self.team(main, leaf)
        preview = self.client.get(f"/api/v1/assistants/{main}/publish-preview", headers=self.admin).json()
        self.assertIn("capabilities", {section["section"] for section in preview.get("diff", [])} | {s.get("section") for s in preview.get("sections", [])})


for _name in [n for n in dir(test_open_api.OpenApiTests) if n.startswith("test_")]:
    setattr(TeamConfigTests, _name, None)


class TeamRuntimeTests(test_agent_runtime.AgentRuntimeTests):
    """The leaf as a tool of the main agent's tool-calling loop."""

    def setUp(self) -> None:
        super().setUp()
        self.leaf_model = ToolChat(script=["叶子的回答"])
        self.leaf_models_made: list[UUID] = []

        def factory(_db, _ctx, assistant, _dsl):
            self.leaf_models_made.append(assistant.id)
            return self.leaf_model

        agent_teams.LEAF_MODEL_FACTORY[:] = [factory]
        self.addCleanup(lambda: agent_teams.LEAF_MODEL_FACTORY.clear())
        self.leaf_id = self.make_leaf("Leaf agent", "LEAF LIVE PROMPT")

    def make_leaf(self, name: str, prompt: str, **dsl_fields) -> UUID:
        with self.session_factory.begin() as db:
            root = db.scalar(select(Organization.id).where(Organization.tenant_id == self.ids["tenant"]))
            leaf = Assistant(tenant_id=self.ids["tenant"], organization_id=root, name=name, description="退货政策专家",
                             draft_config={"prompt": "THE DRAFT SAYS SOMETHING ELSE", "model_id": str(self.ids["chat"])}, status="published")
            db.add(leaf)
            db.flush()
            dsl = AssistantDSLv1(prompt=prompt, model_alias="bw-leaf", **dsl_fields).model_dump(mode="json")
            version = AssistantVersion(
                tenant_id=self.ids["tenant"], assistant_id=leaf.id, version_seq=1, version="1.0",
                snapshot={"config": {"prompt": prompt, "mounts": {}}, "dsl": dsl}, change_note="", change_diff=[],
            )
            db.add(version)
            db.flush()
            leaf.live_version_id = version.id
            return leaf.id

    def team_up(self, *leaves: UUID, delegate: str = "用户问退货政策时交给它") -> None:
        self.configure(
            multi_agent={"enabled": True}, mounts={"agent": [str(leaf) for leaf in leaves]},
            mount_settings={str(leaf): {"tools": [], "delegate": delegate} for leaf in leaves},
        )

    def alias_of(self, llm: ToolChat) -> str:
        return llm.calls[0][1][0]

    # -- delegation ------------------------------------------------------------------------------------------

    def test_the_main_agent_delegates_to_the_leafs_live_version_and_answers_from_its_reply(self) -> None:
        self.team_up(self.leaf_id)
        main = ToolChat(script=[[tool_call("agent_1_agent", {"task": "退货要几天？"})], "叶子说：7 天"])
        run_id, events = self.run_agent(main)
        self.assertEqual((self.run_row(run_id).status, events[-1].data["text"]), ("succeeded", "叶子说：7 天"))
        reply = next(m for m in main.calls[1][0] if isinstance(m, ToolMessage))
        self.assertEqual(reply.content, "叶子的回答")
        leaf_messages = self.leaf_model.calls[0][0]
        self.assertEqual(leaf_messages[0].content, "LEAF LIVE PROMPT")  # the frozen version — not the draft
        self.assertEqual(leaf_messages[-1].content, "退货要几天？")
        self.assertEqual(self.leaf_models_made, [self.leaf_id])

    def test_the_delegation_description_reaches_the_tool_and_the_call_is_recorded(self) -> None:
        self.team_up(self.leaf_id, delegate="仅在用户问退货政策时使用")
        offered = []

        class Spy(ToolChat):
            def bind_tools(self, tools, **kwargs):
                offered.extend(tools)
                return super().bind_tools(tools, **kwargs)

        main = Spy(script=[[tool_call("agent_1_agent", {"task": "x"})], "done"])
        run_id, _events = self.run_agent(main)
        self.assertTrue(offered[0]["function"]["description"].startswith("仅在用户问退货政策时使用"))
        self.assertEqual(offered[0]["function"]["parameters"]["required"], ["task"])
        with self.session_factory() as db:
            rows = db.scalars(select(ToolInvocation).where(ToolInvocation.run_id == UUID(run_id))).all()
        self.assertEqual([(r.source, r.tool_name, r.status) for r in rows], [("agent", f"agent:{self.leaf_id}", "ok")])

    def test_single_agent_mode_offers_no_agent_tools_even_if_mounts_remain(self) -> None:
        self.configure(multi_agent={"enabled": False}, mounts={"agent": [str(self.leaf_id)]}, mount_settings={})
        main = ToolChat(script=["直接回答"])
        self.run_agent(main)
        self.assertEqual(main.calls[0][1], [])

    def test_a_leaf_that_is_no_longer_available_is_not_offered_and_a_forged_call_is_refused(self) -> None:
        self.team_up(self.leaf_id)
        with self.session_factory.begin() as db:
            db.get(Assistant, self.leaf_id).status = "disabled"
        main = ToolChat(script=["我自己回答"])
        self.run_agent(main)
        self.assertEqual(main.calls[0][1], [])
        self.assertEqual(self.leaf_models_made, [])
        # a call the model forges for an agent that was never offered never reaches the leaf
        forged = ToolChat(script=[[tool_call("agent_1_agent", {"task": "x"})], "ok"])
        self.run_agent(forged)
        self.assertEqual(self.leaf_models_made, [])

    def test_the_leaf_uses_its_own_tools_at_depth_two_and_cannot_delegate_further(self) -> None:
        other = self.make_leaf("Other leaf", "OTHER")
        leaf = self.make_leaf(
            "Tool leaf", "TOOL LEAF",
            tools={"mounts": [
                ToolMount(kind="mcp", capability_id=CAP, name="mcp").model_dump(mode="json"),
                ToolMount(kind="agent", capability_id=other, name="other").model_dump(mode="json"),  # a leaf that somehow lists an agent
            ]},
        )
        self.team_up(leaf)
        self.register_source()  # the leaf's tools come from the capability registry (T-162)
        self.leaf_model = ToolChat(script=[[tool_call("mcp_2_search", {"q": "政策"})], "查到了"])
        agent_teams.LEAF_MODEL_FACTORY[:] = [lambda *_: self.leaf_model]
        main = ToolChat(script=[[tool_call("agent_1_agent", {"task": "查政策"})], "结论"])
        run_id, events = self.run_agent(main)
        self.assertEqual(events[-1].data["text"], "结论")
        self.assertTrue(self.leaf_model.calls[0][1] and all(name.startswith("mcp_") for name in self.leaf_model.calls[0][1]))  # its MCP tools only: no agent tool
        self.assertEqual([name for name, _ in self.mcp.executed], [f"mcp:{CAP}:search"])
        with self.session_factory() as db:
            sources = [(r.source, str(r.assistant_id)) for r in db.scalars(select(ToolInvocation).where(ToolInvocation.run_id == UUID(run_id)).order_by(ToolInvocation.created_at))]
        self.assertEqual(sorted(sources), sorted([("mcp", str(leaf)), ("agent", str(self.ids["assistant"]))]))  # the leaf's own tool call carries the leaf; the delegation carries the main agent

    def test_the_leaf_context_is_one_level_deeper_than_the_main_agents(self) -> None:
        seen: list[int] = []
        original = agent_teams.AgentProvider.execute

        def spy(self_, db, ctx: ToolContext, spec, args):
            seen.append(ctx.depth)
            return original(self_, db, ctx, spec, args)

        self.team_up(self.leaf_id)
        with mock.patch.object(agent_teams.AgentProvider, "execute", spy):
            self.run_agent(ToolChat(script=[[tool_call("agent_1_agent", {"task": "x"})], "ok"]))
        self.assertEqual(seen, [1])  # the main agent's call is at depth 1; the leaf's own tools run at depth 2 (tested above)

    def test_arguments_and_leaf_failures_are_told_to_the_main_agent(self) -> None:
        self.team_up(self.leaf_id)
        self.leaf_model = ToolChat(script=[test_agent_runtime.gateway_error()])
        agent_teams.LEAF_MODEL_FACTORY[:] = [lambda *_: self.leaf_model]
        main = ToolChat(script=[[tool_call("agent_1_agent", {"task": ""}, "a"), tool_call("agent_1_agent", {"task": "有内容"}, "b")], "处理完"])
        run_id, _events = self.run_agent(main)
        self.assertEqual(self.run_row(run_id).status, "succeeded")  # a failing leaf does not fail the main run
        replies = [m.content for m in main.calls[1][0] if isinstance(m, ToolMessage)]
        self.assertIn("不能为空", replies[0])  # an empty task is refused before any model is called
        self.assertIn("工具执行失败", replies[1])

    def test_the_leafs_reply_passes_the_main_agents_output_guard(self) -> None:
        self.team_up(self.leaf_id)
        with mock.patch("beework.runtime.executor.scan") as scan:
            scan.return_value = mock.Mock(text="[MASKED]", blocked=False, message="")
            main = ToolChat(script=[[tool_call("agent_1_agent", {"task": "x"})], "done"])
            self.run_agent(main)
        self.assertEqual(next(m for m in main.calls[1][0] if isinstance(m, ToolMessage)).content, "[MASKED]")


for _name in [n for n in dir(test_agent_runtime.AgentRuntimeTests) if n.startswith("test_")]:
    setattr(TeamRuntimeTests, _name, None)

if __name__ == "__main__":
    unittest.main()
