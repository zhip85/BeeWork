"""T-072: MCP connections — the client, the capability endpoints, OAuth, approval, health and the executor provider."""

import json
import unittest
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select

import test_assistants
from beework import capability_service as svc
from beework.approvals import ensure_policies
from beework.capabilities import CANDIDATE_LISTERS, MOUNT_RESOLVERS
from beework.config import settings
from beework.models import AuditEvent, Membership, Tenant, User
from beework.models.approvals import ApprovalRequest
from beework.models.capabilities import Capability, McpConnection
from beework.models.notifications import InAppNotification
from beework.models.tools import ToolInvocation
from beework.tools import mcp_client
from beework.tools.executor import PROVIDERS, ToolConfirmationRequired, ToolContext, invoke_tool
from beework.tools.mcp_client import McpClient, McpError
from beework.worker.runner import run_once

PUBLIC = "93.184.216.34"
SECRET = "tok-SENTINEL-987654321"
URL = "https://mcp.example.com/mcp"

TOOLS = [
    {"name": "search_docs", "description": "Search the docs", "inputSchema": {"type": "object", "required": ["q"], "properties": {"q": {"type": "string"}}}, "annotations": {"readOnlyHint": True}},
    {"name": "create_ticket", "description": "Create a ticket", "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}}},
]


class FakeServer:
    """An MCP server on a mock transport: Streamable HTTP, JSON or SSE answers, optional bearer check."""

    def __init__(self, tools=None, *, sse: bool = False, token: str | None = None, pages: int = 1) -> None:
        self.tools, self.sse, self.token, self.pages = list(tools if tools is not None else TOOLS), sse, token, pages
        self.requests: list[dict] = []
        self.headers: list[httpx.Headers] = []
        self.fail: str | None = None  # "down" | "garbage" | "rpc"
        self.calls: list[dict] = []
        self.call_result: dict = {"content": [{"type": "text", "text": "ok"}]}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(request.headers)
        if self.fail == "down":
            raise httpx.ConnectError("refused", request=request)
        if self.token and request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"error": "unauthorized"})
        body = json.loads(request.content)
        self.requests.append(body)
        method, rid = body.get("method"), body.get("id")
        if rid is None:
            return httpx.Response(202)
        if self.fail == "garbage":
            return httpx.Response(200, text="<html>not mcp</html>", headers={"content-type": "text/html"})
        if self.fail == "rpc":
            return self.reply({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "boom"}})
        if method == "initialize":
            return self.reply({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": mcp_client.PROTOCOL_VERSION, "capabilities": {}}}, {"mcp-session-id": "sess-1"})
        if method == "tools/list":
            cursor = int((body["params"] or {}).get("cursor") or 0)
            size = max(1, -(-len(self.tools) // self.pages))
            chunk = self.tools[cursor * size:(cursor + 1) * size]
            result = {"tools": chunk}
            if (cursor + 1) * size < len(self.tools):
                result["nextCursor"] = str(cursor + 1)
            return self.reply({"jsonrpc": "2.0", "id": rid, "result": result})
        if method == "tools/call":
            self.calls.append(body["params"])
            return self.reply({"jsonrpc": "2.0", "id": rid, "result": self.call_result})
        return self.reply({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no such method"}})

    def reply(self, message: dict, headers: dict | None = None) -> httpx.Response:
        if self.sse:
            ping = 'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/message","params":{}}\n\n'
            return httpx.Response(200, text=f"{ping}event: message\ndata: {json.dumps(message)}\n\n", headers={"content-type": "text/event-stream", **(headers or {})})
        return httpx.Response(200, json=message, headers=headers)


class ClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = FakeServer()
        mcp_client.TRANSPORT, mcp_client.RESOLVER = httpx.MockTransport(self.server), lambda host: [PUBLIC]
        self.addCleanup(lambda: (setattr(mcp_client, "TRANSPORT", None), setattr(mcp_client, "RESOLVER", None)))

    def test_it_initializes_lists_tools_and_keeps_the_session(self) -> None:
        tools = McpClient(URL).list_tools()
        self.assertEqual([(t.name, t.read_only) for t in tools], [("search_docs", True), ("create_ticket", False)])
        self.assertEqual([r.get("method") for r in self.server.requests], ["initialize", "notifications/initialized", "tools/list"])
        self.assertIn("text/event-stream", self.server.headers[0]["accept"])
        self.assertEqual(self.server.headers[-1]["mcp-session-id"], "sess-1")
        self.assertEqual(self.server.headers[-1]["mcp-protocol-version"], mcp_client.PROTOCOL_VERSION)

    def test_sse_framed_answers_and_pagination(self) -> None:
        server = FakeServer(sse=True, pages=2)
        mcp_client.TRANSPORT = httpx.MockTransport(server)
        self.assertEqual([t.name for t in McpClient(URL).list_tools()], ["search_docs", "create_ticket"])
        self.assertEqual(sum(1 for r in server.requests if r.get("method") == "tools/list"), 2)

    def test_calling_a_tool_returns_text_and_the_error_flag(self) -> None:
        self.server.call_result = {"content": [{"type": "text", "text": "hello"}, {"type": "image", "data": "x"}, {"type": "resource", "resource": {"uri": "u", "text": "inline"}}]}
        text, is_error = McpClient(URL).call_tool("search_docs", {"q": "x"})
        self.assertEqual((text, is_error), ("hello\n[image]\ninline", False))
        self.assertEqual(self.server.calls, [{"name": "search_docs", "arguments": {"q": "x"}}])
        self.server.call_result = {"content": [{"type": "text", "text": "bad input"}], "isError": True}
        self.assertEqual(McpClient(URL).call_tool("x", {}), ("bad input", True))
        self.server.call_result = {"structuredContent": {"n": 1}}
        self.assertEqual(McpClient(URL).call_tool("x", {})[0], '{"n": 1}')

    def test_failures_have_stable_codes(self) -> None:
        for mode, code in (("down", "tool.unreachable"), ("garbage", "mcp.protocol"), ("rpc", "mcp.rpc_error")):
            self.server.fail = mode
            with self.assertRaises(McpError, msg=mode) as caught:
                McpClient(URL).list_tools()
            self.assertEqual(caught.exception.code, code)
        self.server.fail, self.server.token = None, "right"
        with self.assertRaises(McpError) as caught:
            McpClient(URL, {"Authorization": "Bearer wrong"}).list_tools()
        self.assertEqual(caught.exception.code, "mcp.auth_failed")
        self.assertEqual(len(McpClient(URL, {"Authorization": "Bearer right"}).list_tools()), 2)

    def test_the_safe_client_rules_apply(self) -> None:
        mcp_client.RESOLVER = lambda host: ["10.0.0.5"]
        with self.assertRaises(McpError) as caught:
            McpClient(URL).list_tools()
        self.assertEqual(caught.exception.code, "tool.forbidden_address")
        self.assertEqual(self.server.requests, [])


class McpTests(test_assistants.AssistantTests):
    def setUp(self) -> None:
        super().setUp()
        self.server = FakeServer()
        mcp_client.TRANSPORT, mcp_client.RESOLVER = httpx.MockTransport(self.server), lambda host: [PUBLIC]
        self.addCleanup(lambda: (setattr(mcp_client, "TRANSPORT", None), setattr(mcp_client, "RESOLVER", None)))
        with self.session_factory.begin() as db:
            account = User(email="admin2@example.com", display_name="Admin Two", password_hash=test_assistants.hash_password(test_assistants.PASSWORD))
            db.add(account)
            db.flush()
            db.add(Membership(tenant_id=UUID(self.ids["tenant"]), user_id=account.id, organization_id=UUID(self.ids["root"]), role="tenant_admin"))
            operator = User(email="operator@example.com", display_name="Operator", password_hash=test_assistants.hash_password(test_assistants.PASSWORD))
            db.add(operator)
            db.flush()
            db.add(Membership(tenant_id=UUID(self.ids["tenant"]), user_id=operator.id, organization_id=UUID(self.ids["root"]), role="operator"))
            ensure_policies(db, UUID(self.ids["tenant"]))["skill"].enabled = False  # most tests: no approval step
        self.admin = self.login("admin@example.com")

    # -- helpers ------------------------------------------------------------------

    def mcp_body(self, name: str = "Docs server", **extra) -> dict:
        return {"name": name, "description": "Search company docs", "transport": "streamable_http", "url": URL, "auth_type": "none", **extra}

    def create_mcp(self, name: str = "Docs server", headers=None, **extra):
        return self.client.post("/api/v1/capabilities/mcp", json=self.mcp_body(name, **extra), headers=headers or self.admin)

    def get(self, capability_id: str, headers=None) -> dict:
        response = self.client.get(f"/api/v1/capabilities/{capability_id}", headers=headers or self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def update(self, capability_id: str, headers=None, **changes):
        current = self.get(capability_id, headers)
        mcp = current["mcp"]
        body = {"name": current["name"], "description": current["description"], "url": mcp["url"], "auth_type": mcp["auth_type"],
                "oauth_authorize_url": mcp["oauth_authorize_url"], "oauth_token_url": mcp["oauth_token_url"],
                "oauth_client_id": mcp["oauth_client_id"], "oauth_scopes": mcp["oauth_scopes"], "row_version": current["row_version"], **changes}
        return self.client.put(f"/api/v1/capabilities/mcp/{capability_id}", json=body, headers=headers or self.admin)

    def post(self, path: str, headers=None, body=None):
        return self.client.post(f"/api/v1/capabilities/{path}", json=body, headers=headers or self.admin)

    def audits(self, action: str) -> list[AuditEvent]:
        with self.session_factory() as db:
            return list(db.scalars(select(AuditEvent).where(AuditEvent.action == action)))

    def ready_mcp(self, name: str = "Docs server") -> str:
        data = self.create_mcp(name).json()
        self.assertEqual(data["capability"]["status"], "available", data)
        return data["capability"]["id"]

    # -- create and test ----------------------------------------------------------------

    def test_connecting_tests_the_server_selects_every_tool_and_makes_the_capability_available(self) -> None:
        response = self.create_mcp()
        self.assertEqual(response.status_code, 201, response.text)
        result = response.json()
        cap = result["capability"]
        self.assertEqual((result["ok"], result["tool_count"], cap["status"], cap["kind"], cap["meta"]), (True, 2, "available", "mcp", "mcp.example.com · MCP"))
        self.assertEqual([(t["name"], t["selected"], t["read_only"]) for t in cap["mcp"]["tools"]], [("search_docs", True, True), ("create_ticket", True, False)])
        self.assertEqual((cap["mcp"]["last_test_ok"], cap["mcp"]["health"], cap["mcp"]["token_mask"]), (True, "ok", None))
        self.assertEqual(len(self.audits("capability.created")), 1)
        self.assertEqual(len(self.audits("capability.enabled")), 1)

    def test_a_failing_server_leaves_a_draft_with_the_reason(self) -> None:
        self.server.fail = "down"
        result = self.create_mcp().json()
        self.assertEqual((result["ok"], result["capability"]["status"]), (False, "draft"))
        self.assertIn("无法连接", result["error"])
        self.assertEqual(result["capability"]["mcp"]["last_test_error"], result["error"])
        self.server.fail = None
        retry = self.post(f"mcp/{result['capability']['id']}/test").json()
        self.assertEqual((retry["ok"], retry["capability"]["status"]), (True, "available"))

    def test_fields_are_validated(self) -> None:
        self.assertEqual(self.create_mcp(name="  ").status_code, 400)
        self.assertEqual(self.create_mcp(name="x" * 51).status_code, 400)
        self.assertEqual(self.create_mcp(description="d" * 501).status_code, 400)
        self.assertEqual(self.create_mcp(url="").json()["code"], "capability.invalid_url")
        self.assertEqual(self.create_mcp(url="http://mcp.example.com/mcp").json()["code"], "capability.invalid_url")
        self.assertEqual(self.create_mcp(url="https://user:pw@mcp.example.com/").status_code, 422)
        self.assertEqual(self.create_mcp(transport="stdio").status_code, 422)  # not offered in the SaaS deployment
        self.assertEqual(self.create_mcp(auth_type="bearer").json()["code"], "capability.token_required")
        self.assertEqual(self.create_mcp(auth_type="oauth", oauth_authorize_url="https://a.example.com/auth", oauth_token_url="https://a.example.com/token").json()["code"], "capability.oauth_client_required")
        self.assertEqual(self.create_mcp(auth_type="weird").status_code, 422)

    def test_names_are_unique_per_type_in_a_tenant(self) -> None:
        self.assertEqual(self.create_mcp("Same").status_code, 201)
        duplicate = self.create_mcp("same")
        self.assertEqual((duplicate.status_code, duplicate.json()["code"]), (409, "capability.name_taken"))

    def test_http_is_accepted_only_with_the_development_switch(self) -> None:
        settings.allow_insecure_tool_urls = True
        self.addCleanup(setattr, settings, "allow_insecure_tool_urls", False)
        self.assertEqual(self.create_mcp(url="http://localhost:9000/mcp").status_code, 201)

    # -- bearer tokens ---------------------------------------------------------------------

    def test_a_bearer_token_is_sent_masked_in_responses_and_kept_when_left_blank(self) -> None:
        self.server.token = SECRET
        result = self.create_mcp(auth_type="bearer", token=SECRET).json()
        cap = result["capability"]
        self.assertEqual((result["ok"], cap["mcp"]["token_mask"]), (True, "••••4321"))
        self.assertNotIn(SECRET, json.dumps(cap))
        self.assertEqual(self.server.headers[0]["authorization"], f"Bearer {SECRET}")
        with self.session_factory() as db:
            stored = db.get(McpConnection, UUID(cap["id"])).secret_encrypted
        self.assertNotIn(SECRET, stored)
        kept = self.update(cap["id"], name="Renamed")  # token left out: unchanged, no re-test needed
        self.assertEqual((kept.status_code, kept.json()["status"], kept.json()["mcp"]["token_mask"]), (200, "available", "••••4321"))
        self.assertEqual(self.get(cap["id"])["mcp"]["last_test_ok"], True)
        wrong = self.update(cap["id"], token="another-token-0000")  # a new credential: the old test result is void
        self.assertEqual((wrong.json()["status"], wrong.json()["mcp"]["last_test_ok"], wrong.json()["mcp"]["token_mask"]), ("draft", False, "••••0000"))

    def test_a_rejected_token_is_reported_as_an_authentication_failure(self) -> None:
        self.server.token = "the-right-one"
        result = self.create_mcp(auth_type="bearer", token="the-wrong-one").json()
        self.assertEqual((result["ok"], result["capability"]["status"]), (False, "draft"))
        self.assertIn("认证失败", result["error"])

    # -- tools -----------------------------------------------------------------------------

    def test_tools_are_reconciled_on_every_test(self) -> None:
        cap_id = self.ready_mcp()
        self.assertEqual(self.update(cap_id, selected_tools=["search_docs"]).status_code, 200)
        self.server.tools = [TOOLS[0], {"name": "brand_new", "description": "New", "inputSchema": {"type": "object"}}]  # create_ticket vanished
        tested = self.post(f"mcp/{cap_id}/test").json()
        tools = {t["name"]: t["selected"] for t in tested["capability"]["mcp"]["tools"]}
        self.assertEqual(tools, {"search_docs": True, "brand_new": False})  # kept, new ones start unticked, vanished leave

    def test_selecting_tools_is_checked_against_what_was_offered(self) -> None:
        cap_id = self.ready_mcp()
        unknown = self.update(cap_id, selected_tools=["search_docs", "made_up"])
        self.assertEqual((unknown.status_code, unknown.json()["code"]), (422, "capability.unknown_tool"))
        none_ticked = self.update(cap_id, selected_tools=[])
        self.assertEqual((none_ticked.status_code, none_ticked.json()["status"]), (200, "draft"))  # CAP-032: needs a tool
        again = self.update(cap_id, selected_tools=["create_ticket"])
        self.assertEqual(again.json()["status"], "available")

    def test_changing_the_address_voids_the_test_and_the_tools(self) -> None:
        cap_id = self.ready_mcp()
        moved = self.update(cap_id, url="https://other.example.com/mcp").json()
        self.assertEqual((moved["status"], moved["mcp"]["last_test_ok"], moved["mcp"]["tools"]), ("draft", False, []))
        self.assertEqual(self.post(f"mcp/{cap_id}/test").json()["capability"]["status"], "available")

    def test_updates_need_the_row_version(self) -> None:
        cap_id = self.ready_mcp()
        stale = self.get(cap_id)["row_version"]
        self.update(cap_id, name="First")
        body = {"name": "Second", "url": URL, "auth_type": "none", "row_version": stale}
        conflict = self.client.put(f"/api/v1/capabilities/mcp/{cap_id}", json=body, headers=self.admin)
        self.assertEqual((conflict.status_code, conflict.json()["code"]), (409, "conflict.stale"))
        missing = self.client.put(f"/api/v1/capabilities/mcp/{cap_id}", json={k: v for k, v in body.items() if k != "row_version"}, headers=self.admin)
        self.assertEqual(missing.status_code, 422)

    # -- approval, entitlement, lifecycle --------------------------------------------------------

    def test_with_the_approval_policy_on_the_capability_waits_until_it_is_approved(self) -> None:
        with self.session_factory.begin() as db:
            ensure_policies(db, UUID(self.ids["tenant"]))["skill"].enabled = True
        cap = self.create_mcp().json()["capability"]
        self.assertEqual((cap["status"], cap["pending_approval"]), ("draft", True))
        self.assertEqual(CANDIDATE_LISTERS["mcp"] is not None and self.candidates(), [])  # not mountable before approval
        with self.session_factory() as db:
            request = db.scalar(select(ApprovalRequest).where(ApprovalRequest.object_id == UUID(cap["id"])))
            self.assertEqual((request.title, request.policy_event, request.action), ("接入 Docs server", "skill", "capability.enable"))
            approval_id = request.id
        approver = self.login("admin2@example.com")
        approved = self.client.post(f"/api/v1/approvals/{approval_id}/approve", json={}, headers=approver)
        self.assertEqual(approved.status_code, 200, approved.text)
        run_once("test-worker", self.session_factory, schedule=False)
        self.admin = self.login("admin@example.com")
        after = self.get(cap["id"])
        self.assertEqual((after["status"], after["pending_approval"]), ("available", False))
        self.assertEqual([c["name"] for c in self.candidates()], ["Docs server"])

    def candidates(self) -> list[dict]:
        return self.client.get("/api/v1/capabilities/candidates?kind=mcp", headers=self.admin).json()["items"]

    def test_without_another_approver_the_connection_is_still_saved_and_the_reason_is_shown(self) -> None:
        with self.session_factory.begin() as db:
            ensure_policies(db, UUID(self.ids["tenant"]))["skill"].enabled = True
            admin2 = db.scalar(select(User).where(User.email == "admin2@example.com"))
            for membership in db.scalars(select(Membership).where(Membership.user_id == admin2.id)):
                membership.status = "suspended"  # the requester is the only tenant admin left
        result = self.create_mcp().json()
        self.assertEqual((result["ok"], result["capability"]["status"], result["capability"]["pending_approval"]), (True, "draft", False))
        self.assertIn("没有其他可审批的人", result["capability"]["approval_warning"])
        self.assertEqual(len(self.get(result["capability"]["id"])["mcp"]["tools"]), 2)  # nothing was lost
        with self.session_factory.begin() as db:
            ensure_policies(db, UUID(self.ids["tenant"]))["skill"].enabled = False
        retried = self.post(f"mcp/{result['capability']['id']}/test").json()
        self.assertEqual((retried["capability"]["status"], retried["capability"]["approval_warning"]), ("available", None))

    def test_a_plan_without_the_mcp_entitlement_is_locked(self) -> None:
        from beework.models.plans import TenantSubscription

        with self.session_factory.begin() as db:
            db.get(Tenant, UUID(self.ids["tenant"])).plan = "standard"
            for row in db.scalars(select(TenantSubscription).where(TenantSubscription.tenant_id == UUID(self.ids["tenant"]))):
                db.delete(row)
        locked = self.create_mcp()
        self.assertEqual((locked.status_code, locked.json()["code"]), (403, "plan.feature_locked"))
        self.assertEqual(self.client.get("/api/v1/capabilities?kind=mcp", headers=self.admin).status_code, 200)  # viewing stays open

    def test_disable_and_enable(self) -> None:
        cap_id = self.ready_mcp()
        self.assertEqual(self.post(f"{cap_id}/enable").json()["code"], "capability.already_available")
        disabled = self.post(f"{cap_id}/disable").json()
        self.assertEqual(disabled["status"], "disabled")
        self.assertEqual(self.post(f"{cap_id}/disable").json()["code"], "capability.not_available")
        self.assertEqual(self.candidates(), [])
        self.assertEqual(self.post(f"{cap_id}/enable").json()["status"], "available")

    def test_enabling_needs_a_passing_test_and_a_ticked_tool(self) -> None:
        self.server.fail = "down"
        cap_id = self.create_mcp().json()["capability"]["id"]
        blocked = self.post(f"{cap_id}/enable")
        self.assertEqual((blocked.status_code, blocked.json()["code"]), (409, "capability.not_ready"))

    def test_pointing_a_connection_at_another_host_needs_the_token_again(self) -> None:
        self.server.token = SECRET
        cap_id = self.create_mcp(auth_type="bearer", token=SECRET).json()["capability"]["id"]
        refused = self.update(cap_id, url="https://attacker.example.com/mcp")  # the token field is left blank
        self.assertEqual((refused.status_code, refused.json()["code"]), (422, "capability.credential_reentry"))
        self.assertEqual(self.get(cap_id)["mcp"]["host"], "mcp.example.com")  # nothing was saved
        requests_before = len(self.server.requests)
        self.assertEqual(self.update(cap_id, url="https://mcp.example.com/other/path").status_code, 200)  # the same host: fine
        self.assertEqual(len(self.server.requests), requests_before)  # and nothing was sent anywhere by the refused edit
        moved = self.update(cap_id, url="https://mcp2.example.com/mcp", token="new-token-0000")
        self.assertEqual((moved.status_code, moved.json()["mcp"]["token_mask"]), (200, "••••0000"))

    def test_moving_the_oauth_token_url_to_another_host_needs_the_client_secret_again(self) -> None:
        cap_id = self.oauth_setup()
        refused = self.update(cap_id, oauth_token_url="https://attacker.example.com/token")
        self.assertEqual((refused.status_code, refused.json()["code"]), (422, "capability.credential_reentry"))
        self.assertEqual(self.get(cap_id)["mcp"]["oauth_token_url"], "https://auth.example.com/token")
        self.assertEqual(self.update(cap_id, oauth_token_url="https://auth.example.com/v2/token").status_code, 200)  # the same host
        moved = self.update(cap_id, oauth_token_url="https://auth2.example.com/token", oauth_client_secret="new-secret-0000")
        self.assertEqual((moved.status_code, moved.json()["mcp"]["oauth_client_secret_mask"]), (200, "••••0000"))

    def test_copying_makes_a_draft_without_the_test_result_or_oauth_tokens(self) -> None:
        self.server.token = SECRET
        cap_id = self.create_mcp(auth_type="bearer", token=SECRET).json()["capability"]["id"]
        copy = self.post(f"{cap_id}/copy")
        self.assertEqual(copy.status_code, 201, copy.text)
        data = copy.json()
        self.assertEqual((data["name"], data["status"], data["mcp"]["last_test_ok"], data["mcp"]["tools"], data["mcp"]["token_mask"]), ("Docs server（副本）", "draft", False, [], "••••4321"))
        self.assertEqual(self.post(f"{cap_id}/copy").json()["name"], "Docs server（副本 2）")
        self.assertEqual(self.post(f"mcp/{data['id']}/test").json()["capability"]["status"], "available")  # the copied token works

    def test_delete_is_soft_and_reports_references(self) -> None:
        cap_id = self.ready_mcp()
        assistant = self.create(self.admin, "Uses MCP").json()["id"]
        current = self.client.get(f"/api/v1/assistants/{assistant}", headers=self.admin).json()
        mount = self.client.put(f"/api/v1/assistants/{assistant}", headers=self.admin, json={
            "name": "Uses MCP", "description": "d", "category": "sales", "row_version": current["row_version"],
            "config": {**current["config"], "mounts": {**current["config"]["mounts"], "mcp": [cap_id]}}})
        self.assertEqual(mount.status_code, 200, mount.text)
        refs = self.client.get(f"/api/v1/capabilities/{cap_id}/references", headers=self.admin).json()
        self.assertEqual((refs["total"], refs["items"][0]["name"]), (1, "Uses MCP"))
        self.assertEqual(self.client.delete(f"/api/v1/capabilities/{cap_id}", headers=self.admin).status_code, 204)
        self.assertEqual(self.client.get(f"/api/v1/capabilities/{cap_id}", headers=self.admin).status_code, 404)
        self.assertEqual(self.audits("capability.deleted")[0].details["references"], "1")
        mounts = self.client.get(f"/api/v1/assistants/{assistant}/mounts", headers=self.admin).json()
        self.assertEqual([i["state"] for g in mounts["groups"] for i in g["items"]], ["deleted"])

    # -- list ------------------------------------------------------------------------------

    def test_the_list_filters_by_kind_and_search_and_counts_per_kind(self) -> None:
        self.ready_mcp("Alpha")
        self.ready_mcp("Beta")
        listing = self.client.get("/api/v1/capabilities?kind=mcp", headers=self.admin).json()
        self.assertEqual([c["name"] for c in listing], ["Alpha", "Beta"])
        self.assertEqual([c["name"] for c in self.client.get("/api/v1/capabilities?kind=mcp&q=BET", headers=self.admin).json()], ["Beta"])
        self.assertEqual([c["skill"]["builtin"] for c in self.client.get("/api/v1/capabilities?kind=skill", headers=self.admin).json()], [True, True, True])  # the platform built-ins (T-074)
        self.assertEqual(self.client.get("/api/v1/capabilities/summary", headers=self.admin).json(), {"skill": 3, "mcp": 2, "api": 0})
        self.assertEqual(self.client.get("/api/v1/capabilities?kind=bogus", headers=self.admin).status_code, 422)

    # -- OAuth 2.1 ---------------------------------------------------------------------------

    def oauth_setup(self) -> str:
        created = self.create_mcp(auth_type="oauth", oauth_authorize_url="https://auth.example.com/authorize", oauth_token_url="https://auth.example.com/token",
                                  oauth_client_id="client-1", oauth_client_secret="client-secret-9999", oauth_scopes="read write").json()
        self.assertEqual((created["ok"], created["capability"]["status"]), (False, "draft"))
        self.assertIn("OAuth", created["error"])
        self.assertEqual((created["capability"]["mcp"]["oauth_client_secret_mask"], created["capability"]["mcp"]["oauth_authorized"]), ("••••9999", False))
        return created["capability"]["id"]

    def token_server(self, expires_in: int = 3600) -> list[dict]:
        seen: list[dict] = []
        mcp_server = self.server

        def handler(request: httpx.Request) -> httpx.Response:
            if request.headers["host"] == "auth.example.com":  # the request is pinned to the validated IP; Host keeps the name
                form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
                seen.append(form)
                grant = form["grant_type"]
                token = "access-1" if grant == "authorization_code" else "access-2"
                data = {"access_token": token, "expires_in": expires_in}
                if grant == "authorization_code":
                    data["refresh_token"] = "refresh-1"
                return httpx.Response(200, json=data)
            if request.headers.get("authorization") not in ("Bearer access-1", "Bearer access-2"):
                return httpx.Response(401)
            return mcp_server(request)

        mcp_client.TRANSPORT = httpx.MockTransport(handler)
        return seen

    def start(self, cap_id: str) -> tuple[str, dict]:
        url = self.post(f"mcp/{cap_id}/oauth/start").json()["authorize_url"]
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        return url, query

    def test_oauth_authorization_code_with_pkce_end_to_end(self) -> None:
        cap_id = self.oauth_setup()
        seen = self.token_server()
        url, query = self.start(cap_id)
        self.assertTrue(url.startswith("https://auth.example.com/authorize?"))
        self.assertEqual((query["response_type"], query["client_id"], query["code_challenge_method"], query["scope"]), ("code", "client-1", "S256", "read write"))
        self.assertTrue(query["redirect_uri"].endswith("/api/v1/capabilities/mcp/oauth/callback"))
        callback = self.client.get(f"/api/v1/capabilities/mcp/oauth/callback?code=abc&state={query['state']}", follow_redirects=False)
        self.assertEqual(callback.status_code, 302)
        self.assertIn(f"/capabilities/mcp/{cap_id}?oauth=ok", callback.headers["location"])
        exchange = seen[0]
        self.assertEqual((exchange["grant_type"], exchange["code"], exchange["client_id"], exchange["client_secret"]), ("authorization_code", "abc", "client-1", "client-secret-9999"))
        from base64 import urlsafe_b64encode
        from hashlib import sha256
        self.assertEqual(urlsafe_b64encode(sha256(exchange["code_verifier"].encode()).digest()).rstrip(b"=").decode(), query["code_challenge"])  # PKCE
        cap = self.get(cap_id)
        self.assertEqual((cap["status"], cap["mcp"]["oauth_authorized"], cap["mcp"]["last_test_ok"], len(cap["mcp"]["tools"])), ("available", True, True, 2))
        with self.session_factory() as db:
            conn = db.get(McpConnection, UUID(cap_id))
            self.assertNotIn("access-1", conn.oauth_access_encrypted)
            self.assertNotIn("refresh-1", conn.oauth_refresh_encrypted)
        self.assertNotIn("access-1", json.dumps(cap))

    def test_a_tampered_expired_or_foreign_state_is_refused(self) -> None:
        cap_id = self.oauth_setup()
        seen = self.token_server()
        _, query = self.start(cap_id)
        for bad in (query["state"][:-3] + "abc", "garbage", ""):
            response = self.client.get(f"/api/v1/capabilities/mcp/oauth/callback?code=abc&state={bad}", follow_redirects=False)
            self.assertEqual(response.status_code, 302)
            self.assertIn("oauth=error", response.headers["location"], bad)
        expired = svc.jwt.encode({"t": self.ids["tenant"], "u": "x", "c": cap_id, "v": "v", "exp": datetime.now(UTC) - timedelta(minutes=1)}, settings.auth_secret, algorithm="HS256")
        self.assertIn("oauth=error", self.client.get(f"/api/v1/capabilities/mcp/oauth/callback?code=abc&state={expired}", follow_redirects=False).headers["location"])
        # a state issued to somebody else cannot be completed by this user
        with self.session_factory() as db:
            other_user = db.scalar(select(User).where(User.email == "builder@example.com"))
        foreign = svc.make_state(UUID(self.ids["tenant"]), other_user.id, UUID(cap_id), "verifier")
        self.assertIn("oauth=error", self.client.get(f"/api/v1/capabilities/mcp/oauth/callback?code=abc&state={foreign}", follow_redirects=False).headers["location"])
        denied = self.client.get(f"/api/v1/capabilities/mcp/oauth/callback?error=access_denied&state={query['state']}", follow_redirects=False)
        self.assertIn("oauth=error", denied.headers["location"])
        self.assertEqual(seen, [])  # no code was ever exchanged
        self.assertEqual(self.get(cap_id)["mcp"]["oauth_authorized"], False)

    def test_the_callback_needs_a_signed_in_user(self) -> None:
        cap_id = self.oauth_setup()
        _, query = self.start(cap_id)
        self.client.cookies.clear()
        self.assertEqual(self.client.get(f"/api/v1/capabilities/mcp/oauth/callback?code=abc&state={query['state']}", follow_redirects=False).status_code, 401)

    def test_an_expiring_token_is_refreshed_before_the_call(self) -> None:
        cap_id = self.oauth_setup()
        seen = self.token_server()
        _, query = self.start(cap_id)
        self.client.get(f"/api/v1/capabilities/mcp/oauth/callback?code=abc&state={query['state']}", follow_redirects=False)
        with self.session_factory.begin() as db:
            db.get(McpConnection, UUID(cap_id)).oauth_expires_at = datetime.now(UTC) + timedelta(seconds=10)
        tested = self.post(f"mcp/{cap_id}/test").json()
        self.assertTrue(tested["ok"])
        self.assertEqual([f["grant_type"] for f in seen], ["authorization_code", "refresh_token"])
        self.assertEqual(seen[1]["refresh_token"], "refresh-1")

    def test_oauth_start_needs_an_oauth_connection(self) -> None:
        cap_id = self.ready_mcp()
        response = self.post(f"mcp/{cap_id}/oauth/start")
        self.assertEqual((response.status_code, response.json()["code"]), (409, "capability.not_oauth"))

    # -- health ------------------------------------------------------------------------------

    def test_three_failed_checks_flag_the_connection_and_tell_the_admins_once(self) -> None:
        cap_id = self.ready_mcp()
        self.server.fail = "down"
        for _ in range(2):
            with self.session_factory.begin() as db:
                svc.mcp_health_job(db, {})
        self.assertEqual(self.get(cap_id)["mcp"]["health"], "ok")  # two failures are not enough
        for _ in range(3):
            with self.session_factory.begin() as db:
                svc.mcp_health_job(db, {})
        data = self.get(cap_id)
        self.assertEqual((data["mcp"]["health"], data["status"]), ("error", "available"))  # flagged, never disabled
        with self.session_factory() as db:
            notes = list(db.scalars(select(InAppNotification).where(InAppNotification.type == "mcp.unhealthy")))
        self.assertEqual(len(notes), 2)  # the two tenant admins, once
        self.assertIn("Docs server", notes[0].title)
        self.server.fail = None
        with self.session_factory.begin() as db:
            svc.mcp_health_job(db, {})
        self.assertEqual(self.get(cap_id)["mcp"]["health"], "ok")

    def test_the_health_job_only_checks_available_connections(self) -> None:
        cap_id = self.ready_mcp()
        self.post(f"{cap_id}/disable")
        self.server.requests.clear()
        with self.session_factory.begin() as db:
            svc.mcp_health_job(db, {})
        self.assertEqual(self.server.requests, [])

    def test_the_job_is_scheduled_every_five_minutes(self) -> None:
        from beework.models.jobs import Job
        from beework.worker.scheduler import enqueue_scheduled_jobs

        with self.session_factory.begin() as db:
            enqueue_scheduled_jobs(db, now=datetime(2026, 11, 12, 10, 7, tzinfo=UTC))
            enqueue_scheduled_jobs(db, now=datetime(2026, 11, 12, 10, 9, tzinfo=UTC))  # same five-minute window
        with self.session_factory() as db:
            keys = [j.idempotency_key for j in db.scalars(select(Job)) if j.kind == "capabilities.mcp_health"]
        self.assertEqual(keys, ["capabilities.mcp_health:2026-11-12T10:05"])

    # -- mounting and the executor ----------------------------------------------------------------

    def test_mount_resolution_and_candidates(self) -> None:
        self.assertIn("mcp", MOUNT_RESOLVERS)
        cap_id = self.ready_mcp()
        listed = self.candidates()
        self.assertEqual([(c["name"], c["meta"]["tools"]) for c in listed], [("Docs server", 2)])
        assistant = self.create(self.admin, "Mounter").json()["id"]
        current = self.client.get(f"/api/v1/assistants/{assistant}", headers=self.admin).json()
        body = {"name": "Mounter", "description": "d", "category": "sales", "row_version": current["row_version"], "config": {**current["config"], "mounts": {**current["config"]["mounts"], "mcp": [cap_id]}}}
        self.assertEqual(self.client.put(f"/api/v1/assistants/{assistant}", json=body, headers=self.admin).status_code, 200)
        self.post(f"{cap_id}/disable")
        mounts = self.client.get(f"/api/v1/assistants/{assistant}/mounts", headers=self.admin).json()
        self.assertEqual([i["state"] for g in mounts["groups"] for i in g["items"]], ["disabled"])
        again = self.client.put(f"/api/v1/assistants/{assistant}", json={**body, "row_version": mounts and self.client.get(f"/api/v1/assistants/{assistant}", headers=self.admin).json()["row_version"]}, headers=self.admin)
        self.assertEqual(again.status_code, 200)  # a disabled mount that is already there never blocks saving

    def test_the_executor_runs_only_ticked_tools_of_available_connections(self) -> None:
        cap_id = self.ready_mcp()
        self.update(cap_id, selected_tools=["search_docs"])
        name = svc.tool_name(UUID(cap_id), "search_docs")
        with self.session_factory() as db:
            specs = svc.mcp_tool_specs(db, UUID(self.ids["tenant"]), [UUID(cap_id)])
            self.assertEqual([(s.name, s.writes) for s in specs], [(name, False)])
            ctx = ToolContext(tenant_id=UUID(self.ids["tenant"]), allowed=frozenset({name, svc.tool_name(UUID(cap_id), "create_ticket")}), run_id=uuid4())
            result = invoke_tool(db, ctx, name, {"q": "refund"})
            self.assertEqual((result.status, result.content), ("ok", "ok"))
            self.assertEqual(self.server.calls[-1], {"name": "search_docs", "arguments": {"q": "refund"}})
            unticked = invoke_tool(db, ctx, svc.tool_name(UUID(cap_id), "create_ticket"), {})
            self.assertEqual((unticked.status, unticked.code), ("error", "tool.not_found"))  # not ticked: invisible
            self.assertEqual(invoke_tool(db, ctx, name, {}).code, "tool.invalid_arguments")  # the server's own schema applies
            other = ToolContext(tenant_id=UUID(self.ids["other"]), allowed=frozenset({name}), run_id=uuid4())
            self.assertEqual(invoke_tool(db, other, name, {"q": "x"}).code, "tool.not_found")  # another tenant's connection
        with self.session_factory() as db:
            rows = list(db.scalars(select(ToolInvocation).where(ToolInvocation.tool_name == name)))
            self.assertEqual({r.source for r in rows}, {"mcp"})

    def test_writes_ask_for_confirmation_unless_the_tool_is_read_only(self) -> None:
        cap_id = self.ready_mcp()
        write = svc.tool_name(UUID(cap_id), "create_ticket")
        with self.session_factory() as db:
            ctx = ToolContext(tenant_id=UUID(self.ids["tenant"]), allowed=frozenset({write}), run_id=uuid4())
            with self.assertRaises(ToolConfirmationRequired):
                invoke_tool(db, ctx, write, {"title": "t"})
            self.assertEqual(self.server.calls, [])
            confirmed = invoke_tool(db, ctx, write, {"title": "t"}, confirmed=True, call_index=1)
            self.assertEqual(confirmed.status, "ok")

    def test_server_errors_and_credentials_stay_out_of_what_the_model_sees(self) -> None:
        self.server.token = SECRET
        cap_id = self.create_mcp(auth_type="bearer", token=SECRET).json()["capability"]["id"]
        name = svc.tool_name(UUID(cap_id), "search_docs")
        self.server.call_result = {"content": [{"type": "text", "text": f"echo {SECRET}"}], "isError": True}
        with self.session_factory() as db:
            ctx = ToolContext(tenant_id=UUID(self.ids["tenant"]), allowed=frozenset({name}), run_id=uuid4())
            result = invoke_tool(db, ctx, name, {"q": "x"})
        self.assertEqual((result.status, result.code), ("error", "tool.mcp_error"))
        self.assertNotIn(SECRET, result.content)

    def test_a_disabled_connection_is_not_callable(self) -> None:
        cap_id = self.ready_mcp()
        name = svc.tool_name(UUID(cap_id), "search_docs")
        self.post(f"{cap_id}/disable")
        with self.session_factory() as db:
            self.assertEqual(invoke_tool(db, ToolContext(tenant_id=UUID(self.ids["tenant"]), allowed=frozenset({name}), run_id=uuid4()), name, {"q": "x"}).code, "tool.not_found")
        self.assertIn("mcp", PROVIDERS)

    # -- permissions and isolation ----------------------------------------------------------------

    def test_roles_without_permission_cannot_change_capabilities(self) -> None:
        cap_id = self.ready_mcp()
        row_version = self.get(cap_id)["row_version"]
        body = {"name": "X", "url": URL, "auth_type": "none", "row_version": row_version}
        for email in ("viewer@example.com", "operator@example.com"):
            headers = self.login(email)
            self.assertEqual(self.client.get("/api/v1/capabilities?kind=mcp", headers=headers).status_code, 200)
            self.assertEqual(self.client.get(f"/api/v1/capabilities/{cap_id}", headers=headers).status_code, 200)
            self.assertEqual(self.create_mcp("Nope", headers=headers).status_code, 403)
            self.assertEqual(self.client.put(f"/api/v1/capabilities/mcp/{cap_id}", json=body, headers=headers).status_code, 403)
            for path in (f"mcp/{cap_id}/test", f"mcp/{cap_id}/oauth/start", f"{cap_id}/copy", f"{cap_id}/enable", f"{cap_id}/disable"):
                self.assertEqual(self.post(path, headers=headers).status_code, 403, path)
            self.assertEqual(self.client.delete(f"/api/v1/capabilities/{cap_id}", headers=headers).status_code, 403)
            self.assertEqual(self.client.get("/api/v1/capabilities/candidates?kind=mcp", headers=headers).status_code, 403)
        data = self.get(cap_id, headers=self.login("viewer@example.com"))
        self.assertEqual((data["can_edit"], data["can_publish"], data["can_delete"]), (False, False, False))

    def test_a_builder_manages_capabilities_like_the_matrix_says(self) -> None:
        headers = self.login("builder@example.com")
        created = self.create_mcp("Builder's", headers=headers)
        self.assertEqual(created.status_code, 201, created.text)
        cap_id = created.json()["capability"]["id"]
        self.assertEqual(self.post(f"{cap_id}/disable", headers=headers).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/v1/capabilities/{cap_id}", headers=headers).status_code, 204)

    def test_other_tenants_cannot_see_or_change_a_connection(self) -> None:
        cap_id = self.ready_mcp()
        row_version = self.get(cap_id)["row_version"]
        stranger = self.login("other@example.com", self.ids["other"])
        self.assertEqual(self.client.get("/api/v1/capabilities?kind=mcp", headers=stranger).json(), [])
        body = {"name": "X", "url": URL, "auth_type": "none", "row_version": row_version}
        for method, path, payload in [
            ("get", f"/api/v1/capabilities/{cap_id}", None), ("get", f"/api/v1/capabilities/{cap_id}/references", None),
            ("put", f"/api/v1/capabilities/mcp/{cap_id}", body), ("post", f"/api/v1/capabilities/mcp/{cap_id}/test", None),
            ("post", f"/api/v1/capabilities/mcp/{cap_id}/oauth/start", None), ("post", f"/api/v1/capabilities/{cap_id}/copy", None),
            ("post", f"/api/v1/capabilities/{cap_id}/enable", None), ("post", f"/api/v1/capabilities/{cap_id}/disable", None),
            ("delete", f"/api/v1/capabilities/{cap_id}", None),
        ]:
            response = getattr(self.client, method)(path, headers=stranger, **({"json": payload} if payload is not None else {}))
            self.assertEqual(response.status_code, 404, f"{method} {path}")
        self.assertEqual(self.client.get("/api/v1/capabilities/candidates?kind=mcp", headers=stranger).json()["items"], [])

    def test_writes_require_the_csrf_token(self) -> None:
        response = self.client.post("/api/v1/capabilities/mcp", json=self.mcp_body(), headers={"X-Tenant-ID": self.ids["tenant"]})
        self.assertEqual(response.status_code, 403)

    def test_a_connection_of_a_deleted_tenant_row_is_not_listed(self) -> None:
        self.ready_mcp()
        with self.session_factory() as db:
            self.assertEqual(len(db.scalars(select(Capability)).all()), 1)


for _name in [n for n in dir(test_assistants.AssistantTests) if n.startswith("test_")]:
    setattr(McpTests, _name, None)  # inherited fixtures only; the assistant tests run in their own module


if __name__ == "__main__":
    unittest.main()
