"""T-070: the capability catalogue — mount types, the unified reference table, invalid references, dependencies."""

import unittest
from uuid import UUID, uuid4

from sqlalchemy import select

import test_assistants
from beework.capabilities import CANDIDATE_LISTERS, MOUNT_RESOLVERS, MountItem
from beework.capability_catalog import (
    assistant_edges,
    flow_edges,
    invalid_references,
    references_to,
)
from beework.models.assistants import Assistant, AssistantVersion
from beework.models.capability_refs import ResourceReference
from beework.models.channels import ChannelInstance


class CatalogTests(test_assistants.AssistantTests):
    def setUp(self) -> None:
        super().setUp()
        self.admin = self.login("admin@example.com")
        # A stand-in capability type (what T-072–T-074 register): two skills of this tenant, one not usable yet.
        self.skill_ok, self.skill_off = uuid4(), uuid4()
        tenant_id = UUID(self.ids["tenant"])

        def resolve(db, tenant, ids):
            known = {self.skill_ok: ("Translate", "available"), self.skill_off: ("Unchecked", "disabled")}
            return {i: MountItem(i, "skill", known[i][0], known[i][1]) for i in ids if i in known and tenant == tenant_id}

        def lister(db, membership, q, limit):
            if membership.tenant_id != tenant_id:
                return []
            items = [{"id": self.skill_ok, "kind": "skill", "name": "Translate", "meta": {"origin": "built-in"}}]
            return [i for i in items if not q or q.lower() in i["name"].lower()][:limit]

        MOUNT_RESOLVERS["skill"], CANDIDATE_LISTERS["skill"] = resolve, lister
        self.addCleanup(lambda: (MOUNT_RESOLVERS.pop("skill", None), CANDIDATE_LISTERS.pop("skill", None)))

    # -- helpers ------------------------------------------------------------------

    def mount(self, assistant_id: str, **mounts):
        current = self.client.get(f"/api/v1/assistants/{assistant_id}", headers=self.admin).json()
        config = {**current["config"], "mounts": {**current["config"]["mounts"], **{k: [str(i) for i in v] for k, v in mounts.items()}}}
        body = {"name": current["name"], "description": current["description"], "category": current["category"], "config": config,
                "row_version": current["row_version"]}
        return self.client.put(f"/api/v1/assistants/{assistant_id}", json=body, headers=self.admin)

    def edges(self, referrer_type: str, referrer_id: str, layer: str | None = None) -> set[tuple]:
        with self.session_factory() as db:
            rows = db.scalars(select(ResourceReference).where(
                ResourceReference.referrer_type == referrer_type, ResourceReference.referrer_id == UUID(referrer_id)))
            return {(r.layer, r.relation, r.target_type, r.target_id) for r in rows if layer in (None, r.layer)}

    def flow(self, name: str, target_id: str | None = None, target_kind: str = "assistant") -> str:
        flow_id = self.client.post("/api/v1/agents", json={"kind": "workflow", "name": name}, headers=self.admin).json()["id"]
        if target_id:
            current = self.client.get(f"/api/v1/agents/{flow_id}", headers=self.admin).json()
            node = {"id": "n9", "type": "agent", "name": "Call", "target_id": target_id, "target_kind": target_kind}
            body = {"row_version": current["row_version"], "config": {**current["config"], "nodes": [node]}}
            self.assertEqual(self.client.put(f"/api/v1/agents/{flow_id}", json=body, headers=self.admin).status_code, 200)
        return flow_id

    # -- edges and the reference table ----------------------------------------------

    def test_edges_come_from_mounts_and_flow_nodes(self) -> None:
        a, b = uuid4(), uuid4()
        config = {"mounts": {"kb": [str(a)], "agent": [str(b)], "chatflow": [str(a)], "flow": [], "bad": ["x"]}}
        self.assertEqual(assistant_edges(config), {("mount", "kb", a), ("mount", "assistant", b), ("mount", "chatflow_connection", a)})
        self.assertEqual(assistant_edges({"mounts": {"kb": ["not-a-uuid"]}}), set())
        nodes = {"nodes": [
            {"type": "agent", "target_id": str(a), "target_kind": "assistant"}, {"type": "copilot", "target_id": str(b), "target_kind": "flow"},
            {"type": "note", "target_id": str(a), "target_kind": "flow"}, {"type": "agent", "target_id": None, "target_kind": "flow"},
        ]}
        self.assertEqual(flow_edges(nodes), {("node", "assistant", a), ("node", "flow", b)})

    def test_saving_replaces_the_rows_of_the_draft_as_a_whole(self) -> None:
        assistant_id = self.create(self.admin).json()["id"]
        self.assertEqual(self.edges("assistant", assistant_id), set())
        self.assertEqual(self.mount(assistant_id, skill=[self.skill_ok]).status_code, 200)
        self.assertEqual(self.edges("assistant", assistant_id), {("draft", "mount", "skill", self.skill_ok)})
        self.assertEqual(self.mount(assistant_id, skill=[]).status_code, 200)  # the old edge is gone, not accumulated
        self.assertEqual(self.edges("assistant", assistant_id), set())

    def test_the_live_layer_follows_the_live_version_and_restore_changes_only_the_draft(self) -> None:
        assistant_id = self.create(self.admin).json()["id"]
        self.mount(assistant_id, skill=[self.skill_ok])
        with self.session_factory.begin() as db:
            assistant = db.get(Assistant, UUID(assistant_id))
            version = AssistantVersion(
                tenant_id=assistant.tenant_id, assistant_id=assistant.id, version_seq=1, version="1.0",
                snapshot={"config": assistant.draft_config}, change_note="", change_diff=[],
            )
            db.add(version)
            db.flush()
            assistant.live_version_id = version.id
        self.assertEqual(self.edges("assistant", assistant_id), {("draft", "mount", "skill", self.skill_ok), ("live", "mount", "skill", self.skill_ok)})
        self.mount(assistant_id, skill=[])  # the draft no longer mounts it, the live version still does
        self.assertEqual(self.edges("assistant", assistant_id), {("live", "mount", "skill", self.skill_ok)})
        with self.session_factory() as db:
            (ref,) = references_to(db, UUID(self.ids["tenant"]), "skill", self.skill_ok)
            self.assertEqual((ref.name, ref.layers), ("Helper", {"live"}))

    def test_deleting_the_referrer_removes_its_rows_and_it_stops_counting(self) -> None:
        first, second = self.create(self.admin, "One").json()["id"], self.create(self.admin, "Two").json()["id"]
        self.mount(first, skill=[self.skill_ok])
        self.mount(second, skill=[self.skill_ok])
        tenant = UUID(self.ids["tenant"])
        with self.session_factory() as db:
            self.assertEqual([r.name for r in references_to(db, tenant, "skill", self.skill_ok)], ["One", "Two"])
        self.assertEqual(self.client.delete(f"/api/v1/assistants/{second}", headers=self.admin).status_code, 204)
        self.assertEqual(self.edges("assistant", second), set())
        with self.session_factory() as db:
            self.assertEqual([r.name for r in references_to(db, tenant, "skill", self.skill_ok)], ["One"])

    def test_references_are_isolated_per_tenant(self) -> None:
        assistant_id = self.create(self.admin).json()["id"]
        self.mount(assistant_id, skill=[self.skill_ok])
        with self.session_factory() as db:
            self.assertEqual(references_to(db, UUID(self.ids["other"]), "skill", self.skill_ok), [])

    def test_a_copy_gets_its_own_rows(self) -> None:
        assistant_id = self.create(self.admin).json()["id"]
        self.mount(assistant_id, skill=[self.skill_ok])
        copy = self.client.post(f"/api/v1/assistants/{assistant_id}/copy", headers=self.admin)
        self.assertEqual(copy.status_code, 201, copy.text)
        self.assertEqual(self.edges("assistant", copy.json()["id"]), {("draft", "mount", "skill", self.skill_ok)})

    def test_flows_are_referrers_too_and_the_flow_counts_use_the_same_table(self) -> None:
        assistant_id = self.create(self.admin).json()["id"]
        flow_id = self.flow("Caller", assistant_id, "assistant")
        self.assertEqual(self.edges("flow", flow_id), {("draft", "node", "assistant", UUID(assistant_id))})
        with self.session_factory() as db:
            (ref,) = references_to(db, UUID(self.ids["tenant"]), "assistant", UUID(assistant_id))
            self.assertEqual((ref.type, ref.kind, ref.name), ("flow", "workflow", "Caller"))
        target = self.flow("Target")
        self.flow("Calls target", target, "flow")
        self.assertEqual(self.client.get(f"/api/v1/agents/{target}/references", headers=self.admin).json()["total"], 1)
        self.client.delete(f"/api/v1/agents/{flow_id}", headers=self.admin)
        self.assertEqual(self.edges("flow", flow_id), set())

    # -- invalid references ---------------------------------------------------------

    def test_invalid_references_report_deleted_and_not_available_mounts(self) -> None:
        assistant_id = self.create(self.admin).json()["id"]
        gone = uuid4()
        with self.session_factory.begin() as db:
            assistant = db.get(Assistant, UUID(assistant_id))
            assistant.draft_config = {**assistant.draft_config, "mounts": {"skill": [str(self.skill_ok), str(self.skill_off), str(gone)]}}
        with self.session_factory() as db:
            found = {(i.id, i.state) for i in invalid_references(db, db.get(Assistant, UUID(assistant_id)))}
        self.assertEqual(found, {(self.skill_off, "disabled"), (gone, "deleted")})

    # -- mounting through the API ----------------------------------------------------

    def test_any_registered_type_can_be_mounted_when_available_and_not_otherwise(self) -> None:
        assistant_id = self.create(self.admin).json()["id"]
        self.assertEqual(self.mount(assistant_id, skill=[self.skill_ok]).status_code, 200)
        unavailable = self.mount(assistant_id, skill=[self.skill_ok, self.skill_off])
        self.assertEqual((unavailable.status_code, unavailable.json()["code"]), (422, "assistant.mount_unavailable"))
        self.assertEqual(self.mount(assistant_id, skill=[self.skill_ok, uuid4()]).status_code, 422)
        self.assertEqual(self.mount(assistant_id, mcp=[uuid4()]).json()["code"], "assistant.mount_unavailable")  # not registered
        self.assertEqual(self.mount(assistant_id, skill=[uuid4() for _ in range(31)]).json()["code"], "assistant.mount_limit")

    def test_a_stale_mount_does_not_block_saving_other_changes(self) -> None:
        assistant_id = self.create(self.admin).json()["id"]
        with self.session_factory.begin() as db:
            assistant = db.get(Assistant, UUID(assistant_id))
            assistant.draft_config = {**assistant.draft_config, "mounts": {"skill": [str(self.skill_off)]}}
        current = self.client.get(f"/api/v1/assistants/{assistant_id}", headers=self.admin).json()
        body = {"name": "Renamed", "description": "d", "config": current["config"], "row_version": current["row_version"]}
        self.assertEqual(self.client.put(f"/api/v1/assistants/{assistant_id}", json=body, headers=self.admin).status_code, 200)

    def test_per_mount_settings_only_for_mounted_resources(self) -> None:
        assistant_id = self.create(self.admin).json()["id"]
        self.mount(assistant_id, skill=[self.skill_ok])

        def save(settings: dict):
            current = self.client.get(f"/api/v1/assistants/{assistant_id}", headers=self.admin).json()
            body = {"name": "Helper", "description": "d", "config": {**current["config"], "mount_settings": settings}, "row_version": current["row_version"]}
            return self.client.put(f"/api/v1/assistants/{assistant_id}", json=body, headers=self.admin)

        ok = save({str(self.skill_ok): {"tools": ["translate", "detect"], "delegate": "When the user writes in another language"}})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["config"]["mount_settings"][str(self.skill_ok)]["tools"], ["translate", "detect"])
        rejected = save({str(uuid4()): {"tools": []}})
        self.assertEqual((rejected.status_code, rejected.json()["code"]), (422, "assistant.mount_settings_invalid"))
        self.assertEqual(save({str(self.skill_ok): {"delegate": "x" * 501}}).status_code, 422)
        self.assertEqual(save({str(self.skill_ok): {"tools": ["t"] * 101}}).status_code, 422)

    # -- the picker's endpoints -------------------------------------------------------

    def test_types_list_every_kind_with_mountability_and_badges(self) -> None:
        items = self.client.get("/api/v1/capabilities/types", headers=self.admin).json()["items"]
        self.assertEqual([i["kind"] for i in items], ["kb", "agent", "mcp", "skill", "chatflow", "flow", "api"])
        by_kind = {i["kind"]: i for i in items}
        self.assertEqual((by_kind["skill"]["mountable"], by_kind["skill"]["available"]), (True, 1))
        self.assertEqual((by_kind["kb"]["mountable"], by_kind["mcp"]["mountable"], by_kind["mcp"]["available"]), (True, False, 0))
        self.assertEqual(self.client.get("/api/v1/capabilities/types", headers=self.login("viewer@example.com")).status_code, 403)

    def test_candidates_of_a_registered_type_support_search_and_hide_other_tenants(self) -> None:
        listed = self.client.get("/api/v1/capabilities/candidates?kind=skill", headers=self.admin).json()["items"]
        self.assertEqual([(i["name"], i["meta"]) for i in listed], [("Translate", {"origin": "built-in"})])
        self.assertEqual(self.client.get("/api/v1/capabilities/candidates?kind=skill&q=zzz", headers=self.admin).json()["items"], [])
        self.assertEqual(self.client.get("/api/v1/capabilities/candidates?kind=mcp", headers=self.admin).json()["code"], "capability.kind_unavailable")
        self.assertEqual(self.client.get("/api/v1/capabilities/candidates?kind=bogus", headers=self.admin).status_code, 422)
        stranger = self.login("other@example.com", self.ids["other"])
        self.assertEqual(self.client.get("/api/v1/capabilities/candidates?kind=skill", headers=stranger).json()["items"], [])

    # -- dependencies tab ---------------------------------------------------------------

    def test_dependencies_list_channels_flows_and_registered_providers(self) -> None:
        from beework.api import assistant_checks
        from beework.schemas.assistant_checks import DependencyEntry

        assistant_id = self.create(self.admin).json()["id"]
        with self.session_factory.begin() as db:
            db.add(ChannelInstance(tenant_id=UUID(self.ids["tenant"]), name="Web", assistant_id=UUID(assistant_id), site_key="k" * 20, status="active"))
        self.flow("Caller", assistant_id, "assistant")
        provider = lambda db, assistant: [DependencyEntry(kind="portal_app", id=UUID(int=5), name="Portal app", status="listed")]  # noqa: E731
        assistant_checks.DEPENDENCY_PROVIDERS.append(provider)
        self.addCleanup(assistant_checks.DEPENDENCY_PROVIDERS.remove, provider)
        items = self.client.get(f"/api/v1/assistants/{assistant_id}/dependencies", headers=self.admin).json()["items"]
        self.assertEqual([(i["kind"], i["name"], i["status"]) for i in items], [("channel", "Web", "active"), ("flow", "Caller", "draft"), ("portal_app", "Portal app", "listed")])
        self.assertEqual(items[1]["detail"], "workflow")

    def test_dependencies_respect_permissions_and_tenant_isolation(self) -> None:
        assistant_id = self.create(self.admin).json()["id"]
        self.assertEqual(self.client.get(f"/api/v1/assistants/{assistant_id}/dependencies", headers=self.login("viewer@example.com")).status_code, 200)
        stranger = self.login("other@example.com", self.ids["other"])
        self.assertEqual(self.client.get(f"/api/v1/assistants/{assistant_id}/dependencies", headers=stranger).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/assistants/{uuid4()}/dependencies", headers=self.login("admin@example.com")).status_code, 404)


for _name in [n for n in dir(test_assistants.AssistantTests) if n.startswith("test_")]:
    setattr(CatalogTests, _name, None)  # inherited fixtures only; the assistant tests run in their own module


if __name__ == "__main__":
    unittest.main()
