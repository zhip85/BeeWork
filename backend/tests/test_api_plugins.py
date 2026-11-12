"""T-073: API plugins — the capability endpoints, the executor provider, authentication and isolation."""

import json
import unittest
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select

import test_assistants
from beework import api_plugin_service as svc
from beework.approvals import ensure_policies
from beework.capabilities import CANDIDATE_LISTERS, MOUNT_RESOLVERS
from beework.models import AuditEvent
from beework.models.capabilities import ApiPlugin
from beework.models.tools import ToolInvocation
from beework.tools.executor import PROVIDERS, ToolContext, invoke_tool

PUBLIC = "93.184.216.34"
BASE = "https://api.shop.example.com/v1"
GRAPHQL = "https://graph.shop.example.com/graphql"
TOKEN_URL = "https://auth.shop.example.com/oauth/token"
SECRET = "key-SENTINEL-1234567890"

OPENAPI = """
openapi: 3.0.3
info: {title: Shop, version: "1"}
paths:
  /orders:
    get:
      operationId: listOrders
      summary: List orders
      parameters:
        - {name: status, in: query, schema: {type: string}}
    post:
      operationId: createOrder
      summary: Create an order
      requestBody:
        required: true
        content:
          application/json:
            schema: {type: object, required: [sku], properties: {sku: {type: string}, qty: {type: integer}}}
  /orders/{orderId}:
    get:
      operationId: getOrder
      summary: One order
      parameters:
        - {name: orderId, in: path, required: true, schema: {type: string}}
        - {name: X-Region, in: header, schema: {type: string}}
"""

SDL = """
type Order { id: ID!, status: String }
type Query { order(id: ID!): Order }
type Mutation { cancelOrder(id: ID!): Order }
"""


