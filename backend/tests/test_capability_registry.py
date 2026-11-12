"""T-160: the unified capability registry — derivation from every source, scope, recall, the API and the backfill."""

import unittest
from unittest import mock
from uuid import UUID

from sqlalchemy import select

import test_assistants
from beework import capability_registry as registry
from beework.knowledge import ContentStats
from beework.models import AuditEvent, Organization
from beework.models.assistants import Assistant, AssistantVersion
from beework.models.capabilities import ApiPlugin, Capability, McpConnection, Skill
from beework.models.capability_registry import CapabilityEntry
from beework.models.flow_connections import FlowConnection
from beework.models.knowledge import KnowledgeBase
from beework.runtime.dsl import AssistantDSLv1

SEARCH = {"name": "search_policy", "description": "Search the refund policy", "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}, "read_only": True}
CREATE = {"name": "create_ticket", "description": "Open a support ticket", "input_schema": {"type": "object"}, "read_only": False}
ORDER = {"name": "get_order", "description": "Get an order by number", "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}}, "writes": False}
CANCEL = {"name": "cancel_order", "description": "Cancel an order", "input_schema": {"type": "object"}, "writes": True}


class RegistryTests(test_assistants.AssistantTests):
    def setUp(self) -> None:
        super().setUp()
        self.admin = self.login("admin@example.com")
        self.tenant = UUID(self.ids["tenant"])
        created = self.create(self.admin, "Main agent")
        self.assertEqual(created.status_code, 201, created.text)
        self.aid = created.json()["id"]  # the assistant whose scope recall works in

    # -- sources ---------------------------------------------------------------------------

    def make_mcp(self, name: str = "Policy MCP", tools=(SEARCH, CREATE), ticked=("search_policy", "create_ticket"), status: str = "available") -> UUID:
        with self.session_factory.begin() as db:
            cap = Capability(tenant_id=self.tenant, kind="mcp", name=name, description="d", status=status)
            db.add(cap)
            db.flush()
            db.add(McpConnection(capability_id=cap.id, tenant_id=self.tenant, url="https://mcp.example.com/mcp", tools=list(tools), selected_tools=list(ticked)))
            return cap.id

    def make_api(self, name: str = "Order API", tools=(ORDER, CANCEL), ticked=("get_order",), status: str = "available") -> UUID:
        with self.session_factory.begin() as db:
            cap = Capability(tenant_id=self.tenant, kind="api", name=name, description="d", status=status)
            db.add(cap)
            db.flush()
            db.add(ApiPlugin(capability_id=cap.id, tenant_id=self.tenant, base_url="https://api.example.com", tools=list(tools), selected_tools=list(ticked)))
            return cap.id

    def make_skill(self, name: str = "Refund skill", status: str = "available") -> UUID:
        with self.session_factory.begin() as db:
            cap = Capability(tenant_id=self.tenant, kind="skill", name=name, description="How to refund", status=status)
            db.add(cap)
            db.flush()
            db.add(Skill(capability_id=cap.id, tenant_id=self.tenant, security_status="passed"))
            return cap.id

    def make_dify(self, name: str = "Return flow", kind: str = "chatflow", status: str = "enabled") -> UUID:
        with self.session_factory.begin() as db:
            conn = FlowConnection(
                tenant_id=self.tenant, kind=kind, name=name, description="Handles returns", source_type="dify", base_url="https://api.dify.example.com/v1",
                app_id="a", input_mapping={}, output_mapping={}, status=status, row_version=1,
            )
            db.add(conn)
            db.flush()
            return conn.id

    def make_leaf(self, name: str = "Leaf agent", description: str = "退货政策专家", published: bool = True) -> UUID:
        with self.session_factory.begin() as db:
            root = db.scalar(select(Organization.id).where(Organization.tenant_id == self.tenant))
            leaf = Assistant(tenant_id=self.tenant, organization_id=root, name=name, description=description, draft_config={"prompt": "p"}, status="published" if published else "draft")
            db.add(leaf)
            db.flush()
            if published:
                dsl = AssistantDSLv1(prompt="p", model_alias="bw-leaf").model_dump(mode="json")
                version = AssistantVersion(tenant_id=self.tenant, assistant_id=leaf.id, version_seq=1, version="1.0", snapshot={"config": {"prompt": "p", "mounts": {}}, "dsl": dsl}, change_note="", change_diff=[])
                db.add(version)
                db.flush()
                leaf.live_version_id = version.id
            return leaf.id

    def make_kb(self, name: str = "退货知识库", kb_type: str = "standard") -> UUID:
        with self.session_factory.begin() as db:
            root = db.scalar(select(Organization.id).where(Organization.tenant_id == self.tenant))
            kb = KnowledgeBase(tenant_id=self.tenant, organization_id=root, name=name, description="退货与退款的制度", kb_type=kb_type)
            db.add(kb)
            db.flush()
            return kb.id

    def indexed(self):
        """Knowledge bases count as indexed (the content providers are real tables this test does not fill)."""
        patcher = mock.patch("beework.knowledge_mounts.content_stats", lambda db, kb: ContentStats(documents=1, faqs=0, indexed_documents=1, processing_documents=0, published_faqs=0))
        patcher.start()
        self.addCleanup(patcher.stop)

    def entries(self, **where) -> list[CapabilityEntry]:
        with self.session_factory() as db:
            statement = select(CapabilityEntry).where(CapabilityEntry.tenant_id == self.tenant)
            for key, value in where.items():
                statement = statement.where(getattr(CapabilityEntry, key) == value)
            return list(db.scalars(statement.order_by(CapabilityEntry.kind, CapabilityEntry.identifier)))

    def live(self, source_id: UUID) -> dict[str, CapabilityEntry]:
        return {e.identifier or e.kind: e for e in self.entries(source_id=source_id) if e.removed_at is None}

    def mount(self, *, team: bool = False, **mounts) -> None:
        with self.session_factory.begin() as db:
            assistant = db.get(Assistant, UUID(self.aid))
            ids = {k: [str(i) for i in v] for k, v in mounts.items() if k != "settings"}
            assistant.draft_config = {
                **assistant.draft_config, "multi_agent": {"enabled": team}, "mounts": {**assistant.draft_config.get("mounts", {}), **ids},
                "mount_settings": mounts.get("settings", {}),
            }

    def recall(self, query: str, k: int = 8):
        response = self.client.get(f"/api/v1/assistants/{self.aid}/registry/recall", params={"q": query, "k": k}, headers=self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def list_entries(self, headers=None, **params) -> dict:
        response = self.client.get("/api/v1/registry/entries", params=params, headers=headers or self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def put(self, entry_id, headers=None, **changes):
        current = self.client.get(f"/api/v1/registry/entries/{entry_id}", headers=headers or self.admin).json()
        body = {
            "intent_description": current["intent_description"], "intent_examples": current["intent_examples"], "direct_enabled": current["direct_enabled"],
            "direct_output": current["direct_output"], "timeout_ms": current["timeout_ms"], "retry": current["retry"], "row_version": current["row_version"], **changes,
        }
        return self.client.put(f"/api/v1/registry/entries/{entry_id}", json=body, headers=headers or self.admin)

    # -- derivation ----------------------------------------------------------------------------------

    def test_every_kind_of_source_becomes_entries_with_the_facts_the_source_owns(self) -> None:
        mcp, api, skill, dify = self.make_mcp(), self.make_api(), self.make_skill(), self.make_dify(kind="workflow")
        leaf, kb = self.make_leaf(), self.make_kb()
        found = self.live(mcp)
        self.assertEqual(sorted(found), ["create_ticket", "search_policy"])
        self.assertEqual((found["search_policy"].kind, found["search_policy"].name, found["search_policy"].description, found["search_policy"].writes), ("mcp_tool", "search_policy", "Search the refund policy", False))
        self.assertEqual((found["create_ticket"].writes, found["search_policy"].input_schema["properties"]["q"]), (True, {"type": "string"}))
        self.assertEqual(found["search_policy"].meta["source_name"], "Policy MCP")
        api_entries = self.live(api)
        self.assertEqual(sorted(api_entries), ["cancel_order", "get_order"])  # every tool is registered; the ticked ones are what is usable
        self.assertEqual((api_entries["cancel_order"].kind, api_entries["cancel_order"].writes), ("api_tool", True))
        (skill_entry,) = self.live(skill).values()
        self.assertEqual((skill_entry.kind, skill_entry.name, skill_entry.writes, skill_entry.identifier), ("skill", "Refund skill", False, ""))
        (dify_entry,) = self.live(dify).values()
        self.assertEqual((dify_entry.kind, dify_entry.name), ("dify_workflow", "Return flow"))
        (agent_entry,) = self.live(leaf).values()
        self.assertEqual((agent_entry.kind, agent_entry.name, agent_entry.description, agent_entry.input_schema["required"]), ("agent", "Leaf agent", "退货政策专家", ["task"]))
        (kb_entry,) = self.live(kb).values()
        self.assertEqual((kb_entry.kind, kb_entry.name, kb_entry.meta["kb_type"], kb_entry.input_schema["required"]), ("kb", "退货知识库", "standard", ["query"]))

    def test_sub_agents_and_knowledge_bases_are_entries_too_but_not_direct_by_default(self) -> None:
        defaults = {
            "mcp": self.make_mcp(), "skill": self.make_skill(), "dify": self.make_dify(), "agent": self.make_leaf(), "kb": self.make_kb(),
        }
        flags = {name: [e.direct_enabled for e in self.live(source).values()] for name, source in defaults.items()}
        self.assertEqual(flags, {"mcp": [True, True], "skill": [True], "dify": [True], "agent": [False], "kb": [False]})
        self.assertTrue(all(not e.direct_output and e.intent_description == "" and e.intent_examples == [] for e in self.entries()))

    def test_the_source_owns_the_facts_and_the_registry_owns_the_rest_and_the_id_never_changes(self) -> None:
        mcp = self.make_mcp()
        before = self.live(mcp)["search_policy"]
        self.assertEqual(self.put(before.id, intent_description="用户查退款政策时用", intent_examples=["退款几天到账", "怎么退货"], timeout_ms=5000, direct_enabled=False).status_code, 200)
        with self.session_factory.begin() as db:  # the source changes: a rename and a new description of a tool
            cap = db.get(Capability, mcp)
            cap.name = "Policy MCP (renamed)"
            conn = db.get(McpConnection, mcp)
            conn.tools = [{**SEARCH, "description": "Search policies (new)"}, CREATE]
        after = self.live(mcp)["search_policy"]
        self.assertEqual((after.id, after.description, after.meta["source_name"]), (before.id, "Search policies (new)", "Policy MCP (renamed)"))
        self.assertEqual((after.intent_description, after.intent_examples, after.timeout_ms, after.direct_enabled), ("用户查退款政策时用", ["退款几天到账", "怎么退货"], 5000, False))  # untouched

    def test_a_vanished_tool_is_marked_removed_and_comes_back_with_the_same_id_and_its_settings(self) -> None:
        mcp = self.make_mcp()
        entry = self.live(mcp)["create_ticket"]
        self.put(entry.id, intent_description="开工单", direct_enabled=True)
        with self.session_factory.begin() as db:
            db.get(McpConnection, mcp).tools = [SEARCH]
        self.assertEqual(sorted(self.live(mcp)), ["search_policy"])
        self.assertIsNotNone(next(e for e in self.entries(source_id=mcp) if e.identifier == "create_ticket").removed_at)  # kept, only marked
        with self.session_factory.begin() as db:
            db.get(McpConnection, mcp).tools = [SEARCH, CREATE]
        back = self.live(mcp)["create_ticket"]
        self.assertEqual((back.id, back.intent_description, back.removed_at), (entry.id, "开工单", None))

    def test_a_deleted_source_removes_its_entries(self) -> None:
        mcp, kb = self.make_mcp(), self.make_kb()
        with self.session_factory.begin() as db:
            from beework.models import utc_now

            db.get(Capability, mcp).deleted_at = utc_now()
            db.get(KnowledgeBase, kb).deleted_at = utc_now()
        self.assertEqual((self.live(mcp), self.live(kb)), ({}, {}))

    def test_a_sub_agent_is_an_entry_only_while_it_can_be_called_as_a_leaf(self) -> None:
        draft = self.make_leaf("Not yet", published=False)
        self.assertEqual(self.live(draft), {})
        leaf = self.make_leaf("Published")
        self.assertEqual(list(self.live(leaf)), ["agent"])
        with self.session_factory.begin() as db:
            db.get(Assistant, leaf).status = "disabled"
        self.assertEqual(self.live(leaf), {})
        with self.session_factory.begin() as db:
            db.get(Assistant, leaf).status = "published"
            db.get(Assistant, leaf).name = "Renamed"
        self.assertEqual(self.live(leaf)["agent"].name, "Renamed")
        with self.session_factory.begin() as db:  # it grew a team behind the scenes: no longer a leaf
            agent = db.get(Assistant, leaf)
            agent.draft_config = {**agent.draft_config, "multi_agent": {"enabled": True}}
        self.assertEqual(self.live(leaf), {})

    # -- the status is read from the source ----------------------------------------------------------------

    def statuses(self) -> dict[str, str]:
        return {f"{e['kind']}:{e['identifier'] or e['name']}": e["status"] for e in self.list_entries(limit=500)["items"]}

    def test_the_status_follows_the_source_and_the_ticked_tools_without_any_cache(self) -> None:
        self.indexed()
        mcp = self.make_mcp(ticked=("search_policy",))
        api = self.make_api()
        dify = self.make_dify()
        self.make_kb()
        found = self.statuses()
        self.assertEqual(
            {k: v for k, v in found.items() if k.startswith(("mcp_tool", "api_tool", "dify", "kb"))},
            {"mcp_tool:search_policy": "available", "mcp_tool:create_ticket": "disabled", "api_tool:get_order": "available", "api_tool:cancel_order": "disabled",
             "dify_chatflow:Return flow": "available", "kb:退货知识库": "available"},
        )
        with self.session_factory.begin() as db:
            db.get(Capability, mcp).status = "disabled"
            db.get(FlowConnection, dify).status = "disabled"
            db.get(ApiPlugin, api).selected_tools = []  # the administrator un-ticked every tool
        after = self.statuses()
        self.assertEqual((after["mcp_tool:search_policy"], after["dify_chatflow:Return flow"], after["api_tool:get_order"]), ("disabled", "disabled", "disabled"))

    def test_a_knowledge_base_without_indexed_content_is_disabled_until_it_has_some(self) -> None:
        self.make_kb()
        self.assertEqual(self.statuses()["kb:退货知识库"], "disabled")
        self.indexed()
        self.assertEqual(self.statuses()["kb:退货知识库"], "available")

    # -- scope and recall ---------------------------------------------------------------------------------

    def test_an_assistant_sees_only_what_it_mounted_and_ticked(self) -> None:
        mcp, api, other = self.make_mcp(), self.make_api(), self.make_mcp("Other MCP", tools=(SEARCH,), ticked=("search_policy",))
        self.mount(mcp=[mcp], api=[api])
        with self.session_factory() as db:
            seen = registry.visible_entries(db, self.tenant, db.get(Assistant, UUID(self.aid)).draft_config)
        self.assertEqual(sorted((s.entry.kind, s.entry.identifier) for s in seen), [("api_tool", "get_order"), ("mcp_tool", "create_ticket"), ("mcp_tool", "search_policy")])
        self.assertNotIn(other, {s.entry.source_id for s in seen})  # an unmounted source is never in scope
        self.mount(mcp=[mcp], api=[], settings={str(mcp): {"tools": ["search_policy"], "delegate": "x"}})  # only some tools ticked for this agent
        with self.session_factory() as db:
            seen = registry.visible_entries(db, self.tenant, db.get(Assistant, UUID(self.aid)).draft_config)
        self.assertEqual([s.entry.identifier for s in seen], ["search_policy"])

    def test_sub_agents_are_in_scope_only_while_collaboration_is_on(self) -> None:
        leaf = self.make_leaf()
        self.mount(agent=[leaf], team=False)
        with self.session_factory() as db:
            self.assertEqual(registry.visible_entries(db, self.tenant, db.get(Assistant, UUID(self.aid)).draft_config), [])
        self.mount(agent=[leaf], team=True)
        with self.session_factory() as db:
            seen = registry.visible_entries(db, self.tenant, db.get(Assistant, UUID(self.aid)).draft_config)
        self.assertEqual([(s.entry.kind, s.entry.name) for s in seen], [("agent", "Leaf agent")])

    def test_recall_ranks_by_name_examples_intent_and_description_inside_the_scope(self) -> None:
        self.indexed()
        mcp, kb, leaf = self.make_mcp(), self.make_kb(), self.make_leaf()
        policy = self.live(mcp)["search_policy"]
        ticket = self.live(mcp)["create_ticket"]
        self.put(policy.id, intent_description="用户询问退款政策、退货条件时使用", intent_examples=["退货要几天", "退款多久到账"])
        self.put(ticket.id, intent_description="用户要提交工单或投诉时使用", intent_examples=["我要投诉"])
        self.mount(mcp=[mcp], kb=[kb], agent=[leaf], team=True)
        ranked = self.recall("退款多久到账")
        self.assertEqual(ranked["scope"], 4)
        self.assertEqual(ranked["items"][0]["entry"]["name"], "search_policy")
        self.assertGreater(ranked["items"][0]["score"], 0.5)
        self.assertEqual([i["entry"]["name"] for i in self.recall("我要投诉")["items"]][0], "create_ticket")
        english = self.recall("Open a support ticket")
        self.assertEqual(english["items"][0]["entry"]["name"], "create_ticket")
        self.assertEqual(self.recall("zzzzqqqq")["items"], [])
        kinds = {i["entry"]["kind"] for i in self.recall("退货 退款 政策", k=20)["items"]}
        self.assertTrue({"mcp_tool", "kb", "agent"} <= kinds)  # sub-agents and knowledge bases take part like any other capability

    def test_recall_is_limited_to_the_scope_the_available_ones_and_k(self) -> None:
        mcp = self.make_mcp(ticked=("search_policy",))
        self.make_mcp("Not mounted", tools=({**SEARCH, "name": "search_secret", "description": "Search the refund policy"},), ticked=("search_secret",))
        self.mount(mcp=[mcp])
        names = [i["entry"]["name"] for i in self.recall("Search the refund policy")["items"]]
        self.assertEqual(names, ["search_policy"])  # not the unmounted one, not the unticked create_ticket
        self.assertLessEqual(len(self.recall("search", k=1)["items"]), 1)
        self.assertEqual(self.client.get(f"/api/v1/assistants/{self.aid}/registry/recall", params={"q": ""}, headers=self.admin).status_code, 422)

    def test_the_tokenizer_matches_chinese_by_character_pairs_and_ignores_case(self) -> None:
        self.assertEqual(registry.tokens("退款政策"), {"退款", "款政", "政策"})
        self.assertEqual(registry.tokens("Refund POLICY v2 a"), {"refund", "policy", "v2"})
        self.assertEqual(registry.tokens(""), set())

    # -- the API ------------------------------------------------------------------------------------------

    def test_the_list_shows_every_kind_with_where_each_is_managed(self) -> None:
        self.indexed()
        mcp, leaf, kb = self.make_mcp(), self.make_leaf(), self.make_kb()
        items = self.list_entries()["items"]
        owners = {i["kind"]: i["owner"] for i in items}
        self.assertEqual({k: v["page"] for k, v in owners.items()}, {"mcp_tool": "capabilities", "agent": "assistants", "kb": "knowledge"})
        self.assertEqual({str(mcp), str(leaf), str(kb)}, {v["id"] for v in owners.values()})
        self.assertEqual(owners["mcp_tool"]["name"], "Policy MCP")
        self.assertTrue(all(i["input_schema"] is None for i in items))  # the list does not carry schemas
        self.assertEqual(self.list_entries(kind="kb")["total"], 1)
        self.assertEqual(self.list_entries(source_id=str(mcp))["total"], 2)
        self.assertEqual([i["name"] for i in self.list_entries(q="TICKET")["items"]], ["create_ticket"])
        self.assertEqual({i["status"] for i in self.list_entries(status="available")["items"]}, {"available"})

    def test_an_entry_is_read_with_its_schema_and_resolved_policy(self) -> None:
        entry = self.live(self.make_mcp())["search_policy"]
        data = self.client.get(f"/api/v1/registry/entries/{entry.id}", headers=self.admin).json()
        self.assertEqual(data["input_schema"]["properties"]["q"], {"type": "string"})
        self.assertEqual(data["policy"], {"timeout_ms": 30000, "max_attempts": 1, "backoff_ms": 0, "retry_on": []})

    def test_editing_changes_only_what_the_registry_holds_and_is_audited(self) -> None:
        entry = self.live(self.make_mcp())["search_policy"]
        response = self.put(
            entry.id, intent_description="  查政策  ", intent_examples=["a", " b ", ""], direct_enabled=True, direct_output=True, timeout_ms=8000,
            retry={"max_attempts": 3, "backoff_ms": 200, "retry_on": ["timeout", "network", "timeout"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual((data["intent_description"], data["intent_examples"], data["direct_output"], data["timeout_ms"]), ("查政策", ["a", "b"], True, 8000))
        self.assertEqual(data["policy"], {"timeout_ms": 8000, "max_attempts": 3, "backoff_ms": 200, "retry_on": ["network", "timeout"]})
        self.assertEqual((data["name"], data["description"], data["writes"], data["row_version"]), ("search_policy", "Search the refund policy", False, entry.row_version + 1))
        extra = self.client.put(f"/api/v1/registry/entries/{entry.id}", json={**{k: data[k] for k in ("intent_description", "intent_examples", "direct_enabled", "direct_output", "timeout_ms", "retry", "row_version")}, "name": "hijacked", "writes": True}, headers=self.admin)
        self.assertEqual((extra.status_code, extra.json()["name"], extra.json()["writes"]), (200, "search_policy", False))  # the source's facts cannot be edited here
        with self.session_factory() as db:
            self.assertEqual(len(list(db.scalars(select(AuditEvent).where(AuditEvent.action == "registry_entry.updated")))), 2)

    def test_direct_output_means_nothing_without_direct_calls(self) -> None:
        entry = self.live(self.make_kb())["kb"]
        self.assertFalse(self.put(entry.id, direct_enabled=False, direct_output=True).json()["direct_output"])

    def test_invalid_edits_are_refused(self) -> None:
        entry = self.live(self.make_mcp())["search_policy"]
        for bad in (
            {"intent_description": "x" * 501}, {"intent_examples": ["x"] * 11}, {"intent_examples": ["x" * 201]}, {"timeout_ms": 999}, {"timeout_ms": 300001},
            {"retry": {"max_attempts": 6}}, {"retry": {"max_attempts": 0}}, {"retry": {"backoff_ms": 60001}}, {"retry": {"retry_on": ["whenever"]}},
        ):
            self.assertEqual(self.put(entry.id, **bad).status_code, 422, bad)
        stale = self.client.put(f"/api/v1/registry/entries/{entry.id}", json={"direct_enabled": True, "row_version": entry.row_version + 9}, headers=self.admin)
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "conflict.stale"))

    def test_viewers_read_but_cannot_edit_and_other_tenants_see_nothing(self) -> None:
        entry = self.live(self.make_mcp())["search_policy"]
        viewer = self.login("viewer@example.com")
        self.assertEqual(self.list_entries(headers=viewer)["total"], 2)
        self.assertEqual(self.put(entry.id, headers=viewer, direct_enabled=False).status_code, 403)
        self.assertEqual(self.client.post("/api/v1/registry/resync", headers=viewer).status_code, 403)
        stranger = self.login("other@example.com", self.ids["other"])
        self.assertEqual(self.list_entries(headers=stranger)["items"], [])
        self.assertEqual(self.client.get(f"/api/v1/registry/entries/{entry.id}", headers=stranger).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/assistants/{self.aid}/registry/recall", params={"q": "x"}, headers=stranger).status_code, 404)
        self.admin = self.login("admin@example.com")
        self.assertEqual(self.client.put(f"/api/v1/registry/entries/{entry.id}", json={"direct_enabled": True, "row_version": 1}, headers={"X-Tenant-ID": self.ids["tenant"]}).status_code, 403)  # no CSRF token

    # -- backfill and repair ------------------------------------------------------------------------------

    def test_a_tenant_that_predates_the_registry_is_backfilled_on_first_read(self) -> None:
        mcp, leaf = self.make_mcp(), self.make_leaf()
        with self.session_factory.begin() as db:  # data from before the registry existed
            for entry in db.scalars(select(CapabilityEntry)):
                db.delete(entry)
        self.assertEqual(self.entries(), [])
        listed = self.list_entries()
        self.assertEqual({(i["kind"], i["name"]) for i in listed["items"]}, {("mcp_tool", "search_policy"), ("mcp_tool", "create_ticket"), ("agent", "Leaf agent")})
        self.assertEqual({e.source_id for e in self.entries()}, {mcp, leaf})

    def test_resync_repairs_what_changed_behind_the_applications_back(self) -> None:
        mcp = self.make_mcp()
        with self.session_factory.begin() as db:
            from sqlalchemy import text

            db.execute(text("UPDATE mcp_connections SET tools = :t WHERE capability_id = :c"), {"t": '[{"name": "only_one", "description": "x", "input_schema": {}, "read_only": true}]', "c": str(mcp).replace("-", "")})
        self.assertEqual(sorted(self.live(mcp)), ["create_ticket", "search_policy"])  # the listener did not see a raw UPDATE
        response = self.client.post("/api/v1/registry/resync", headers=self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(sorted(self.live(mcp)), ["only_one"])
        with self.session_factory() as db:
            self.assertEqual(len(list(db.scalars(select(AuditEvent).where(AuditEvent.action == "registry.resynced")))), 1)

    def test_entries_of_other_tenants_are_never_mixed(self) -> None:
        self.make_mcp()
        with self.session_factory.begin() as db:
            cap = Capability(tenant_id=UUID(self.ids["other"]), kind="mcp", name="Foreign", description="d", status="available")
            db.add(cap)
            db.flush()
            db.add(McpConnection(capability_id=cap.id, tenant_id=UUID(self.ids["other"]), url="https://x.example.com", tools=[SEARCH], selected_tools=["search_policy"]))
        self.assertEqual({e.tenant_id for e in self.entries()}, {self.tenant})
        stranger = self.login("other@example.com", self.ids["other"])
        self.assertEqual([i["owner"]["name"] for i in self.list_entries(headers=stranger)["items"]], ["Foreign"])

    # -- the policy ---------------------------------------------------------------------------------------

    def test_the_policy_is_the_entrys_own_over_the_platform_default(self) -> None:
        entry = CapabilityEntry(timeout_ms=None, retry=None)
        self.assertEqual(registry.resolve_policy(entry), {"timeout_ms": 30000, "max_attempts": 1, "backoff_ms": 0, "retry_on": []})
        entry = CapabilityEntry(timeout_ms=9000, retry={"max_attempts": 2, "backoff_ms": 100, "retry_on": ["5xx"]})
        self.assertEqual(registry.resolve_policy(entry), {"timeout_ms": 9000, "max_attempts": 2, "backoff_ms": 100, "retry_on": ["5xx"]})
        partial = CapabilityEntry(timeout_ms=None, retry={"max_attempts": 3})
        self.assertEqual(registry.resolve_policy(partial), {"timeout_ms": 30000, "max_attempts": 3, "backoff_ms": 0, "retry_on": []})


for _parent in RegistryTests.__mro__[1:]:
    for _name in [n for n in vars(_parent) if n.startswith("test_")]:
        setattr(RegistryTests, _name, None)  # the inherited assistant tests run in their own module


if __name__ == "__main__":
    unittest.main()
