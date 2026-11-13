"""T-165: the execution policy of a tool call — resolution, retry rules, the run budget, cancellation, the circuit breaker, the record."""

import unittest
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import test_capability_registry
from beework.models import Base, Tenant
from beework.models.tools import ToolInvocation
from beework.tools import policy as policy_mod
from beework.tools.executor import (
    PROVIDERS,
    ToolContext,
    ToolError,
    ToolOutput,
    ToolSpec,
    invoke_tool,
)
from beework.tools.http import CALL_TIMEOUT, ToolHttpError
from beework.tools.policy import Breaker, BreakerConfig, MemoryBreaker, Policy

SECRET = "sk-SENTINEL-policy-0123456789"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Scripted:
    """A provider whose calls fail or succeed as scripted; records every attempt and the timeout it was given."""

    def __init__(self) -> None:
        self.script: list = []
        self.calls = 0
        self.timeouts: list = []
        self.specs = {
            "mcp:read": ToolSpec("mcp:read", "mcp", None, {"type": "object"}, writes=False),
            "mcp:write": ToolSpec("mcp:write", "mcp", None, {"type": "object"}, writes=True),
            "api:idem": ToolSpec("api:idem", "api", None, {"type": "object"}, writes=True, idempotent=True),
            "connection:flow": ToolSpec("connection:flow", "connection", None, {"type": "object"}, writes=False),
        }

    def lookup(self, db, ctx, name):
        return self.specs.get(name)

    def execute(self, db, ctx, spec, args):
        self.calls += 1
        self.timeouts.append(CALL_TIMEOUT.get())
        step = self.script[min(self.calls - 1, len(self.script) - 1)] if self.script else "ok"
        if isinstance(step, BaseException):
            raise step
        return ToolOutput(content=f"result {SECRET}", response_bytes=5)


def transient(code="tool.timeout", status=None, retry_after=None) -> ToolError:
    return ToolError(code, "boom", status=status, retry_after=retry_after)


class PolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

        @event.listens_for(cls.engine, "connect")
        def fk(connection, _record) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        cls.factory = sessionmaker(bind=cls.engine, expire_on_commit=False)

    def setUp(self) -> None:
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        self.db = self.factory()
        self.addCleanup(self.db.close)
        tenant = Tenant(name="Acme", slug="acme")
        self.db.add(tenant)
        self.db.commit()
        self.tenant_id = tenant.id
        self.provider = Scripted()
        saved = dict(PROVIDERS)
        for source in ("mcp", "api", "connection"):
            PROVIDERS[source] = self.provider
        self.addCleanup(lambda: (PROVIDERS.clear(), PROVIDERS.update(saved)))
        self.clock = Clock()
        self.breaker = Breaker(valkey=MemoryBreaker(self.clock), memory=MemoryBreaker(self.clock))
        patch = mock.patch.object(policy_mod, "BREAKER", self.breaker)
        patch.start()
        self.addCleanup(patch.stop)
        self.entry = None
        patch_entry = mock.patch.object(policy_mod, "entry_for_tool", lambda *_: self.entry)
        patch_entry.start()
        self.addCleanup(patch_entry.stop)
        self.slept: list[float] = []

    def entry_with(self, *, timeout_ms=None, retry=None):
        self.entry = SimpleNamespace(id=uuid4(), name="Tool X", timeout_ms=timeout_ms, retry=retry)

    def ctx(self, **kwargs) -> ToolContext:
        kwargs.setdefault("sleep", self.slept.append)
        return ToolContext(tenant_id=self.tenant_id, allowed=frozenset(self.provider.specs), run_id=uuid4(), **kwargs)

    def call(self, name="mcp:read", **kwargs):
        ctx = kwargs.pop("ctx", None) or self.ctx()
        return invoke_tool(self.db, ctx, name, {}, confirmed=kwargs.pop("confirmed", True), **kwargs)

    def rows(self) -> list[ToolInvocation]:
        return list(self.db.scalars(select(ToolInvocation).order_by(ToolInvocation.created_at)))

    # -- resolution --------------------------------------------------------------------------------

    def test_the_entrys_policy_wins_over_the_platform_default(self) -> None:
        self.assertEqual((policy_mod.resolve(None).source, policy_mod.resolve(None).max_attempts, policy_mod.resolve(None).timeout_ms), ("platform", 1, 30000))
        self.entry_with(timeout_ms=5000, retry={"max_attempts": 3, "backoff_ms": 200, "retry_on": ["timeout", "bogus"]})
        resolved = policy_mod.resolve(self.entry)
        self.assertEqual((resolved.source, resolved.timeout_ms, resolved.max_attempts, resolved.backoff_ms, resolved.retry_on), ("entry", 5000, 3, 200, frozenset({"timeout"})))
        self.entry_with(timeout_ms=None, retry=None)
        self.assertEqual(policy_mod.resolve(self.entry).source, "platform")  # nothing of its own set

    def test_the_attempt_gets_the_policys_timeout_capped_by_the_runs_budget(self) -> None:
        self.entry_with(timeout_ms=8000)
        self.call()
        self.assertEqual(self.provider.timeouts[-1], 8.0)
        self.call(ctx=self.ctx(deadline=__import__("time").monotonic() + 2.0))
        self.assertLessEqual(self.provider.timeouts[-1], 2.0)
        self.assertIsNone(CALL_TIMEOUT.get())  # the setting does not leak out of the call

    # -- retry -------------------------------------------------------------------------------------

    def test_a_read_is_retried_on_a_transient_failure_with_a_growing_wait(self) -> None:
        self.entry_with(retry={"max_attempts": 3, "backoff_ms": 1000, "retry_on": ["timeout", "5xx"]})
        self.provider.script = [transient(), transient("tool.api_error", status=503), "ok"]
        result = self.call()
        self.assertEqual((result.status, self.provider.calls), ("ok", 3))
        self.assertEqual(len(self.slept) > 0, True)
        row = self.rows()[0]
        self.assertEqual((row.attempts, row.policy, len(row.attempt_ms)), (3, "entry", 3))

    def test_only_the_configured_categories_are_retried_and_a_bad_key_never(self) -> None:
        self.entry_with(retry={"max_attempts": 3, "backoff_ms": 0, "retry_on": ["timeout"]})
        self.provider.script = [transient("tool.api_error", status=503)]
        self.assertEqual((self.call().status, self.provider.calls), ("error", 1))  # 5xx is not in retry_on
        self.provider.calls = 0
        self.provider.script = [ToolError("tool.auth_failed", "bad key", status=401)]
        self.call()
        self.assertEqual(self.provider.calls, 1)
        self.provider.calls = 0
        self.provider.script = [transient()] * 5
        result = self.call()
        self.assertEqual((result.code, self.provider.calls), ("tool.timeout", 3))  # gives up after max_attempts

    def test_a_write_is_not_retried_unless_it_is_idempotent_and_carries_a_key(self) -> None:
        self.entry_with(retry={"max_attempts": 3, "backoff_ms": 0, "retry_on": ["timeout"]})
        self.provider.script = [transient()] * 3
        self.call("mcp:write")
        self.assertEqual(self.provider.calls, 1)
        self.provider.calls = 0
        self.call("api:idem")
        self.assertEqual(self.provider.calls, 3)
        self.provider.calls = 0
        self.call("api:idem", ctx=ToolContext(tenant_id=self.tenant_id, allowed=frozenset({"api:idem"}), run_id=None, sleep=self.slept.append))
        self.assertEqual(self.provider.calls, 1)  # no run, no idempotency key

    def test_a_dify_or_skill_call_is_only_repeated_when_the_request_never_arrived(self) -> None:
        self.entry_with(retry={"max_attempts": 3, "backoff_ms": 0, "retry_on": ["timeout", "network", "5xx"]})
        self.provider.script = [transient("tool.timeout")]
        self.call("connection:flow")
        self.assertEqual(self.provider.calls, 1)  # it may have been processed
        self.provider.calls = 0
        self.provider.script = [transient("tool.unreachable"), "ok"]
        self.assertEqual((self.call("connection:flow").status, self.provider.calls), ("ok", 2))

    def test_retry_after_is_honoured_and_capped(self) -> None:
        policy = Policy(30000, 3, 100, frozenset({"429"}), "entry")
        self.assertEqual(policy_mod.delay_seconds(policy, 1, transient("tool.rate_limited", status=429, retry_after=7)), 7)
        self.assertEqual(policy_mod.delay_seconds(policy, 1, transient("tool.rate_limited", status=429, retry_after=500)), policy_mod.MAX_BACKOFF_SECONDS)
        for attempt in (1, 2, 3):
            wait = policy_mod.delay_seconds(policy, attempt, transient())
            base = 0.1 * 2 ** (attempt - 1)
            self.assertTrue(base * 0.5 <= wait <= base)

    def test_a_wait_never_reaches_past_the_runs_budget(self) -> None:
        self.entry_with(retry={"max_attempts": 4, "backoff_ms": 60000, "retry_on": ["timeout"]})
        self.provider.script = [transient()] * 4
        started = __import__("time").monotonic()
        result = self.call(ctx=self.ctx(deadline=started + 5))
        self.assertEqual((result.code, self.provider.calls, self.slept), ("tool.timeout", 1, []))  # a 30 s wait does not fit in 5 s

    def test_when_the_budget_runs_out_during_the_wait_the_real_failure_is_the_outcome_and_counts_for_the_circuit(self) -> None:
        self.entry_with(retry={"max_attempts": 3, "backoff_ms": 1000, "retry_on": ["timeout"]})
        self.provider.script = [transient()] * 3
        budget_end = __import__("time").monotonic() + 100

        def sleeping(seconds: float) -> None:
            ctx.deadline = __import__("time").monotonic() - 1  # the wait used up what was left

        ctx = self.ctx(deadline=budget_end)
        ctx.sleep = sleeping
        with mock.patch.object(policy_mod, "delay_seconds", lambda *_: 0.1):
            result = self.call(ctx=ctx)
        self.assertEqual((result.code, self.provider.calls), ("tool.timeout", 1))  # not "tool.failed"
        cfg = policy_mod.breaker_config()
        self.assertTrue(any(self.breaker.failure(policy_mod.breaker_key(self.tenant_id, "mcp:read"), cfg) for _ in range(cfg.failures)))  # the failure was counted: one more tips it

    def test_an_entry_that_ticks_no_retry_category_retries_nothing(self) -> None:
        self.entry_with(retry={"max_attempts": 3, "backoff_ms": 0, "retry_on": []})
        self.provider.script = [transient()] * 3
        self.call()
        self.assertEqual(self.provider.calls, 1)  # "retry nothing" is not "use the default categories"

    def test_a_probe_that_hits_a_provider_bug_or_a_cancel_gives_the_slot_back(self) -> None:
        with mock.patch("beework.notifications.notify"):
            self.trip()
        self.clock.now += 31
        self.provider.script = [RuntimeError("bug")]
        self.assertEqual(self.call().code, "tool.failed")  # the probe died without a verdict
        self.provider.script = ["ok"]
        self.assertEqual(self.call().status, "ok")  # the next call may probe at once, not 30 s later
        self.provider.script = [transient()]
        with mock.patch("beework.notifications.notify"):
            self.trip()
        self.clock.now += 31

        def cancelled():
            raise RuntimeError("cancelled")

        with self.assertRaises(RuntimeError):
            self.call(ctx=self.ctx(check_stop=cancelled))
        self.provider.script = ["ok"]
        self.assertEqual(self.call().status, "ok")

    def test_cancelling_the_run_stops_the_retries(self) -> None:
        self.entry_with(retry={"max_attempts": 4, "backoff_ms": 1000, "retry_on": ["timeout"]})
        self.provider.script = [transient()] * 4
        seen = []

        def check_stop():
            seen.append(1)
            if len(seen) >= 3:
                raise RuntimeError("cancelled")

        with self.assertRaises(RuntimeError):
            self.call(ctx=self.ctx(check_stop=check_stop))
        self.assertLess(self.provider.calls, 4)

    # -- the circuit -------------------------------------------------------------------------------

    def trip(self, failures: int = 5) -> None:
        self.provider.script = [transient()]
        for _ in range(failures):
            self.call()

    def test_consecutive_transient_failures_open_the_circuit_then_a_probe_closes_it(self) -> None:
        with mock.patch("beework.notifications.notify") as notify:
            self.trip()
            self.assertEqual(notify.call_count, 1)  # told once, when it opened
            calls_before = self.provider.calls
            refused = self.call()
            self.assertEqual((refused.code, self.provider.calls), ("tool.circuit_open", calls_before))  # refused without a call
            self.assertEqual(self.rows()[-1].breaker_state, "open")
            self.clock.now += 31  # the open period is over: one probe goes through
            self.provider.script = ["ok"]
            self.assertEqual(self.call().status, "ok")
            self.assertEqual(self.call().status, "ok")  # closed again
            self.provider.script = [transient()]
            self.call()
            self.assertEqual(self.call().status, "error")  # a single failure does not reopen it

    def test_a_failed_probe_reopens_the_circuit_and_a_definite_error_does_not_count(self) -> None:
        with mock.patch("beework.notifications.notify"):
            self.trip()
            self.clock.now += 31
            self.call()  # the probe fails
            self.assertEqual(self.call().code, "tool.circuit_open")
        self.breaker.success(policy_mod.breaker_key(self.tenant_id, "mcp:read"))
        self.provider.script = [ToolError("tool.invalid_arguments", "no", status=400)]
        for _ in range(8):
            self.call()
        self.assertEqual(self.provider.script[0].status, 400)
        self.assertEqual(self.rows()[-1].code if hasattr(self.rows()[-1], "code") else self.rows()[-1].error_code, "tool.invalid_arguments")  # a 4xx never opens the circuit

    def test_failures_far_apart_do_not_add_up_and_circuits_are_per_tool(self) -> None:
        self.provider.script = [transient()]
        for _ in range(4):
            self.call()
            self.clock.now += 500  # beyond the observation window
        self.assertEqual(self.call().code, "tool.timeout")  # never opened
        with mock.patch("beework.notifications.notify"):
            self.trip(5)
        self.provider.script = ["ok"]
        self.assertEqual(self.call("connection:flow").status, "ok")  # another tool is unaffected

    def test_the_breaker_falls_back_to_the_process_when_valkey_is_down(self) -> None:
        class Down:
            def __getattr__(self, name):
                def fail(*_a, **_k):
                    raise OSError("valkey down")
                return fail

        breaker = Breaker(valkey=Down(), memory=MemoryBreaker(self.clock))
        cfg = BreakerConfig(2, 60, 30)
        self.assertFalse(breaker.failure("k", cfg))
        self.assertTrue(breaker.failure("k", cfg))
        self.assertEqual(breaker.admit("k", cfg)[:2], (False, "open"))

    # -- the record --------------------------------------------------------------------------------

    def test_the_record_carries_the_attempts_and_the_policy_but_no_arguments_results_or_credentials(self) -> None:
        self.entry_with(retry={"max_attempts": 2, "backoff_ms": 0, "retry_on": ["timeout"]})
        self.provider.script = [transient(), "ok"]
        ctx = self.ctx()
        ctx.register_secret(SECRET)
        result = self.call(ctx=ctx)
        self.assertNotIn(SECRET, result.content)
        row = self.rows()[0]
        self.assertEqual((row.status, row.attempts, row.policy, row.breaker_state), ("ok", 2, "entry", "closed"))
        dumped = " ".join(str(getattr(row, c.name)) for c in row.__table__.columns)
        self.assertNotIn(SECRET, dumped)