class FakeShop:
    """A REST + GraphQL + OAuth server on a mock transport; records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.tokens_issued = 0
        self.expires_in = 3600
        self.fail: int | str | None = None  # an HTTP status, or "down"
        self.key = SECRET
        self.graphql_errors = False
        self.reject_all = False  # answer 401 to every API call, whatever credential comes

    def authorized(self, request: httpx.Request) -> bool:
        if self.reject_all:
            return False
        if self.key is None:  # an open API
            return True
        header = request.headers.get("authorization") or ""
        return request.headers.get("x-api-key") == self.key or header in (f"Bearer {self.key}", f"Bearer tok-{self.tokens_issued}")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail == "down":
            raise httpx.ConnectError("refused", request=request)
        if request.url.host == PUBLIC and request.url.path == "/oauth/token":
            form = parse_qs(request.content.decode())
            if form.get("client_secret") != ["cs-SENTINEL-9999"] or form.get("grant_type") != ["client_credentials"]:
                return httpx.Response(400, json={"error": "invalid_client"})
            self.tokens_issued += 1
            return httpx.Response(200, json={"access_token": f"tok-{self.tokens_issued}", "expires_in": self.expires_in})
        if isinstance(self.fail, int):
            return httpx.Response(self.fail, json={"message": f"broken {self.key}"})
        if not self.authorized(request):
            return httpx.Response(401, json={"message": "no"})
        path = request.url.path
        if path == "/graphql":
            body = json.loads(request.content)
            if self.graphql_errors:
                return httpx.Response(200, json={"errors": [{"message": "Order not found"}], "data": None})
            field = "cancelOrder" if "cancelOrder" in body["query"] else "order"
            return httpx.Response(200, json={"data": {field: {"id": body["variables"].get("id"), "status": "CANCELLED" if field == "cancelOrder" else "OPEN"}}})
        if path == "/v1/orders" and request.method == "GET":
            return httpx.Response(200, json=[{"id": "o1", "status": request.url.params.get("status", "all")}])
        if path == "/v1/orders" and request.method == "POST":
            return httpx.Response(201, json={"id": "o2", **json.loads(request.content)})
        if path.startswith("/v1/orders/"):
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[1], "region": request.headers.get("x-region")})
        return httpx.Response(404, json={"message": "not found"})


class ApiPluginTests(test_assistants.AssistantTests):
    def setUp(self) -> None:
        super().setUp()
        self.shop = FakeShop()
        svc.TRANSPORT, svc.RESOLVER = httpx.MockTransport(self.shop), lambda host: [PUBLIC]
        self.addCleanup(lambda: (setattr(svc, "TRANSPORT", None), setattr(svc, "RESOLVER", None)))
        with self.session_factory.begin() as db:
            ensure_policies(db, UUID(self.ids["tenant"]))["skill"].enabled = False  # most tests: no approval step
        self.admin = self.login("admin@example.com")

    # -- helpers -------------------------------------------------------------------

    def plugin_body(self, name: str = "Shop API", **extra) -> dict:
        return {
            "name": name, "description": "Orders", "api_type": "rest", "base_url": BASE, "auth_type": "api_key",
            "api_key_header": "X-API-Key", "secret": SECRET, "spec_text": OPENAPI, "spec_filename": "shop.yaml", **extra,
        }

    def make_plugin(self, name: str = "Shop API", headers=None, **extra):
        return self.client.post("/api/v1/capabilities/api", json=self.plugin_body(name, **extra), headers=headers or self.admin)

    def get_cap(self, cap_id: str, headers=None) -> dict:
        response = self.client.get(f"/api/v1/capabilities/{cap_id}", headers=headers or self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def update_plugin(self, cap_id: str, headers=None, **changes):
        current = self.get_cap(cap_id, headers)
        api = current["api"]
        body = {
            "name": current["name"], "description": current["description"], "api_type": api["api_type"], "base_url": api["base_url"],
            "auth_type": api["auth_type"], "api_key_header": api["api_key_header"], "oauth_token_url": api["oauth_token_url"],
            "oauth_client_id": api["oauth_client_id"], "oauth_scope": api["oauth_scope"], "row_version": current["row_version"], **changes,
        }
        return self.client.put(f"/api/v1/capabilities/api/{cap_id}", json=body, headers=headers or self.admin)

    def call_api(self, cap_id: str, tool: str, arguments: dict | None = None, confirmed=None, headers=None):
        return self.client.post(
            f"/api/v1/capabilities/api/{cap_id}/test-call", json={"tool": tool, "arguments": arguments or {}, "confirmed": confirmed},
            headers=headers or self.admin,
        )

    def ready_plugin(self, name: str = "Shop API", tools=("listOrders", "createOrder", "getOrder"), **extra) -> str:
        cap_id = self.make_plugin(name, **extra).json()["id"]
        self.assertEqual(self.update_plugin(cap_id, selected_tools=list(tools)).status_code, 200)
        self.assertTrue(self.call_api(cap_id, "listOrders").json()["ok"])
        data = self.get_cap(cap_id)
        self.assertEqual(data["status"], "available", data)
        return cap_id

    def audits(self, action: str) -> list[AuditEvent]:
        with self.session_factory() as db:
            return list(db.scalars(select(AuditEvent).where(AuditEvent.action == action)))

    def tool_ctx(self, *names: str, tenant: str | None = None) -> ToolContext:
        return ToolContext(tenant_id=UUID(tenant or self.ids["tenant"]), allowed=frozenset(names), run_id=uuid4())

    # -- create and parse ---------------------------------------------------------------

    def test_saving_a_plugin_with_a_spec_parses_the_tools_and_leaves_a_draft(self) -> None:
        response = self.make_plugin()
        self.assertEqual(response.status_code, 201, response.text)
        cap = response.json()
        api = cap["api"]
        self.assertEqual((cap["kind"], cap["status"], cap["meta"], api["has_spec"], api["spec_filename"]), ("api", "draft", "api.shop.example.com · REST", True, "shop.yaml"))
        self.assertEqual([(t["name"], t["writes"], t["selected"]) for t in api["tools"]], [("listOrders", False, False), ("createOrder", True, False), ("getOrder", False, False)])
        self.assertEqual((api["secret_mask"], api["auth_type"], api["api_key_header"]), ("••••7890", "api_key", "X-API-Key"))
        self.assertEqual(len(self.audits("capability.created")), 1)

    def test_a_plugin_without_a_spec_is_a_draft(self) -> None:
        cap = self.make_plugin(spec_text=None).json()
        self.assertEqual((cap["status"], cap["api"]["has_spec"], cap["api"]["tools"]), ("draft", False, []))

    def test_an_unreadable_spec_is_refused_with_the_line_and_reason(self) -> None:
        refused = self.make_plugin(spec_text="openapi: 3.0.0\npaths:\n  /a:\n    get: [unclosed\n")
        self.assertEqual((refused.status_code, refused.json()["code"]), (422, "capability.spec_invalid"))
        self.assertIn("第", refused.json()["detail"])
        external = self.make_plugin(spec_text=OPENAPI.replace("{type: object, required: [sku]", "{$ref: 'https://evil.example.com/x.json#/A', required: [sku]"))
        self.assertEqual(external.status_code, 422)
        self.assertIn("外部", external.json()["detail"])

    def test_the_parse_endpoint_previews_without_saving(self) -> None:
        ok = self.client.post("/api/v1/capabilities/api/parse", json={"api_type": "rest", "spec_text": OPENAPI}, headers=self.admin).json()
        self.assertEqual((ok["ok"], ok["tool_count"], [t["name"] for t in ok["tools"]]), (True, 3, ["listOrders", "createOrder", "getOrder"]))
        bad = self.client.post("/api/v1/capabilities/api/parse", json={"api_type": "rest", "spec_text": "not: [valid"}, headers=self.admin).json()
        self.assertEqual((bad["ok"], bad["line"] is not None), (False, True))
        graphql = self.client.post("/api/v1/capabilities/api/parse", json={"api_type": "graphql", "spec_text": SDL}, headers=self.admin).json()
        self.assertEqual([(t["name"], t["writes"]) for t in graphql["tools"]], [("order", False), ("cancelOrder", True)])
        self.assertEqual(self.client.get("/api/v1/capabilities?kind=api", headers=self.admin).json(), [])

    def test_fields_are_validated(self) -> None:
        self.assertEqual(self.make_plugin(name="  ").status_code, 400)
        self.assertEqual(self.make_plugin(name="x" * 51).status_code, 400)
        self.assertEqual(self.make_plugin(description="d" * 501).status_code, 400)
        self.assertEqual(self.make_plugin(base_url="").json()["code"], "capability.invalid_url")
        self.assertEqual(self.make_plugin(base_url="http://api.shop.example.com").json()["code"], "capability.invalid_url")
        self.assertEqual(self.make_plugin(base_url="https://user:pw@api.shop.example.com").status_code, 422)
        self.assertEqual(self.make_plugin(api_key_header="").json()["code"], "capability.invalid_header")
        self.assertEqual(self.make_plugin(api_key_header="Host").json()["code"], "capability.invalid_header")
        self.assertEqual(self.make_plugin(secret="").json()["code"], "capability.token_required")
        self.assertEqual(self.make_plugin(auth_type="bearer", secret=None).json()["code"], "capability.token_required")
        self.assertEqual(self.make_plugin(auth_type="oauth_cc", oauth_token_url=TOKEN_URL, oauth_client_id="").json()["code"], "capability.oauth_client_required")
        self.assertEqual(self.make_plugin(auth_type="oauth_cc", oauth_token_url="http://x.example.com", oauth_client_id="c", oauth_client_secret="s").json()["code"], "capability.invalid_url")
        self.assertEqual(self.make_plugin(auth_type="oauth_cc", oauth_token_url=TOKEN_URL, oauth_client_id="c").json()["code"], "capability.oauth_secret_required")
        self.assertEqual(self.make_plugin(auth_type="invalid").status_code, 422)
        self.make_plugin("Dup")
        self.assertEqual(self.make_plugin(" dup ").json()["code"], "capability.name_taken")

    def test_secrets_are_masked_encrypted_and_kept_on_blank(self) -> None:
        cap_id = self.make_plugin().json()["id"]
        everything = json.dumps(self.get_cap(cap_id)) + json.dumps(self.client.get("/api/v1/capabilities?kind=api", headers=self.admin).json())
        self.assertNotIn(SECRET, everything)
        with self.session_factory() as db:
            row = db.get(ApiPlugin, UUID(cap_id))
            self.assertTrue(row.secret_encrypted)
            self.assertNotIn(SECRET, row.secret_encrypted)
        kept = self.update_plugin(cap_id, description="edited")  # no "secret" in the body: the stored key stays
        self.assertEqual((kept.status_code, kept.json()["api"]["secret_mask"]), (200, "••••7890"))
        replaced = self.update_plugin(cap_id, secret="key-ROTATED-0000")
        self.assertEqual(replaced.json()["api"]["secret_mask"], "••••0000")

    # -- tools and the test call -----------------------------------------------------------

    def test_a_plugin_becomes_available_only_after_a_ticked_tool_and_a_successful_test_call(self) -> None:
        cap_id = self.make_plugin().json()["id"]
        refused = self.call_api(cap_id, "listOrders")
        self.assertEqual((refused.status_code, refused.json()["code"]), (422, "capability.tool_not_selected"))
        unknown = self.update_plugin(cap_id, selected_tools=["nope"])
        self.assertEqual((unknown.status_code, unknown.json()["code"]), (422, "capability.unknown_tool"))
        ticked = self.update_plugin(cap_id, selected_tools=["listOrders"]).json()
        self.assertEqual((ticked["status"], [t["selected"] for t in ticked["api"]["tools"]]), ("draft", [True, False, False]))
        called = self.call_api(cap_id, "listOrders", {"status": "open"}).json()
        self.assertEqual((called["ok"], called["status"], json.loads(called["content"])), (True, "ok", [{"id": "o1", "status": "open"}]))
        self.assertEqual((called["capability"]["status"], called["capability"]["api"]["last_test_ok"], called["capability"]["api"]["last_test_tool"]), ("available", True, "listOrders"))
        self.assertEqual(len(self.audits("capability.tested")), 1)  # the refused call (tool not ticked) is not a test
        self.assertEqual(len(self.audits("capability.enabled")), 1)

    def test_a_failed_test_call_keeps_the_draft_with_the_reason(self) -> None:
        cap_id = self.make_plugin().json()["id"]
        self.update_plugin(cap_id, selected_tools=["listOrders"])
        self.shop.fail = 500
        failed = self.call_api(cap_id, "listOrders").json()
        self.assertEqual((failed["ok"], failed["code"], failed["capability"]["status"]), (False, "tool.api_error", "draft"))
        self.assertIn("接口返回 500", failed["content"])
        self.assertNotIn(SECRET, failed["content"])  # the server echoed the key; the executor scrubbed it
        self.assertEqual(failed["capability"]["api"]["last_test_ok"], False)
        self.shop.fail = None
        self.assertTrue(self.call_api(cap_id, "listOrders").json()["ok"])

    def test_the_tool_arguments_are_validated_by_the_executor(self) -> None:
        cap_id = self.ready_plugin()
        invalid = self.call_api(cap_id, "getOrder", {}).json()  # orderId is required
        self.assertEqual((invalid["ok"], invalid["code"]), (False, "tool.invalid_arguments"))
        self.assertEqual(self.call_api(cap_id, "createOrder", {"body": {"qty": 1}}, confirmed=True).json()["code"], "tool.invalid_arguments")  # sku missing

    def test_a_write_needs_a_second_confirmation_even_in_a_test(self) -> None:
        cap_id = self.ready_plugin()
        before = len(self.shop.requests)
        asked = self.call_api(cap_id, "createOrder", {"body": {"sku": "A1", "qty": 2}}).json()
        self.assertEqual((asked["ok"], asked["status"], asked["code"]), (False, "confirmation_required", "tool.confirmation_required"))
        self.assertEqual(len(self.shop.requests), before)  # nothing was sent
        cancelled = self.call_api(cap_id, "createOrder", {"body": {"sku": "A1"}}, confirmed=False).json()
        self.assertEqual((cancelled["status"], len(self.shop.requests)), ("cancelled", before))
        done = self.call_api(cap_id, "createOrder", {"body": {"sku": "A1", "qty": 2}}, confirmed=True).json()
        self.assertEqual((done["ok"], json.loads(done["content"])), (True, {"id": "o2", "sku": "A1", "qty": 2}))
        sent = self.shop.requests[-1]
        self.assertEqual((sent.method, json.loads(sent.content)), ("POST", {"sku": "A1", "qty": 2}))

    def test_changing_the_spec_or_base_url_resets_the_test_and_drops_a_plugin_back_to_draft(self) -> None:
        cap_id = self.ready_plugin(tools=("listOrders",))
        dropped = self.update_plugin(cap_id, base_url="https://api2.shop.example.com/v1", secret=SECRET).json()  # a new host: the key is entered again
        self.assertEqual((dropped["status"], dropped["api"]["last_test_ok"]), ("draft", False))
        self.assertTrue(self.call_api(cap_id, "listOrders").json()["ok"])
        again = self.update_plugin(cap_id, spec_text=OPENAPI.replace("listOrders", "listAll")).json()
        self.assertEqual((again["status"], [t["name"] for t in again["api"]["tools"]], [t["selected"] for t in again["api"]["tools"]]), ("draft", ["listAll", "createOrder", "getOrder"], [False, False, False]))
        cleared = self.update_plugin(cap_id, spec_text="").json()
        self.assertEqual((cleared["api"]["has_spec"], cleared["api"]["tools"], cleared["status"]), (False, [], "draft"))

    def test_the_description_can_be_graphql(self) -> None:
        cap_id = self.make_plugin("Graph", api_type="graphql", base_url=GRAPHQL, spec_text=SDL, spec_filename="shop.graphql").json()["id"]
        self.assertEqual(self.get_cap(cap_id)["meta"], "graph.shop.example.com · GraphQL")
        self.update_plugin(cap_id, selected_tools=["order", "cancelOrder"])
        called = self.call_api(cap_id, "order", {"id": "7"}).json()
        self.assertEqual((called["ok"], json.loads(called["content"])), (True, {"id": "7", "status": "OPEN"}))
        document = json.loads(self.shop.requests[-1].content)
        self.assertEqual(document["query"], "query($id: ID!) { order(id: $id) { id status } }")
        self.assertEqual(document["variables"], {"id": "7"})
        self.assertEqual(self.call_api(cap_id, "cancelOrder", {"id": "7"}).json()["status"], "confirmation_required")  # a Mutation is a write
        cancelled = self.call_api(cap_id, "cancelOrder", {"id": "7"}, confirmed=True).json()
        self.assertEqual(json.loads(cancelled["content"])["status"], "CANCELLED")
        self.shop.graphql_errors = True
        failed = self.call_api(cap_id, "order", {"id": "7"}).json()
        self.assertEqual((failed["ok"], failed["code"]), (False, "tool.api_error"))
        self.assertIn("Order not found", failed["content"])

    # -- the request the executor builds ---------------------------------------------------------

    def run_tool(self, cap_id: str, tool: str, args: dict, confirmed=True):
        name = svc.tool_name(UUID(cap_id), tool)
        with self.session_factory() as db:
            return invoke_tool(db, self.tool_ctx(name), name, args, confirmed=confirmed)

    def test_path_query_and_header_parameters_are_put_in_the_right_places(self) -> None:
        cap_id = self.ready_plugin()
        result = self.run_tool(cap_id, "getOrder", {"orderId": "a/b c", "X-Region": "eu"})
        self.assertEqual((result.status, json.loads(result.content)["region"]), ("ok", "eu"))
        request = self.shop.requests[-1]
        self.assertEqual(request.url.raw_path, b"/v1/orders/a%2Fb%20c")  # encoded: a value cannot add path segments
        self.assertEqual((request.method, request.url.host, request.headers["host"], request.headers["x-api-key"]), ("GET", PUBLIC, "api.shop.example.com", SECRET))
        self.assertEqual(self.run_tool(cap_id, "listOrders", {"status": "open"}).status, "ok")
        self.assertEqual(self.shop.requests[-1].url.query, b"status=open")

    def test_dot_segments_in_a_path_parameter_are_refused(self) -> None:
        cap_id = self.ready_plugin()
        for value in ("..", "."):
            self.assertEqual(self.run_tool(cap_id, "getOrder", {"orderId": value}).code, "tool.invalid_arguments", value)

    def test_the_credential_goes_only_to_the_plugins_host(self) -> None:
        cap_id = self.ready_plugin()
        self.run_tool(cap_id, "listOrders", {})
        self.assertEqual({r.url.host for r in self.shop.requests}, {PUBLIC})
        with self.session_factory.begin() as db:  # a spec that points elsewhere cannot make a call leave the base host
            row = db.get(ApiPlugin, UUID(cap_id))
            tools = json.loads(json.dumps(row.tools))
            tools[0]["binding"]["path"] = "//evil.example.com/steal"
            row.tools = tools
        self.run_tool(cap_id, "listOrders", {})  # the odd path just 404s on the base host: it cannot reach another one
        self.assertTrue(all(r.url.host == PUBLIC and r.headers["host"] == "api.shop.example.com" for r in self.shop.requests))
        self.assertTrue(self.shop.requests[-1].url.raw_path.startswith(b"/v1//evil.example.com"))

    def test_a_redirect_is_not_followed_and_an_auth_failure_is_reported(self) -> None:
        cap_id = self.ready_plugin()
        self.shop.fail = 302
        self.assertEqual(self.run_tool(cap_id, "listOrders", {}).code, "tool.redirect_blocked")
        self.shop.fail = None
        self.shop.key = "rotated"
        denied = self.run_tool(cap_id, "listOrders", {})
        self.assertEqual((denied.status, denied.code), ("error", "tool.auth_failed"))
        self.shop.fail = "down"
        self.assertEqual(self.run_tool(cap_id, "listOrders", {}).code, "tool.unreachable")

    # -- authentication ----------------------------------------------------------------------

    def test_bearer_authentication(self) -> None:
        self.shop.key = "bearer-tok-1"
        cap_id = self.ready_plugin(auth_type="bearer", secret="bearer-tok-1", api_key_header="")
        self.assertEqual(self.shop.requests[-1].headers["authorization"], "Bearer bearer-tok-1")
        self.assertEqual(self.get_cap(cap_id)["api"]["auth_type"], "bearer")

    def test_no_authentication_sends_no_credential_header(self) -> None:
        self.shop.key = None
        cap_id = self.make_plugin(auth_type="none", secret=None, api_key_header="").json()["id"]
        self.assertEqual(self.get_cap(cap_id)["api"]["secret_mask"], None)
        self.update_plugin(cap_id, selected_tools=["listOrders"])
        self.assertTrue(self.call_api(cap_id, "listOrders").json()["ok"])
        sent = self.shop.requests[-1].headers
        self.assertTrue("authorization" not in sent and "x-api-key" not in sent)

    def test_oauth_client_credentials_fetch_cache_and_refresh_the_token(self) -> None:
        extra = {"auth_type": "oauth_cc", "secret": None, "api_key_header": "", "oauth_token_url": TOKEN_URL, "oauth_client_id": "client-1", "oauth_client_secret": "cs-SENTINEL-9999", "oauth_scope": "orders.read"}
        cap_id = self.ready_plugin(**extra)
        self.assertEqual(self.shop.tokens_issued, 1)
        form = parse_qs(next(r for r in self.shop.requests if r.url.path == "/oauth/token").content.decode())
        self.assertEqual((form["client_id"], form["scope"]), (["client-1"], ["orders.read"]))
        self.assertEqual(self.run_tool(cap_id, "listOrders", {}).status, "ok")
        self.assertEqual(self.shop.tokens_issued, 1)  # cached, not fetched again
        with self.session_factory.begin() as db:  # about to expire → refreshed
            db.get(ApiPlugin, UUID(cap_id)).oauth_expires_at = datetime.now(UTC) + timedelta(seconds=10)
        self.assertEqual(self.run_tool(cap_id, "listOrders", {}).status, "ok")
        self.assertEqual(self.shop.tokens_issued, 2)
        with self.session_factory() as db:
            row = db.get(ApiPlugin, UUID(cap_id))
            self.assertTrue(row.oauth_access_encrypted)
            self.assertNotIn("tok-", row.oauth_access_encrypted)  # encrypted at rest
        self.assertEqual(self.get_cap(cap_id)["api"]["oauth_client_secret_mask"], "••••9999")
        self.assertNotIn("cs-SENTINEL", json.dumps(self.get_cap(cap_id)))

    def test_a_rejected_client_credential_is_an_error(self) -> None:
        extra = {"auth_type": "oauth_cc", "secret": None, "api_key_header": "", "oauth_token_url": TOKEN_URL, "oauth_client_id": "client-1", "oauth_client_secret": "wrong-secret", "oauth_scope": ""}
        cap_id = self.make_plugin(**extra).json()["id"]
        self.update_plugin(cap_id, selected_tools=["listOrders"])
        failed = self.call_api(cap_id, "listOrders").json()
        self.assertEqual((failed["ok"], failed["code"]), (False, "tool.auth_failed"))

    # -- lifecycle ---------------------------------------------------------------------------------

    def test_disable_enable_copy_delete(self) -> None:
        cap_id = self.ready_plugin()
        self.assertEqual(self.client.post(f"/api/v1/capabilities/{cap_id}/disable", headers=self.admin).json()["status"], "disabled")
        name = svc.tool_name(UUID(cap_id), "listOrders")
        with self.session_factory() as db:  # a disabled plugin is not callable, not even as a test
            self.assertEqual(invoke_tool(db, ToolContext(tenant_id=UUID(self.ids["tenant"]), allowed=frozenset({name}), allow_unavailable=True), name, {}).code, "tool.not_found")
        self.assertEqual(self.client.post(f"/api/v1/capabilities/{cap_id}/enable", headers=self.admin).json()["status"], "available")
        copy = self.client.post(f"/api/v1/capabilities/{cap_id}/copy", headers=self.admin)
        self.assertEqual(copy.status_code, 201, copy.text)
        data = copy.json()
        self.assertEqual((data["name"], data["status"], data["api"]["secret_mask"], data["api"]["last_test_ok"], len(data["api"]["tools"])), ("Shop API（副本）", "draft", "••••7890", False, 3))
        self.assertEqual(self.client.delete(f"/api/v1/capabilities/{cap_id}", headers=self.admin).status_code, 204)
        self.assertEqual(self.client.get(f"/api/v1/capabilities/{cap_id}", headers=self.admin).status_code, 404)
        counts = self.client.get("/api/v1/capabilities/summary", headers=self.admin).json()
        self.assertEqual(counts["api"], 1)

    def test_enabling_goes_through_the_approval_policy_like_mcp(self) -> None:
        with self.session_factory.begin() as db:
            ensure_policies(db, UUID(self.ids["tenant"]))["skill"].enabled = True
        cap_id = self.make_plugin().json()["id"]
        self.update_plugin(cap_id, selected_tools=["listOrders"])
        called = self.call_api(cap_id, "listOrders").json()
        self.assertEqual(called["capability"]["status"], "draft")  # waiting for approval, not enabled at once
        self.assertTrue(called["capability"]["pending_approval"] or called["capability"]["approval_warning"])

    # -- the catalogue and the agent's tools ----------------------------------------------------------

    def test_the_api_kind_is_mountable_and_lists_only_available_plugins(self) -> None:
        self.assertIn("api", MOUNT_RESOLVERS)
        self.assertIn("api", CANDIDATE_LISTERS)
        draft = self.make_plugin("Draft only").json()["id"]
        cap_id = self.ready_plugin("Ready one")
        items = self.client.get("/api/v1/capabilities/candidates?kind=api", headers=self.admin).json()["items"]
        self.assertEqual([(i["id"], i["name"]) for i in items], [(cap_id, "Ready one")])
        self.assertIn("3 个工具", items[0]["meta"]["summary"])
        self.assertNotEqual(draft, cap_id)
        types = {t["kind"]: t for t in self.client.get("/api/v1/capabilities/types", headers=self.admin).json()["items"]}
        self.assertEqual((types["api"]["mountable"], types["api"]["available"]), (True, 1))

    def test_a_mounted_plugin_offers_its_ticked_tools_to_the_agent(self) -> None:
        from beework.models.assistants import Assistant
        from beework.runtime.dsl import dsl_from_draft
        from beework.runtime.tool_runtime import ToolRuntime

        cap_id = self.ready_plugin(tools=("listOrders", "getOrder"))
        agent_id = self.create(self.admin, "Shop agent").json()["id"]
        current = self.client.get(f"/api/v1/assistants/{agent_id}", headers=self.admin).json()
        config = {**current["config"], "mounts": {**current["config"]["mounts"], "api": [cap_id]}}
        body = {
            "name": current["name"], "description": current["description"], "category": current["category"], "visibility": current["visibility"],
            "visible_org_ids": current["visible_org_ids"], "row_version": current["row_version"], "config": config,
        }
        self.assertEqual(self.client.put(f"/api/v1/assistants/{agent_id}", json=body, headers=self.admin).status_code, 200)
        with self.session_factory() as db:
            assistant = db.get(Assistant, UUID(agent_id))
            runtime = ToolRuntime(db.get_bind(), dsl_from_draft(assistant, db), tenant_id=assistant.tenant_id, run_id=None, assistant_id=assistant.id)
        self.assertEqual(sorted(t.spec.name for t in runtime.offered), sorted(svc.tool_name(UUID(cap_id), n) for n in ("listOrders", "getOrder")))
        self.assertTrue(all(t.alias.startswith("api_") for t in runtime.offered))
        by_name = {t.spec.name: t for t in runtime.offered}
        result, _ms = runtime.call(by_name[svc.tool_name(UUID(cap_id), "listOrders")].alias, {"status": "x"})
        self.assertEqual(result.status, "ok")

    def test_a_tool_that_was_not_ticked_disabled_or_of_another_tenant_is_not_callable(self) -> None:
        cap_id = self.ready_plugin(tools=("listOrders",))
        for tool in ("createOrder", "nope"):
            name = svc.tool_name(UUID(cap_id), tool)
            with self.session_factory() as db:
                self.assertEqual(invoke_tool(db, self.tool_ctx(name), name, {}).code, "tool.not_found", tool)
        name = svc.tool_name(UUID(cap_id), "listOrders")
        with self.session_factory() as db:
            self.assertEqual(invoke_tool(db, self.tool_ctx(), name, {}).code, "tool.not_mounted")
            self.assertEqual(invoke_tool(db, self.tool_ctx(name, tenant=self.ids["other"]), name, {}).code, "tool.not_found")
            record = db.scalar(select(ToolInvocation).where(ToolInvocation.source == "api").order_by(ToolInvocation.created_at.desc()))
            self.assertIsNotNone(record)
        self.assertEqual(len(PROVIDERS), len(set(PROVIDERS)))

    # -- permissions and isolation ----------------------------------------------------------------------

    def test_roles_without_permission_cannot_change_plugins(self) -> None:
        cap_id = self.ready_plugin()
        for email in ("viewer@example.com",):
            headers = self.login(email)
            self.assertEqual(self.client.get("/api/v1/capabilities?kind=api", headers=headers).status_code, 200)
            self.assertEqual(self.make_plugin("Nope", headers=headers).status_code, 403)
            self.assertEqual(self.update_plugin(cap_id, headers=headers, description="x").status_code, 403)
            self.assertEqual(self.call_api(cap_id, "listOrders", headers=headers).status_code, 403)
            self.assertEqual(self.client.post("/api/v1/capabilities/api/parse", json={"api_type": "rest", "spec_text": OPENAPI}, headers=headers).status_code, 403)
        data = self.get_cap(cap_id, headers=self.login("viewer@example.com"))
        self.assertEqual((data["can_edit"], data["can_delete"]), (False, False))

    def test_other_tenants_cannot_see_or_use_a_plugin(self) -> None:
        cap_id = self.ready_plugin()
        row_version = self.get_cap(cap_id)["row_version"]
        stranger = self.login("other@example.com", self.ids["other"])
        self.assertEqual(self.client.get("/api/v1/capabilities?kind=api", headers=stranger).json(), [])
        self.assertEqual(self.client.get(f"/api/v1/capabilities/{cap_id}", headers=stranger).status_code, 404)
        put = self.client.put(f"/api/v1/capabilities/api/{cap_id}", json={**self.plugin_body(), "row_version": row_version}, headers=stranger)
        self.assertEqual(put.status_code, 404)
        self.assertEqual(self.call_api(cap_id, "listOrders", headers=stranger).status_code, 404)

    def test_writes_require_the_csrf_token(self) -> None:
        self.assertEqual(self.client.post("/api/v1/capabilities/api", json=self.plugin_body(), headers={"X-Tenant-ID": self.ids["tenant"]}).status_code, 403)

    def test_the_plan_must_include_the_feature(self) -> None:
        from beework import entitlements

        original = entitlements.has_entitlement
        entitlements.has_entitlement = lambda db, tenant_id, key: False if key == "external_flows" else original(db, tenant_id, key)
        self.addCleanup(lambda: setattr(entitlements, "has_entitlement", original))
        refused = self.make_plugin()
        self.assertEqual((refused.status_code, refused.json()["code"]), (403, "plan.feature_locked"))

    # -- review findings (T-073) ---------------------------------------------------------------------

    def test_switching_the_auth_type_never_reuses_the_stored_credential_of_the_old_one(self) -> None:
        cap_id = self.make_plugin().json()["id"]  # api_key with a stored key
        refused = self.update_plugin(cap_id, auth_type="bearer", api_key_header="")
        self.assertEqual((refused.status_code, refused.json()["code"]), (422, "capability.token_required"))
        with self.session_factory() as db:
            self.assertEqual(db.get(ApiPlugin, UUID(cap_id)).auth_type, "api_key")  # nothing was saved
        switched = self.update_plugin(cap_id, auth_type="bearer", api_key_header="", secret="bearer-NEW-1111")
        self.assertEqual((switched.status_code, switched.json()["api"]["secret_mask"]), (200, "••••1111"))
        back = self.update_plugin(cap_id, auth_type="api_key", api_key_header="X-API-Key")  # and back again: also needs a new key
        self.assertEqual(back.json()["code"], "capability.token_required")
        same = self.update_plugin(cap_id, auth_type="bearer", api_key_header="", description="same auth keeps the secret")
        self.assertEqual((same.status_code, same.json()["api"]["secret_mask"]), (200, "••••1111"))

    def test_switching_to_oauth_needs_the_client_secret_again(self) -> None:
        extra = {"auth_type": "oauth_cc", "secret": None, "api_key_header": "", "oauth_token_url": TOKEN_URL, "oauth_client_id": "c1", "oauth_client_secret": "cs-SENTINEL-9999"}
        cap_id = self.make_plugin(**extra).json()["id"]
        self.assertEqual(self.update_plugin(cap_id, auth_type="none").status_code, 200)
        refused = self.update_plugin(cap_id, auth_type="oauth_cc", oauth_token_url=TOKEN_URL, oauth_client_id="c1")
        self.assertEqual(refused.json()["code"], "capability.oauth_secret_required")

    def test_a_test_call_needs_the_plan_feature_like_the_other_endpoints(self) -> None:
        from beework import entitlements

        cap_id = self.ready_plugin()
        original = entitlements.has_entitlement
        entitlements.has_entitlement = lambda db, tenant_id, key: False if key == "external_flows" else original(db, tenant_id, key)
        self.addCleanup(lambda: setattr(entitlements, "has_entitlement", original))
        before = len(self.shop.requests)
        refused = self.call_api(cap_id, "createOrder", {"body": {"sku": "A"}}, confirmed=True)
        self.assertEqual((refused.status_code, refused.json()["code"]), (403, "plan.feature_locked"))
        self.assertEqual(len(self.shop.requests), before)  # nothing left the platform

    def test_a_revoked_oauth_token_is_dropped_and_the_call_retried_once(self) -> None:
        extra = {"auth_type": "oauth_cc", "secret": None, "api_key_header": "", "oauth_token_url": TOKEN_URL, "oauth_client_id": "client-1", "oauth_client_secret": "cs-SENTINEL-9999", "oauth_scope": ""}
        cap_id = self.ready_plugin(**extra)
        self.assertEqual(self.shop.tokens_issued, 1)
        self.shop.tokens_issued += 1  # the provider rotated: the cached tok-1 is no longer accepted, long before its expiry
        result = self.run_tool(cap_id, "listOrders", {})
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.shop.tokens_issued, 3)  # exactly one fresh token was fetched
        self.assertEqual(self.shop.requests[-1].headers["authorization"], "Bearer tok-3")
        self.shop.reject_all = True  # a fresh token that is rejected too is reported, not retried forever
        before = len([r for r in self.shop.requests if r.url.path == "/oauth/token"])
        denied = self.run_tool(cap_id, "listOrders", {})
        self.assertEqual((denied.status, denied.code), ("error", "tool.auth_failed"))
        self.assertEqual(len([r for r in self.shop.requests if r.url.path == "/oauth/token"]) - before, 1)  # one refresh, then it stops

    def test_a_header_that_differs_only_in_case_cannot_override_the_credential(self) -> None:
        spec = OPENAPI.replace("        - {name: X-Region, in: header, schema: {type: string}}", "        - {name: x-api-key, in: header, schema: {type: string}}")
        cap_id = self.ready_plugin(spec_text=spec)
        result = self.run_tool(cap_id, "getOrder", {"orderId": "7", "x-api-key": "model-guess"})
        self.assertEqual(result.status, "ok")
        sent = self.shop.requests[-1].headers
        self.assertEqual(sent.get_list("x-api-key"), [SECRET])

    def test_a_parameter_called_body_and_a_request_body_both_reach_the_server(self) -> None:
        spec = ("openapi: 3.0.0\npaths:\n  /orders:\n    post:\n      operationId: createOrder\n      parameters:\n        - {name: body, in: query, schema: {type: string}}\n"
                "      requestBody:\n        required: true\n        content:\n          application/json:\n            schema: {type: object, properties: {sku: {type: string}}}\n")
        cap_id = self.make_plugin(spec_text=spec).json()["id"]
        self.assertEqual(self.update_plugin(cap_id, selected_tools=["createOrder"]).status_code, 200)
        called = self.call_api(cap_id, "createOrder", {"body": "q", "request_body": {"sku": "S1"}}, confirmed=True).json()
        self.assertTrue(called["ok"], called)
        sent = self.shop.requests[-1]
        self.assertEqual((sent.url.query, json.loads(sent.content)), (b"body=q", {"sku": "S1"}))

    def test_a_tool_stored_before_the_body_argument_was_named_still_works(self) -> None:
        cap_id = self.ready_plugin()
        with self.session_factory.begin() as db:
            row = db.get(ApiPlugin, UUID(cap_id))
            tools = json.loads(json.dumps(row.tools))
            for tool in tools:
                tool["binding"].pop("body_arg", None)
            row.tools = tools
        self.assertEqual(self.run_tool(cap_id, "createOrder", {"body": {"sku": "OLD"}}).status, "ok")
        self.assertEqual(json.loads(self.shop.requests[-1].content), {"sku": "OLD"})

    def test_the_list_carries_names_and_counts_and_the_detail_carries_the_schemas(self) -> None:
        cap_id = self.ready_plugin()
        listed = self.client.get("/api/v1/capabilities?kind=api", headers=self.admin).json()[0]["api"]
        self.assertEqual(([t["name"] for t in listed["tools"]], [t["input_schema"] for t in listed["tools"]]), (["listOrders", "createOrder", "getOrder"], [{}, {}, {}]))
        detail = self.get_cap(cap_id)["api"]
        self.assertIn("properties", detail["tools"][1]["input_schema"])

    def test_the_service_reuses_the_capability_helpers(self) -> None:
        from beework import capability_service

        self.assertIs(svc.host_of, capability_service.host_of)
        self.assertFalse(hasattr(svc, "mask_of"))

    def test_pointing_the_plugin_at_another_host_needs_the_credential_again(self) -> None:
        cap_id = self.make_plugin().json()["id"]
        refused = self.update_plugin(cap_id, base_url="https://attacker.example.com/v1")
        self.assertEqual((refused.status_code, refused.json()["code"]), (422, "capability.credential_reentry"))
        self.assertEqual(self.get_cap(cap_id)["api"]["host"], "api.shop.example.com")  # nothing was saved
        self.assertEqual(self.update_plugin(cap_id, base_url="https://api.shop.example.com/v2").status_code, 200)  # same host: fine
        moved = self.update_plugin(cap_id, base_url="https://api2.shop.example.com/v1", secret="key-NEW-HOST-0000")
        self.assertEqual((moved.status_code, moved.json()["api"]["secret_mask"]), (200, "••••0000"))

    def test_moving_the_oauth_token_url_to_another_host_needs_the_client_secret_again(self) -> None:
        extra = {"auth_type": "oauth_cc", "secret": None, "api_key_header": "", "oauth_token_url": TOKEN_URL, "oauth_client_id": "c1", "oauth_client_secret": "cs-SENTINEL-9999"}
        cap_id = self.make_plugin(**extra).json()["id"]
        refused = self.update_plugin(cap_id, oauth_token_url="https://attacker.example.com/token")
        self.assertEqual((refused.status_code, refused.json()["code"]), (422, "capability.credential_reentry"))
        self.assertEqual(self.update_plugin(cap_id, oauth_token_url="https://auth.shop.example.com/v2/token").status_code, 200)


if __name__ == "__main__":
    unittest.main()
