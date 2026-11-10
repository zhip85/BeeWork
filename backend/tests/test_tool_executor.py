"""T-071: the controlled tool executor and its safe HTTP client."""

import json
import logging
import unittest
from uuid import UUID, uuid4

import httpx
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from beework.config import settings
from beework.models import Base, Tenant
from beework.models.tools import ToolInvocation
from beework.tools import executor
from beework.tools.executor import (
    PROVIDERS,
    ToolConfirmationRequired,
    ToolContext,
    ToolError,
    ToolOutput,
    ToolSpec,
    invoke_tool,
)
from beework.tools.http import ToolHttpError, safe_request
from beework.tools.schema import validate

PUBLIC = "93.184.216.34"
SECRET = "sk-live-SENTINEL-0123456789"


def resolver_for(mapping: dict[str, list[str]], calls: list[str] | None = None):
    def resolve(hostname: str) -> list[str]:
        if calls is not None:
            calls.append(hostname)
        return mapping[hostname]
    return resolve


class SafeHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original = settings.allow_insecure_tool_urls
        settings.allow_insecure_tool_urls = False
        self.addCleanup(setattr, settings, "allow_insecure_tool_urls", self.original)
        self.seen: list[httpx.Request] = []

    def transport(self, handler=None) -> httpx.MockTransport:
        def respond(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            return handler(request) if handler else httpx.Response(200, json={"ok": True})
        return httpx.MockTransport(respond)

    def get(self, url: str, mapping: dict[str, list[str]] | None = None, **kwargs):
        resolver = resolver_for(mapping or {"tools.example.com": [PUBLIC]})
        return safe_request("GET", url, resolver=resolver, transport=kwargs.pop("transport", None) or self.transport(), **kwargs)

    def test_a_public_https_host_is_called_on_the_validated_address(self) -> None:
        result = self.get("https://tools.example.com/v1/things?x=1")
        self.assertEqual((result.status, json.loads(result.body)), (200, {"ok": True}))
        request = self.seen[0]
        self.assertEqual((request.url.host, request.headers["host"], request.url.path), (PUBLIC, "tools.example.com", "/v1/things"))
        self.assertEqual(request.extensions["sni_hostname"], "tools.example.com")  # TLS still validates the real name

    def test_internal_addresses_are_refused_by_name_and_by_literal(self) -> None:
        for host, addresses in {
            "a.example.com": ["10.0.0.5"], "b.example.com": ["169.254.169.254"], "c.example.com": ["::1"],
            "d.example.com": ["127.0.0.1"], "e.example.com": ["192.168.1.9"], "f.example.com": ["::ffff:10.0.0.1"],
            "g.example.com": ["0.0.0.0"],
        }.items():
            with self.assertRaises(ToolHttpError, msg=host) as caught:
                self.get(f"https://{host}/", {host: addresses})
            self.assertEqual(caught.exception.code, "tool.forbidden_address")
        for literal in ("https://169.254.169.254/latest/meta-data", "https://[::1]/", "https://10.1.2.3:8443/x"):
            with self.assertRaises(ToolHttpError) as caught:
                self.get(literal)
            self.assertEqual(caught.exception.code, "tool.forbidden_address")
        self.assertEqual(self.seen, [])  # nothing was sent

    def test_one_internal_address_among_public_ones_rejects_the_whole_name(self) -> None:
        with self.assertRaises(ToolHttpError):
            self.get("https://tools.example.com/", {"tools.example.com": [PUBLIC, "10.0.0.7"]})
        self.assertEqual(self.seen, [])

    def test_dns_rebinding_cannot_reach_the_inside(self) -> None:
        """The name is resolved once; its second answer (internal) is never consulted, the connection stays pinned."""
        answers = iter([[PUBLIC], ["10.0.0.9"], ["10.0.0.9"]])
        calls: list[str] = []

        def rebinding(hostname: str) -> list[str]:
            calls.append(hostname)
            return next(answers)

        safe_request("GET", "https://tools.example.com/x", resolver=rebinding, transport=self.transport())
        self.assertEqual(len(calls), 1)
        self.assertEqual({request.url.host for request in self.seen}, {PUBLIC})

    def test_scheme_credentials_and_unresolvable_names_are_refused(self) -> None:
        cases = [
            ("http://tools.example.com/", "tool.invalid_url"),
            ("ftp://tools.example.com/", "tool.invalid_url"),
            ("https://user:pw@tools.example.com/", "tool.invalid_url"),
            ("https:///nohost", "tool.invalid_url"),
        ]
        for url, code in cases:
            with self.assertRaises(ToolHttpError, msg=url) as caught:
                self.get(url)
            self.assertEqual(caught.exception.code, code)

        def broken(_hostname: str) -> list[str]:
            raise OSError("no such host")

        with self.assertRaises(ToolHttpError) as caught:
            safe_request("GET", "https://nowhere.example.com/", resolver=broken, transport=self.transport())
        self.assertEqual(caught.exception.code, "tool.unresolvable")

    def test_redirects_are_never_followed(self) -> None:
        handler = lambda request: httpx.Response(302, headers={"location": "https://evil.example.org/"})  # noqa: E731
        with self.assertRaises(ToolHttpError) as caught:
            self.get("https://tools.example.com/", transport=self.transport(handler))
        self.assertEqual(caught.exception.code, "tool.redirect_blocked")
        self.assertEqual(len(self.seen), 1)  # only the first hop was made

    def test_a_response_over_the_limit_is_an_error_not_a_silent_cut(self) -> None:
        big = lambda request: httpx.Response(200, content=b"x" * 2048)  # noqa: E731
        with self.assertRaises(ToolHttpError) as caught:
            self.get("https://tools.example.com/", transport=self.transport(big), max_bytes=1024)
        self.assertEqual(caught.exception.code, "tool.response_too_large")
        declared = lambda request: httpx.Response(200, content=b"ok", headers={"content-length": "999999999"})  # noqa: E731
        with self.assertRaises(ToolHttpError):
            self.get("https://tools.example.com/", transport=self.transport(declared))
        self.assertEqual(self.get("https://tools.example.com/", max_bytes=1024).status, 200)

    def test_timeouts_become_a_tool_error(self) -> None:
        def slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)
        with self.assertRaises(ToolHttpError) as caught:
            self.get("https://tools.example.com/", transport=self.transport(slow))
        self.assertEqual(caught.exception.code, "tool.timeout")

    def test_a_plugin_may_be_limited_to_its_own_host(self) -> None:
        self.assertEqual(self.get("https://tools.example.com/a", allowed_host="TOOLS.example.com").status, 200)
        with self.assertRaises(ToolHttpError) as caught:
            self.get("https://other.example.com/a", {"other.example.com": [PUBLIC]}, allowed_host="tools.example.com")
        self.assertEqual(caught.exception.code, "tool.host_not_allowed")

    def test_a_connection_failure_falls_back_to_the_next_validated_address(self) -> None:
        second = "93.184.216.35"

        def handler(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            if request.url.host == PUBLIC:
                raise httpx.ConnectError("down", request=request)
            return httpx.Response(200, json={"ok": True})

        result = safe_request("GET", "https://tools.example.com/", resolver=resolver_for({"tools.example.com": [PUBLIC, second]}),
                              transport=httpx.MockTransport(handler))
        self.assertEqual((result.status, result.address), (200, second))

    def test_the_development_switch_allows_http_and_local_addresses(self) -> None:
        settings.allow_insecure_tool_urls = True
        self.assertEqual(self.get("http://localhost:9000/x", {"localhost": ["127.0.0.1"]}).status, 200)
        self.assertEqual(self.seen[0].url.port, 9000)


class RealSocketTests(unittest.TestCase):
    """No mock transport: a real connection goes to the pinned address and carries the original Host header."""

    def test_the_connection_goes_to_the_pinned_address_with_the_original_host(self) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        seen: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                seen["host"], seen["path"] = self.headers["Host"], self.path
                body = b'{"hello": "world"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        original = settings.allow_insecure_tool_urls
        settings.allow_insecure_tool_urls = True
        self.addCleanup(setattr, settings, "allow_insecure_tool_urls", original)
        port = server.server_address[1]
        calls: list[str] = []
        result = safe_request("GET", f"http://tools.example.com:{port}/v1/x?q=1", params={"p": "2"},
                              resolver=resolver_for({"tools.example.com": ["127.0.0.1"]}, calls))
        self.assertEqual((result.status, json.loads(result.body), result.address), (200, {"hello": "world"}, "127.0.0.1"))
        self.assertEqual((seen["host"], seen["path"]), (f"tools.example.com:{port}", "/v1/x?q=1&p=2"))
        self.assertEqual(calls, ["tools.example.com"])


class SchemaTests(unittest.TestCase):
    def test_the_supported_subset(self) -> None:
        schema = {
            "type": "object", "required": ["city"], "additionalProperties": False,
            "properties": {
                "city": {"type": "string", "minLength": 2, "maxLength": 5},
                "days": {"type": "integer", "minimum": 1, "maximum": 7},
                "unit": {"enum": ["c", "f"]},
                "tags": {"type": "array", "maxItems": 2, "items": {"type": "string"}},
            },
        }
        self.assertEqual(validate({"city": "Paris", "days": 3, "unit": "c", "tags": ["a"]}, schema), [])
        self.assertEqual(len(validate({}, schema)), 1)
        for bad in ({"city": "P"}, {"city": "Paris", "days": 0}, {"city": "Paris", "days": True}, {"city": "Paris", "unit": "k"},
                    {"city": "Paris", "tags": ["a", "b", "c"]}, {"city": "Paris", "tags": [1]}, {"city": "Paris", "extra": 1}, {"city": 3}):
            self.assertTrue(validate(bad, schema), bad)
        self.assertEqual(validate("anything", None), [])


class FakeProvider:
    """A stand-in for MCP / API providers: knows two tools of tenant A and records what it executed."""

    def __init__(self, tenant_id: UUID) -> None:
        self.tenant_id = tenant_id
        self.executed: list[tuple[str, dict]] = []
        self.specs = {
            "api:weather": ToolSpec("api:weather", "api", uuid4(), {"type": "object", "required": ["city"], "properties": {"city": {"type": "string"}}}),
            "api:refund": ToolSpec("api:refund", "api", uuid4(), {"type": "object", "properties": {"amount": {"type": "number"}}}, writes=True, description="Refund an order"),
            "api:boom": ToolSpec("api:boom", "api", uuid4()),
            "api:leaky": ToolSpec("api:leaky", "api", uuid4()),
            "api:down": ToolSpec("api:down", "api", uuid4()),
        }

    def lookup(self, db, ctx, name):
        return self.specs.get(name) if ctx.tenant_id == self.tenant_id else None

    def execute(self, db, ctx, spec, args):
        ctx.register_secret(SECRET)  # a provider decrypts its credential and registers it
        self.executed.append((spec.name, args))
        if spec.name == "api:boom":
            raise RuntimeError(f"connection string with {SECRET} exploded")
        if spec.name == "api:leaky":
            return ToolOutput(f"echo: Authorization Bearer {SECRET}", response_bytes=40, request_bytes=5)
        if spec.name == "api:down":
            raise ToolError("tool.http_500", f"upstream said {SECRET}")
        return ToolOutput(f"sunny in {args.get('city', '?')}", response_bytes=12, request_bytes=20)


class ExecutorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

        @event.listens_for(cls.engine, "connect")
        def fk(connection, _record) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        cls.factory = sessionmaker(bind=cls.engine, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        self.db = self.factory()
        self.addCleanup(self.db.close)
        tenant, other = Tenant(name="Acme", slug="acme"), Tenant(name="Other", slug="other")
        self.db.add_all([tenant, other])
        self.db.commit()
        self.tenant_id, self.other_id = tenant.id, other.id
        self.provider = FakeProvider(self.tenant_id)
        self.saved = dict(PROVIDERS)
        PROVIDERS["api"] = self.provider
        self.addCleanup(lambda: (PROVIDERS.clear(), PROVIDERS.update(self.saved)))
        self.run_id = uuid4()

    def ctx(self, allowed=("api:weather", "api:refund", "api:boom", "api:leaky", "api:down"), **kwargs) -> ToolContext:
        return ToolContext(tenant_id=kwargs.pop("tenant_id", self.tenant_id), allowed=frozenset(allowed), run_id=self.run_id, **kwargs)

    def rows(self) -> list[ToolInvocation]:
        return list(self.db.scalars(select(ToolInvocation).order_by(ToolInvocation.created_at, ToolInvocation.call_index)))

    # -- authorization -------------------------------------------------------------

    def test_a_mounted_tool_runs_and_is_recorded_without_bodies(self) -> None:
        result = invoke_tool(self.db, self.ctx(), "api:weather", {"city": "Paris"})
        self.assertEqual((result.status, result.content), ("ok", "sunny in Paris"))
        (row,) = self.rows()
        self.assertEqual((row.status, row.tool_name, row.source, row.is_write, row.request_bytes, row.response_bytes),
                         ("ok", "api:weather", "api", False, 20, 12))
        self.assertEqual(row.run_id, self.run_id)
        self.assertNotIn("Paris", json.dumps({c.name: str(getattr(row, c.name)) for c in ToolInvocation.__table__.columns}))

    def test_a_tool_that_is_not_mounted_is_refused_and_not_executed(self) -> None:
        result = invoke_tool(self.db, self.ctx(allowed=("api:refund",)), "api:weather", {"city": "Paris"})
        self.assertEqual((result.status, result.code), ("error", "tool.not_mounted"))
        self.assertEqual(self.provider.executed, [])
        self.assertEqual([(r.status, r.error_code) for r in self.rows()], [("denied", "tool.not_mounted")])

    def test_unknown_tools_sources_and_other_tenants_capabilities_are_not_found(self) -> None:
        for name, tenant in (("api:nope", self.tenant_id), ("mcp:x", self.tenant_id), ("nonsense", self.tenant_id), ("api:weather", self.other_id)):
            result = invoke_tool(self.db, self.ctx(allowed=(name,), tenant_id=tenant), name, {"city": "Paris"})
            self.assertEqual((result.status, result.code), ("error", "tool.not_found"), name)
        self.assertEqual(self.provider.executed, [])

    def test_the_call_depth_limit(self) -> None:
        self.assertEqual(invoke_tool(self.db, self.ctx(depth=executor.MAX_CALL_DEPTH), "api:weather", {"city": "A"}).status, "ok")
        deep = invoke_tool(self.db, self.ctx(depth=executor.MAX_CALL_DEPTH + 1), "api:weather", {"city": "A"})
        self.assertEqual((deep.status, deep.code), ("error", "tool.depth_exceeded"))

    def test_arguments_are_checked_against_the_schema_before_anything_runs(self) -> None:
        result = invoke_tool(self.db, self.ctx(), "api:weather", {})
        self.assertEqual((result.status, result.code), ("error", "tool.invalid_arguments"))
        self.assertIn("city", result.content)
        self.assertEqual(self.provider.executed, [])

    # -- writes --------------------------------------------------------------------

    def test_a_write_interrupts_for_confirmation_then_resumes_exactly_once(self) -> None:
        ctx = self.ctx()
        with self.assertRaises(ToolConfirmationRequired) as paused:
            invoke_tool(self.db, ctx, "api:refund", {"amount": 12})
        payload = paused.exception.payload
        self.assertEqual((payload["tool"], payload["description"]), ("api:refund", "Refund an order"))
        self.assertEqual(self.provider.executed, [])  # nothing ran while waiting
        resumed = invoke_tool(self.db, ctx, "api:refund", {"amount": 12}, confirmed=True, call_index=payload["call_index"])
        self.assertEqual((resumed.status, len(self.provider.executed)), ("ok", 1))
        (row,) = self.rows()
        self.assertEqual((row.is_write, row.idempotency_key), (True, f"{self.run_id}:{payload['call_index']}"))
        again = invoke_tool(self.db, ctx, "api:refund", {"amount": 12}, confirmed=True, call_index=payload["call_index"])
        self.assertTrue(again.deduplicated)  # the same call is never executed twice
        self.assertEqual(len(self.provider.executed), 1)

    def test_a_declined_write_is_cancelled_without_executing(self) -> None:
        ctx = self.ctx()
        with self.assertRaises(ToolConfirmationRequired) as paused:
            invoke_tool(self.db, ctx, "api:refund", {"amount": 1})
        result = invoke_tool(self.db, ctx, "api:refund", {"amount": 1}, confirmed=False, call_index=paused.exception.payload["call_index"])
        self.assertEqual((result.status, result.content, result.code), ("cancelled", "用户取消", "tool.cancelled"))
        self.assertEqual(self.provider.executed, [])
        self.assertEqual([r.status for r in self.rows()], ["cancelled"])
        # a later confirmed attempt of the same call is still possible
        self.assertEqual(invoke_tool(self.db, ctx, "api:refund", {"amount": 1}, confirmed=True, call_index=paused.exception.payload["call_index"]).status, "ok")

    def test_each_call_gets_its_own_index_and_idempotency_key(self) -> None:
        ctx = self.ctx()
        invoke_tool(self.db, ctx, "api:weather", {"city": "A"})
        with self.assertRaises(ToolConfirmationRequired) as paused:
            invoke_tool(self.db, ctx, "api:refund", {})
        self.assertEqual(paused.exception.payload["call_index"], 2)
        self.assertEqual(paused.exception.payload["idempotency_key"], f"{self.run_id}:2")

    # -- credentials and failures ----------------------------------------------------

    def test_failures_come_back_as_results_and_never_carry_credentials(self) -> None:
        with self.assertLogs("beework.tools.executor", level=logging.WARNING) as logs:
            boom = invoke_tool(self.db, self.ctx(), "api:boom", {})
        self.assertEqual((boom.status, boom.code, boom.content), ("error", "tool.failed", "工具执行失败"))
        down = invoke_tool(self.db, self.ctx(), "api:down", {})
        self.assertEqual((down.status, down.code), ("error", "tool.http_500"))
        leaky = invoke_tool(self.db, self.ctx(), "api:leaky", {})
        self.assertEqual(leaky.status, "ok")
        everything = json.dumps([boom.content, down.content, leaky.content, logs.output], ensure_ascii=False)
        self.assertNotIn(SECRET, everything)
        self.assertIn("***", leaky.content)
        stored = json.dumps([{c.name: str(getattr(r, c.name)) for c in ToolInvocation.__table__.columns} for r in self.rows()])
        self.assertNotIn(SECRET, stored)

    def test_output_passes_the_guard_before_the_model_sees_it(self) -> None:
        ctx = self.ctx(guard_output=lambda text: text.replace("Paris", "[城市]"))
        self.assertEqual(invoke_tool(self.db, ctx, "api:weather", {"city": "Paris"}).content, "sunny in [城市]")

    def test_results_are_capped_before_going_back_to_the_model(self) -> None:
        class Big(FakeProvider):
            def execute(self, db, ctx, spec, args):
                return ToolOutput("y" * (executor.MAX_RESULT_CHARS + 500))
        PROVIDERS["api"] = Big(self.tenant_id)
        self.assertEqual(len(invoke_tool(self.db, self.ctx(), "api:weather", {"city": "A"}).content), executor.MAX_RESULT_CHARS)

    def test_http_errors_from_the_safe_client_are_shown_with_their_codes(self) -> None:
        class Blocked(FakeProvider):
            def execute(self, db, ctx, spec, args):
                raise ToolHttpError("tool.forbidden_address", "工具地址不得指向内网、回环或保留地址")
        PROVIDERS["api"] = Blocked(self.tenant_id)
        result = invoke_tool(self.db, self.ctx(), "api:weather", {"city": "A"})
        self.assertEqual((result.status, result.code), ("error", "tool.forbidden_address"))

    def test_the_span_wraps_the_call(self) -> None:
        names: list[str] = []
        from contextlib import contextmanager

        @contextmanager
        def span(name: str):
            names.append(name)
            yield

        invoke_tool(self.db, self.ctx(span=span), "api:weather", {"city": "A"})
        self.assertEqual(names, ["tool.api:weather"])


if __name__ == "__main__":
    unittest.main()