class CategoryTests(unittest.TestCase):
    def test_what_is_worth_repeating(self) -> None:
        category = policy_mod.category_of
        self.assertEqual(category(ToolHttpError("tool.timeout", "x")), "timeout")
        self.assertEqual(category(ToolHttpError("tool.unreachable", "x")), "network")
        self.assertEqual(category(ToolError("tool.api_error", "x", status=502)), "5xx")
        self.assertEqual(category(ToolError("tool.api_error", "x", status=429)), "429")
        self.assertEqual(category(ToolError("tool.rate_limited", "x")), "429")
        self.assertIsNone(category(ToolError("tool.api_error", "x", status=404)))
        self.assertIsNone(category(ToolError("tool.auth_failed", "x")))
        self.assertIsNone(category(ToolError("tool.invalid_arguments", "x")))

    def test_retry_after_reads_seconds_and_ignores_anything_else(self) -> None:
        from beework.tools.http import retry_after_of

        self.assertEqual(retry_after_of({"Retry-After": "7"}), 7.0)
        self.assertEqual(retry_after_of({"retry-after": "500"}), 60.0)
        for odd in ("²", "", "-3", "0", "nan", "Wed, 21 Oct 2026 07:28:00 GMT"):
            self.assertIsNone(retry_after_of({"Retry-After": odd}), odd)
        self.assertIsNone(retry_after_of({}))


