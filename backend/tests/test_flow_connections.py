"""T-075: Chatflow / Workflow connections — the endpoints, the Dify client, state and references, and the executor provider."""

import json
import unittest
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select

import test_assistants
from beework import flow_connection_service as svc
from beework.capabilities import CANDIDATE_LISTERS, MOUNT_RESOLVERS
from beework.models import AuditEvent, Membership, User
from beework.models.flow_connections import FlowConnection
from beework.models.tools import ToolInvocation
from beework.security import hash_password
from beework.tools.executor import PROVIDERS, ToolContext, invoke_tool

PUBLIC = "93.184.216.34"
BASE = "https://api.dify.example.com/v1"
KEY = "app-SENTINEL-1234567890"


class FakeDify:
    """A Dify server on a mock transport: ``/chat-messages`` and ``/workflows/run``, a bearer check, scripted failures."""

    def __init__(self) -> None:
        self.key = KEY
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict] = []
        self.fail: int | str | None = None  # an HTTP status, or "down" / "garbage"
        self.workflow_status = "succeeded"
        self.outputs: dict = {"text": "refund approved", "score": 3}
        self.echo_key_in_error = False
        self.mode: str | None = None  # Dify's /info ``mode``
        self.mode_forced: str | None = None  # a test that wants a mismatch sets this (and ``mode``)
        self.form: list = []  # /parameters ``user_input_form``

    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail == "down":
            raise httpx.ConnectError("refused", request=request)
        if request.headers.get("authorization") != f"Bearer {self.key}":
            return httpx.Response(401, json={"code": "unauthorized", "message": "Access token is invalid"})
        if request.method == "GET":
            if isinstance(self.fail, int):
                return httpx.Response(self.fail, json={"message": "upstream problem"})
            if request.url.path.endswith("/info"):
                return httpx.Response(200, json={"name": "app", "mode": self.mode or "advanced-chat"})
            if request.url.path.endswith("/parameters"):
                return httpx.Response(200, json={"user_input_form": self.form})
            return httpx.Response(404, json={"message": "not found"})
        body = json.loads(request.content)
        self.bodies.append(body)
        if isinstance(self.fail, int):
            message = f"boom {self.key}" if self.echo_key_in_error else "upstream problem"
            return httpx.Response(self.fail, json={"message": message})
        if self.fail == "garbage":
            return httpx.Response(200, text="<html>nope</html>", headers={"content-type": "text/html"})
        if request.url.path.endswith("/chat-messages"):
            return httpx.Response(200, json={"answer": f"echo: {body['query']}", "conversation_id": "c1"})
        if request.url.path.endswith("/workflows/run"):
            data = {"status": self.workflow_status, "outputs": self.outputs, "error": "node failed" if self.workflow_status != "succeeded" else None}
            return httpx.Response(200, json={"workflow_run_id": "w1", "data": data})
        return httpx.Response(404, json={"message": "not found"})