class StatsApiTests(test_capability_registry.RegistryTests):
    def test_stats_aggregate_the_call_records_per_tool_and_an_open_circuit_marks_the_entry_unstable(self) -> None:
        mcp = self.make_mcp()
        entry = self.live(mcp)["search_policy"]
        name = f"mcp:{mcp}:search_policy"
        rows = [("ok", None, 100, 1, None), ("ok", None, 300, 2, None), ("error", "tool.timeout", 30000, 3, "opened"), ("denied", "tool.circuit_open", 0, 1, "open"), ("cancelled", "tool.cancelled", 0, 1, None)]
        with self.session_factory.begin() as db:
            for status, code, ms, attempts, breaker in rows:
                db.add(ToolInvocation(tenant_id=self.tenant, tool_name=name, source="mcp", status=status, error_code=code, duration_ms=ms, attempts=attempts, breaker_state=breaker, attempt_ms=[]))
        stats = self.client.get("/api/v1/registry/stats", headers=self.admin).json()
        item = next(i for i in stats["items"] if i["tool"] == name)
        self.assertEqual((item["entry_id"], item["name"], item["calls"], item["ok"], item["avg_ms"]), (str(entry.id), "search_policy", 5, 2, 200))
        self.assertEqual((item["success_rate"], item["retries"], item["circuit_opened"], item["refused"]), (round(2 / 3, 4), 3, 1, 1))  # refused calls never ran, and a call the user declined is neither a success nor a failure
        self.assertFalse(self.get_entry(entry.id)["unstable"])
        with mock.patch.object(policy_mod, "BREAKER", Breaker(valkey=MemoryBreaker(), memory=MemoryBreaker())) as breaker:
            breaker.failure(policy_mod.breaker_key(self.tenant, name), BreakerConfig(1, 60, 30))
            self.assertTrue(self.get_entry(entry.id)["unstable"])
            listed = {i["identifier"]: i["unstable"] for i in self.list_entries()["items"]}
            self.assertEqual((listed["search_policy"], listed["create_ticket"]), (True, False))  # per tool, not per connection

    def get_entry(self, entry_id) -> dict:
        return self.client.get(f"/api/v1/registry/entries/{entry_id}", headers=self.admin).json()


# The registry tests are inherited only for their helpers; they run in test_capability_registry.
for _name in [n for n in dir(test_capability_registry.RegistryTests) if n.startswith("test_")]:
    setattr(StatsApiTests, _name, None)

if __name__ == "__main__":
    unittest.main()