class FlowConnectionTests(test_assistants.AssistantTests):
    def setUp(self) -> None:
        super().setUp()
        with self.session_factory.begin() as db:
            account = User(email="operator@example.com", display_name="Operator", password_hash=hash_password(test_assistants.PASSWORD))
            db.add(account)
            db.flush()
            db.add(Membership(tenant_id=UUID(self.ids["tenant"]), user_id=account.id, organization_id=UUID(self.ids["root"]), role="operator"))
        self.admin = self.login("admin@example.com")
        self.server = FakeDify()
        svc.TRANSPORT, svc.RESOLVER = httpx.MockTransport(self.server), lambda host: [PUBLIC]
        self.addCleanup(lambda: (setattr(svc, "TRANSPORT", None), setattr(svc, "RESOLVER", None)))

    # -- helpers ------------------------------------------------------------------

    def dify_body(self, kind: str = "chatflow", name: str = "Dify refund", **extra) -> dict:
        return {
            "kind": kind, "name": name, "description": "External", "source_type": "dify", "base_url": BASE, "app_id": "app-1",
            "credential": KEY, **extra,
        }

    def connect(self, body: dict, headers=None):
        if self.server.mode_forced is None:  # the fake Dify application is whatever kind the connection says (unless a test forces one)
            self.server.mode = "workflow" if body.get("kind") == "workflow" else "advanced-chat"
        return self.client.post("/api/v1/flow-connections", json=body, headers=headers or self.admin)

    def dify(self, kind: str = "chatflow", name: str = "Dify refund", **extra) -> dict:
        response = self.connect(self.dify_body(kind, name, **extra))
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def conn(self, connection_id: str, headers=None) -> dict:
        response = self.client.get(f"/api/v1/flow-connections/{connection_id}", headers=headers or self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def update(self, connection_id: str, headers=None, **changes):
        current = self.conn(connection_id)
        body = {
            "name": current["name"], "description": current["description"], "source_type": current["source_type"],
            "base_url": current["base_url"], "app_id": current["app_id"],
            "input_mapping": current["input_mapping"], "output_mapping": current["output_mapping"],
            "streaming": current["streaming"], "row_version": current["row_version"], **changes,
        }
        return self.client.put(f"/api/v1/flow-connections/{connection_id}", json=body, headers=headers or self.admin)

    def act(self, connection_id: str, action: str, headers=None):
        return self.client.post(f"/api/v1/flow-connections/{connection_id}/{action}", headers=headers or self.admin)

    def flow_audits(self, action: str) -> list[AuditEvent]:
        with self.session_factory() as db:
            return list(db.scalars(select(AuditEvent).where(AuditEvent.action == action)))

    def agent_with(self, kind: str, connection_id: str) -> str:
        agent_id = self.create(self.admin, f"Agent {uuid4().hex[:6]}").json()["id"]
        current = self.client.get(f"/api/v1/assistants/{agent_id}", headers=self.admin).json()
        config = {**current["config"], "mounts": {**current["config"]["mounts"], kind: [connection_id]}}
        body = {
            "name": current["name"], "description": current["description"], "category": current["category"],
            "visibility": current["visibility"], "visible_org_ids": current["visible_org_ids"], "row_version": current["row_version"],
            "config": config,
        }
        self.assertEqual(self.client.put(f"/api/v1/assistants/{agent_id}", json=body, headers=self.admin).status_code, 200)
        return agent_id

    def tool_ctx(self, *names: str, tenant: str | None = None) -> ToolContext:
        return ToolContext(tenant_id=UUID(tenant or self.ids["tenant"]), allowed=frozenset(names), run_id=uuid4())

    # -- the retired canvas source -----------------------------------------------------------

    def test_a_native_connection_can_no_longer_be_created_or_switched_to(self) -> None:
        refused = self.connect({"kind": "chatflow", "name": "Native", "source_type": "native"})
        self.assertEqual((refused.status_code, refused.json()["code"]), (422, "flow_connection.source_retired"))
        data = self.dify()
        switched = self.update(data["id"], source_type="native")
        self.assertEqual((switched.status_code, switched.json()["code"]), (422, "flow_connection.source_retired"))
        self.assertEqual(self.conn(data["id"])["source_type"], "dify")
        self.assertIn(self.client.get("/api/v1/flow-connections/available-flows?kind=chatflow", headers=self.admin).status_code, (404, 405, 422))  # the endpoint is gone
        self.assertNotIn("flow_id", self.conn(data["id"]))

    def test_a_leftover_native_connection_is_invalid_not_callable_and_never_a_candidate(self) -> None:
        legacy = self.dify("chatflow", "Was native")["id"]
        agent_id = self.agent_with("chatflow", legacy)  # mounted before the canvas was retired
        with self.session_factory.begin() as db:
            row = db.get(FlowConnection, UUID(legacy))
            row.source_type, row.base_url, row.app_id, row.credential_encrypted, row.credential_last4 = "native", "", "", None, None
        shown = self.conn(legacy)
        self.assertEqual((shown["source_type"], shown["state"], shown["state_reason"]), ("native", "invalid", "本平台画布已取消，请改用 Dify 应用"))
        self.assertEqual(self.client.get("/api/v1/capabilities/candidates?kind=chatflow", headers=self.admin).json()["items"], [])
        result = self.act(legacy, "test").json()
        self.assertEqual((result["ok"], result["error_category"]), (False, "invalid"))
        self.assertEqual(self.server.posts(), [])
        name = svc.tool_name(UUID(legacy))
        with self.session_factory() as db:
            self.assertEqual(invoke_tool(db, self.tool_ctx(name), name, {"query": "x"}).code, "tool.not_found")
        codes = [i["code"] for i in self.client.get(f"/api/v1/assistants/{agent_id}/checks", headers=self.admin).json()["issues"]]
        self.assertIn("mount.invalid", codes)  # the agent that mounted it sees the dangling reference — never silently ignored
        moved = self.update(legacy, source_type="dify", base_url=BASE, app_id="a", credential=KEY)  # it can be converted to a Dify connection
        self.assertEqual((moved.status_code, moved.json()["source_type"], moved.json()["state"]), (200, "dify", "available"))

    def test_names_are_unique_per_kind_and_trimmed_and_limited(self) -> None:
        self.dify("chatflow", "Same name")
        again = self.connect(self.dify_body("chatflow", " same NAME "))
        self.assertEqual((again.status_code, again.json()["code"]), (409, "flow_connection.name_taken"))
        self.assertEqual(self.dify("workflow", "Same name")["kind"], "workflow")  # another kind is fine
        self.assertEqual(self.connect(self.dify_body(name="  ")).status_code, 400)
        self.assertEqual(self.connect(self.dify_body(name="x" * 51)).status_code, 400)
        self.assertEqual(self.connect(self.dify_body(description="d" * 501, name="Long")).status_code, 400)
        self.assertEqual(self.connect({**self.dify_body(name="No kind"), "kind": None}).status_code, 422)

    def test_a_dify_connection_is_created_available(self) -> None:
        data = self.dify()
        self.assertEqual((data["kind"], data["source_type"], data["status"], data["state"], data["credential_mask"]), ("chatflow", "dify", "enabled", "available", "****7890"))
        self.assertEqual(len(self.flow_audits("flow_connection.created")), 1)

    # -- Dify connections --------------------------------------------------------------

    def test_a_dify_connection_needs_base_url_app_id_and_a_key(self) -> None:
        for missing in ("base_url", "app_id", "credential"):
            body = self.dify_body(name=f"No {missing}")
            body[missing] = ""
            refused = self.connect(body)
            self.assertEqual((refused.status_code, refused.json()["code"]), (422, "validation.invalid"), missing)
        self.assertEqual(self.connect({**self.dify_body(), "credential": None}).status_code, 422)

    def test_private_and_non_https_addresses_are_refused(self) -> None:
        for url in ("https://10.0.0.5/v1", "https://127.0.0.1/v1", "https://169.254.169.254/v1", "http://api.dify.example.com/v1", "ftp://x.example.com"):
            refused = self.connect(self.dify_body(name="Bad", base_url=url))
            self.assertEqual((refused.status_code, refused.json()["code"]), (400, "security.invalid_url"), url)
        self.assertEqual(self.dify_body()["base_url"], BASE)

    def test_the_key_is_never_returned_logged_or_audited(self) -> None:
        data = self.dify()
        self.assertEqual((data["credential_mask"], data["host"], data["app_id"]), ("****7890", "api.dify.example.com", "app-1"))
        everything = json.dumps(self.conn(data["id"])) + json.dumps(self.client.get("/api/v1/flow-connections", headers=self.admin).json())
        everything += json.dumps([a.details for a in self.flow_audits("flow_connection.created")], default=str)
        self.assertNotIn(KEY, everything)
        with self.session_factory() as db:
            row = db.get(FlowConnection, UUID(data["id"]))
            self.assertNotIn(KEY, row.credential_encrypted or "")  # encrypted at rest
            self.assertTrue(row.credential_encrypted)

    def test_a_blank_key_on_update_keeps_the_stored_one_and_a_new_one_replaces_it(self) -> None:
        data = self.dify()
        self.assertEqual(self.update(data["id"], credential="", description="edited").status_code, 200)
        self.assertEqual(self.act(data["id"], "test").json()["ok"], True)  # still the old key
        replaced = self.update(data["id"], credential="app-NEWKEY-0000000000")
        self.assertEqual((replaced.status_code, replaced.json()["credential_mask"]), (200, "****0000"))
        self.assertEqual(self.act(data["id"], "test").json()["ok"], False)  # the fake server only knows the old key

    def test_the_mappings_are_validated(self) -> None:
        for bad in ({"1bad": "q"}, {"ok": "not valid!"}, {"": "q"}):
            self.assertEqual(self.connect(self.dify_body(name="M", input_mapping=bad)).status_code, 422, bad)
        good = self.connect(self.dify_body("workflow", "Mapped", input_mapping={"order_no": "order", "note": "note"}, output_mapping={"field": "text"}))
        self.assertEqual((good.status_code, good.json()["input_mapping"], good.json()["output_mapping"]), (201, {"order_no": "order", "note": "note"}, {"field": "text"}))

    def test_testing_a_chatflow_calls_chat_messages_once_and_records_the_result(self) -> None:
        data = self.dify("chatflow")
        result = self.act(data["id"], "test").json()
        self.assertEqual((result["ok"], result["error"], result["error_category"]), (True, None, None))
        self.assertEqual([r.url.path for r in self.server.posts()], ["/v1/chat-messages"])
        sent = self.server.posts()[0]
        self.assertEqual((sent.method, sent.headers["authorization"], sent.headers["host"]), ("POST", f"Bearer {KEY}", "api.dify.example.com"))
        self.assertEqual((self.server.bodies[0]["response_mode"], bool(self.server.bodies[0]["query"])), ("blocking", True))
        recorded = self.conn(data["id"])
        self.assertEqual((recorded["last_test_ok"], recorded["last_test_error"]), (True, None))
        self.assertIsNotNone(recorded["last_test_at"])
        self.assertIsNotNone(recorded["last_test_ms"])
        self.assertEqual(len(self.flow_audits("flow_connection.tested")), 1)

    def test_testing_a_workflow_calls_workflows_run(self) -> None:
        data = self.dify("workflow", "WF")
        self.assertTrue(self.act(data["id"], "test").json()["ok"])
        self.assertEqual([r.url.path for r in self.server.posts()], ["/v1/workflows/run"])

    def test_dify_failures_are_mapped_to_a_reason_and_a_category(self) -> None:
        data = self.dify()
        cases = [
            (401, "Dify 拒绝了凭据", "auth_failed"), (403, "Dify 拒绝了凭据", "auth_failed"),
            (404, "找不到该应用", "not_found"), (429, "限流", "rate_limited"), (400, "Dify 拒绝了输入", "invalid_arguments"),
            (500, "Dify 返回 500", "upstream_error"), ("down", "无法连接", "network"), ("garbage", "不是有效的 JSON", "bad_response"),
        ]
        for fail, expected, category in cases:
            self.server.fail = fail
            result = self.act(data["id"], "test").json()
            self.assertFalse(result["ok"], fail)
            self.assertTrue(expected in result["error"] or expected == result["error_category"], (fail, result["error"]))
            self.assertEqual(result["error_category"], category, fail)
            self.assertEqual(self.conn(data["id"])["last_test_ok"], False)
        self.server.fail = None
        self.assertTrue(self.act(data["id"], "test").json()["ok"])  # the retry entry point

    def test_a_failed_workflow_run_is_an_error(self) -> None:
        data = self.dify("workflow", "WF")
        self.server.workflow_status = "failed"
        result = self.act(data["id"], "test").json()
        self.assertEqual((result["ok"], result["error_category"]), (False, "workflow_failed"))
        self.assertIn("node failed", result["error"])

    def test_the_key_goes_only_to_the_configured_host_and_a_redirect_is_not_followed(self) -> None:
        data = self.dify()
        self.assertTrue(self.act(data["id"], "test").json()["ok"])
        self.assertEqual({r.url.host for r in self.server.posts()}, {PUBLIC})  # pinned to the validated address
        self.server.fail = 302
        refused = self.act(data["id"], "test").json()
        self.assertEqual((refused["ok"], refused["error_category"]), (False, "config"))

    def test_pointing_a_dify_connection_at_another_host_needs_the_key_again(self) -> None:
        data = self.dify()
        refused = self.update(data["id"], base_url="https://attacker.example.com/v1")
        self.assertEqual((refused.status_code, refused.json()["code"]), (422, "flow_connection.credential_reentry"))
        self.assertEqual(self.conn(data["id"])["host"], "api.dify.example.com")  # nothing was saved
        self.assertEqual(self.update(data["id"], base_url="https://api.dify.example.com/v2").status_code, 200)  # same host: fine
        moved = self.update(data["id"], base_url="https://dify.other.example.com/v1", credential="app-MOVED-0000000000")
        self.assertEqual((moved.status_code, moved.json()["credential_mask"]), (200, "****0000"))

    # -- lifecycle ----------------------------------------------------------------------

    def test_update_checks_the_row_version_and_cannot_change_the_kind(self) -> None:
        data = self.dify()
        self.assertEqual(self.update(data["id"], description="new").json()["row_version"], data["row_version"] + 1)
        stale = self.client.put(
            f"/api/v1/flow-connections/{data['id']}",
            json={**self.dify_body(name="x"), "row_version": data["row_version"]}, headers=self.admin,
        )
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "conflict.stale"))
        self.assertEqual(self.update(data["id"], kind="workflow").json()["code"], "flow_connection.kind_immutable")
        self.assertEqual(self.client.put(f"/api/v1/flow-connections/{data['id']}", json={"name": "x", "source_type": "dify"}, headers=self.admin).status_code, 422)  # no row_version
        self.assertEqual(len(self.flow_audits("flow_connection.updated")), 1)

    def test_disable_and_enable_change_the_state_and_what_can_be_mounted(self) -> None:
        data = self.dify()
        candidates = lambda: self.client.get("/api/v1/capabilities/candidates?kind=chatflow", headers=self.admin).json()["items"]  # noqa: E731
        self.assertEqual([c["id"] for c in candidates()], [data["id"]])
        self.assertEqual(self.act(data["id"], "disable").json()["state"], "disabled")
        self.assertEqual(candidates(), [])  # RUL-010: only what can be called now
        self.assertEqual(self.act(data["id"], "enable").json()["state"], "available")
        self.assertEqual([c["id"] for c in candidates()], [data["id"]])
        self.assertEqual((len(self.flow_audits("flow_connection.disabled")), len(self.flow_audits("flow_connection.enabled"))), (1, 1))

    def test_delete_is_soft_and_leaves_the_referencing_agent_with_a_dangling_reference(self) -> None:
        data = self.dify()
        agent_id = self.agent_with("chatflow", data["id"])
        self.assertEqual(self.conn(data["id"])["references"], 1)
        refs = self.client.get(f"/api/v1/flow-connections/{data['id']}/references", headers=self.admin).json()
        self.assertEqual((refs["total"], refs["items"][0]["id"]), (1, agent_id))
        self.assertEqual(self.client.delete(f"/api/v1/flow-connections/{data['id']}", headers=self.admin).status_code, 204)
        self.assertEqual(self.client.get(f"/api/v1/flow-connections/{data['id']}", headers=self.admin).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/flow-connections", headers=self.admin).json()["total"], 0)
        codes = [i["code"] for i in self.client.get(f"/api/v1/assistants/{agent_id}/checks", headers=self.admin).json()["issues"]]
        self.assertIn("mount.invalid", codes)
        with self.session_factory() as db:
            self.assertIsNone(db.get(FlowConnection, UUID(data["id"])).credential_encrypted)  # no key left to recover
        self.assertEqual(len(self.flow_audits("flow_connection.deleted")), 1)
        self.assertEqual(self.connect(self.dify_body(name="Dify refund")).status_code, 201)  # the name is free again

    def test_list_filters_by_kind_and_search(self) -> None:
        self.dify("chatflow", "Alpha")
        self.dify("workflow", "Beta")
        listed = lambda query: [c["name"] for c in self.client.get(f"/api/v1/flow-connections{query}", headers=self.admin).json()["items"]]  # noqa: E731
        self.assertEqual(listed(""), ["Alpha", "Beta"])
        self.assertEqual(listed("?kind=workflow"), ["Beta"])
        self.assertEqual(listed("?q=alp"), ["Alpha"])

    # -- permissions and isolation --------------------------------------------------------

    def test_roles_without_permission_cannot_change_connections(self) -> None:
        data = self.dify()
        for email in ("viewer@example.com", "operator@example.com"):
            headers = self.login(email)
            self.assertEqual(self.client.get("/api/v1/flow-connections", headers=headers).status_code, 200)
            self.assertEqual(self.client.get(f"/api/v1/flow-connections/{data['id']}", headers=headers).status_code, 200)
            self.assertEqual(self.connect(self.dify_body(name="Nope"), headers=headers).status_code, 403)
            self.assertEqual(self.update(data["id"], headers=headers, name="x").status_code, 403)
            for action in ("test", "enable", "disable"):
                self.assertEqual(self.act(data["id"], action, headers=headers).status_code, 403, action)
            self.assertEqual(self.client.delete(f"/api/v1/flow-connections/{data['id']}", headers=headers).status_code, 403)
        view = self.conn(data["id"], headers=self.login("viewer@example.com"))
        self.assertEqual((view["can_edit"], view["can_delete"]), (False, False))

    def test_other_tenants_cannot_see_or_change_a_connection(self) -> None:
        data = self.dify()
        stranger = self.login("other@example.com", self.ids["other"])
        self.assertEqual(self.client.get("/api/v1/flow-connections", headers=stranger).json()["items"], [])
        for method, suffix in (("get", ""), ("get", "/references"), ("post", "/test"), ("post", "/enable"), ("post", "/disable"), ("delete", "")):
            response = getattr(self.client, method)(f"/api/v1/flow-connections/{data['id']}{suffix}", headers=stranger)
            self.assertEqual(response.status_code, 404, f"{method} {suffix}")
        put = self.client.put(
            f"/api/v1/flow-connections/{data['id']}", json={**self.dify_body(name="x"), "row_version": 1}, headers=stranger,
        )
        self.assertEqual(put.status_code, 404)
        self.admin = self.login("admin@example.com")  # logging in as the stranger replaced the shared session
        self.assertEqual(self.conn(data["id"])["name"], data["name"])

    def test_writes_require_the_csrf_token(self) -> None:
        response = self.client.post("/api/v1/flow-connections", json=self.dify_body(), headers={"X-Tenant-ID": self.ids["tenant"]})
        self.assertEqual(response.status_code, 403)

    def test_a_dify_connection_needs_the_plan_feature(self) -> None:
        from beework import entitlements

        original = entitlements.has_entitlement
        entitlements.has_entitlement = lambda db, tenant_id, key: False if key == "external_flows" else original(db, tenant_id, key)
        self.addCleanup(lambda: setattr(entitlements, "has_entitlement", original))
        refused = self.connect(self.dify_body(name="Locked"))
        self.assertEqual((refused.status_code, refused.json()["code"]), (403, "plan.feature_locked"))

    # -- the capability catalogue ---------------------------------------------------------------

    def test_both_kinds_register_a_resolver_and_a_lister(self) -> None:
        for kind in ("chatflow", "flow"):
            self.assertIn(kind, MOUNT_RESOLVERS)
            self.assertIn(kind, CANDIDATE_LISTERS)
        types = {t["kind"]: t for t in self.client.get("/api/v1/capabilities/types", headers=self.admin).json()["items"]}
        self.assertTrue(types["chatflow"]["mountable"] and types["flow"]["mountable"])

    def test_workflows_mount_under_flow_and_chatflows_under_chatflow(self) -> None:
        chat = self.dify("chatflow", "Chat")
        workflow = self.dify("workflow", "Work")
        wrong = self.agent_with("chatflow", chat["id"])
        mounts = self.client.get(f"/api/v1/assistants/{wrong}/mounts", headers=self.admin).json()
        self.assertEqual([(g["kind"], g["items"][0]["state"]) for g in mounts["groups"]], [("chatflow", "available")])
        self.assertEqual(self.client.get("/api/v1/capabilities/candidates?kind=flow", headers=self.admin).json()["items"][0]["id"], workflow["id"])
        self.assertEqual(self.client.get("/api/v1/capabilities/candidates?kind=chatflow", headers=self.admin).json()["items"][0]["id"], chat["id"])

    # -- the runtime: the executor's provider -----------------------------------------------------

    def test_a_chatflow_connection_is_called_as_a_tool_with_the_query(self) -> None:
        data = self.dify("chatflow", "Chat")
        name = svc.tool_name(UUID(data["id"]))
        with self.session_factory() as db:
            ctx = self.tool_ctx(name)
            result = invoke_tool(db, ctx, name, {"query": "hello"})
        self.assertEqual((result.status, result.content), ("ok", "echo: hello"))
        self.assertEqual((self.server.bodies[0]["query"], self.server.bodies[0]["inputs"]), ("hello", {}))
        with self.session_factory() as db:
            record = db.scalar(select(ToolInvocation).order_by(ToolInvocation.created_at.desc()))
            self.assertEqual((record.source, record.status), ("connection", "ok"))

    def test_a_workflow_connection_maps_inputs_and_returns_the_chosen_output(self) -> None:
        data = self.dify("workflow", "Flow", input_mapping={"order_no": "order"}, output_mapping={"field": "text"})
        name = svc.tool_name(UUID(data["id"]))
        spec = PROVIDERS["connection"].lookup(self.session_factory(), self.tool_ctx(name), name)
        self.assertEqual(spec.parameters["properties"], {"order": {"type": "string"}})
        with self.session_factory() as db:
            result = invoke_tool(db, self.tool_ctx(name), name, {"order": "A-1"})
        self.assertEqual((result.status, result.content), ("ok", "refund approved"))
        self.assertEqual(self.server.bodies[0]["inputs"], {"order_no": "A-1"})
        self.server.outputs = {"text": "x", "score": 3}
        unmapped = self.update(data["id"], output_mapping={})
        self.assertEqual(unmapped.status_code, 200)
        with self.session_factory() as db:
            result = invoke_tool(db, self.tool_ctx(name), name, {"order": "A-2"})
        self.assertEqual(json.loads(result.content), {"text": "x", "score": 3})

    def test_a_tool_not_ticked_disabled_deleted_or_of_another_tenant_is_not_callable(self) -> None:
        data = self.dify("chatflow", "Chat")
        name = svc.tool_name(UUID(data["id"]))
        with self.session_factory() as db:
            self.assertEqual(invoke_tool(db, self.tool_ctx(), name, {"query": "x"}).code, "tool.not_mounted")
            self.assertEqual(invoke_tool(db, self.tool_ctx(name, tenant=self.ids["other"]), name, {"query": "x"}).code, "tool.not_found")
            self.assertEqual(invoke_tool(db, self.tool_ctx(name), name, {}).code, "tool.invalid_arguments")  # query is required
        self.act(data["id"], "disable")
        with self.session_factory() as db:
            self.assertEqual(invoke_tool(db, self.tool_ctx(name), name, {"query": "x"}).code, "tool.not_found")
        self.assertEqual(self.server.posts(), [])  # nothing reached Dify

    def test_a_failing_dify_call_is_an_error_result_and_never_shows_the_key(self) -> None:
        data = self.dify("chatflow", "Chat")
        name = svc.tool_name(UUID(data["id"]))
        self.server.fail, self.server.echo_key_in_error = 500, True
        with self.session_factory() as db:
            result = invoke_tool(db, self.tool_ctx(name), name, {"query": "x"})
        self.assertEqual((result.status, result.code), ("error", "tool.upstream_error"))
        self.assertNotIn(KEY, result.content)

    def test_the_agent_offers_a_mounted_connection_as_a_tool(self) -> None:
        from beework.runtime.dsl import AssistantDSLv1
        from beework.runtime.tool_runtime import ToolRuntime

        data = self.dify("chatflow", "Chat", description="Answers refund questions")
        agent_id = self.agent_with("chatflow", data["id"])
        with self.session_factory() as db:
            from beework.models.assistants import Assistant
            from beework.runtime.dsl import dsl_from_draft

            assistant = db.get(Assistant, UUID(agent_id))
            dsl: AssistantDSLv1 = dsl_from_draft(assistant, db)
            runtime = ToolRuntime(db.get_bind(), dsl, tenant_id=assistant.tenant_id, run_id=None, assistant_id=assistant.id)
        self.assertEqual([t.spec.name for t in runtime.offered], [svc.tool_name(UUID(data["id"]))])
        self.assertEqual(runtime.schemas()[0]["function"]["parameters"]["required"], ["query"])


if __name__ == "__main__":
    unittest.main()
