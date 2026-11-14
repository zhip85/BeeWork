"""T-084: call-trace list, waterfall, Langfuse client contract and retention."""

import json
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from beework import traces
from beework.config import settings
from beework.db import get_db
from beework.integrations import langfuse as lf
from beework.main import app
from beework.models import AuditEvent, Base, Membership, Organization, Tenant, User
from beework.models.assistants import Assistant
from beework.models.conversations import Conversation
from beework.models.jobs import RunEvent
from beework.models.runtime import Run
from beework.security import hash_password
from isolation import disable_external_integrations, stub_public_dns

PASSWORD = "correct horse battery"
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def observation(name: str, start: str, end: str, **extra) -> dict:
    return {"id": extra.pop("id", name), "name": name, "type": extra.pop("type", "SPAN"), "startTime": start, "endTime": end, "level": "DEFAULT", **extra}


class TraceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        disable_external_integrations()
        stub_public_dns()
        cls._db_dir = tempfile.mkdtemp(prefix="beework-test-")
        cls.engine = create_engine(f"sqlite:///{cls._db_dir}/test.db", connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(cls.engine, "connect")
        def pragmas(connection, _record) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        cls.session_factory = sessionmaker(bind=cls.engine, expire_on_commit=False)

        def override_get_db():
            with cls.session_factory() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db

    @classmethod
    def tearDownClass(cls) -> None:
        app.dependency_overrides.clear()
        cls.engine.dispose()
        shutil.rmtree(cls._db_dir, ignore_errors=True)

    def setUp(self) -> None:
        raw = self.engine.raw_connection()
        raw.cursor().execute("PRAGMA foreign_keys=OFF")
        raw.close()
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        self.client = TestClient(app)
        with self.session_factory.begin() as db:
            tenants = {code: Tenant(name=code, slug=code, plan="enterprise" if code == "other" else code, seat_limit=20) for code in ("enterprise", "standard", "other")}
            db.add_all(tenants.values())
            db.flush()
            roots, users, assistants = {}, {}, {}
            for code, tenant in tenants.items():
                roots[code] = Organization(tenant_id=tenant.id, name=code)
                db.add(roots[code])
                users[code] = User(email=f"{code}@example.com", display_name=code, password_hash=hash_password(PASSWORD))
                db.add(users[code])
            db.flush()
            for code, tenant in tenants.items():
                db.add(Membership(tenant_id=tenant.id, user_id=users[code].id, organization_id=roots[code].id, role="tenant_admin"))
                assistants[code] = Assistant(tenant_id=tenant.id, organization_id=roots[code].id, name=f"Bot-{code}", description="d", draft_config={})
                db.add(assistants[code])
            db.flush()
            self.ids = {code: {"tenant": tenants[code].id, "assistant": assistants[code].id} for code in tenants}

    # -- helpers ----------------------------------------------------------------

    def login(self, code: str = "enterprise", tenant: str | None = None) -> dict[str, str]:
        self.client.cookies.clear()
        response = self.client.post("/api/v1/auth/login", json={"email": f"{code}@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return {"X-CSRF-Token": self.client.cookies.get("beework_csrf"), "X-Tenant-ID": str(self.ids[tenant or code]["tenant"])}

    def make_run(self, code: str = "enterprise", *, source: str = "api", status: str = "succeeded", latency: int | None = 800, linked: bool = True,
            created: datetime | None = None, conversation: Conversation | None = None, **extra) -> Run:
        with self.session_factory.begin() as db:
            run = Run(
                id=uuid4(), tenant_id=self.ids[code]["tenant"], assistant_id=self.ids[code]["assistant"], status=status, source=source,
                latency_ms=latency, tokens_in=10, tokens_out=5, config={}, input={}, created_at=created or NOW,
                conversation_id=conversation.id if conversation else None, **extra,
            )
            run.trace_url = f"https://cloud.langfuse.com/project/p/traces/{traces_id(run.id)}" if linked else None
            db.add(run)
            return run

    def conversation(self, code: str = "enterprise", source: str = "website") -> Conversation:
        with self.session_factory.begin() as db:
            conversation = Conversation(
                tenant_id=self.ids[code]["tenant"], assistant_id=self.ids[code]["assistant"], source=source, environment="production",
                status="in_progress", started_at=NOW, last_active_at=NOW,
            )
            db.add(conversation)
            return conversation

    def list(self, headers, **params) -> dict:
        response = self.client.get("/api/v1/traces", params=params, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    # -- list -------------------------------------------------------------------

    def test_the_list_filters_by_status_origin_latency_and_channel(self) -> None:
        site = self.conversation(source="website")
        ok = self.make_run(latency=400, conversation=site)
        slow = self.make_run(latency=settings.trace_slow_ms + 1)
        failed = self.make_run(status="failed", latency=100, error_code="runtime.failed")
        debug = self.make_run(source="debug")
        trial = self.make_run(source="test")
        evaluation = self.make_run(source="eval")
        headers = self.login()

        def ids(**params):
            return {item["run_id"] for item in self.list(headers, **params)["items"]}

        self.assertEqual(ids(), {str(r.id) for r in (ok, slow, failed, debug, trial, evaluation)})
        self.assertEqual(ids(status="slow"), {str(slow.id)})
        self.assertEqual(ids(status="error"), {str(failed.id)})
        self.assertEqual(ids(status="success"), {str(r.id) for r in (ok, debug, trial, evaluation)})
        self.assertEqual(ids(origin="online"), {str(r.id) for r in (ok, slow, failed)})
        self.assertEqual((ids(origin="debug"), ids(origin="trial"), ids(origin="eval")), ({str(debug.id)}, {str(trial.id)}, {str(evaluation.id)}))
        self.assertEqual(ids(min_latency_ms=settings.trace_slow_ms), {str(slow.id)})
        self.assertEqual(ids(channel="website"), {str(ok.id)})
        self.assertEqual(ids(assistant_id=str(self.ids["enterprise"]["assistant"])), ids())
        row = self.list(headers, status="slow")["items"][0]
        self.assertEqual((row["status"], row["origin"], row["assistant_name"], row["trace_available"]), ("slow", "online", "Bot-enterprise", True))
        self.assertEqual(len(row["trace_id"]), 32)

    def test_the_list_filters_by_time_and_searches_exact_ids(self) -> None:
        old = self.make_run(created=NOW - timedelta(days=3))
        site = self.conversation()
        recent = self.make_run(conversation=site)
        headers = self.login()
        since = (NOW - timedelta(days=1)).isoformat()
        self.assertEqual([i["run_id"] for i in self.list(headers, created_from=since)["items"]], [str(recent.id)])
        self.assertEqual([i["run_id"] for i in self.list(headers, created_to=since)["items"]], [str(old.id)])
        self.assertEqual([i["run_id"] for i in self.list(headers, q=str(recent.id))["items"]], [str(recent.id)])
        self.assertEqual([i["run_id"] for i in self.list(headers, q=str(site.id))["items"]], [str(recent.id)])
        trace_id = self.list(headers, q=str(old.id))["items"][0]["trace_id"]
        self.assertEqual([i["run_id"] for i in self.list(headers, q=trace_id)["items"]], [str(old.id)])
        self.assertEqual(self.list(headers, q=str(uuid4()))["total"], 0)
        self.assertEqual(self.list(headers, q="not-an-id")["total"], 0)  # exact match only, never a substring search
        page = self.list(headers, limit=1, offset=1)
        self.assertEqual((page["total"], len(page["items"])), (2, 1))

    def test_other_tenants_portal_conversations_and_unknown_runs_are_not_reachable(self) -> None:
        mine = self.make_run()
        theirs = self.make_run("other")
        private = self.make_run(conversation=self.conversation(source="portal"))
        headers = self.login()
        self.assertEqual({i["run_id"] for i in self.list(headers)["items"]}, {str(mine.id)})
        for hidden in (theirs, private):
            self.assertEqual(self.client.get(f"/api/v1/traces/{hidden.id}", headers=headers).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/traces/{uuid4()}", headers=headers).status_code, 404)
        # the caller has no membership in the other tenant: the tenant header alone gives nothing
        foreign = self.login("enterprise", tenant="other")
        self.assertIn(self.client.get("/api/v1/traces", headers=foreign).status_code, (403, 404))
        self.assertEqual(self.client.get(f"/api/v1/traces/{theirs.id}", headers=foreign).status_code in (403, 404), True)

    # -- detail -----------------------------------------------------------------

    def test_the_waterfall_comes_from_langfuse_in_start_order_and_is_redacted(self) -> None:
        run = self.make_run()
        with self.session_factory.begin() as db:
            db.add(RunEvent(tenant_id=run.tenant_id, run_id=run.id, seq=1, type="run.started", data={}, created_at=NOW))
        observations = [
            observation("oc.execute", "2026-11-14T12:00:01.000Z", "2026-11-14T12:00:03.000Z", id="b", parentObservationId="a"),
            observation("assistant.run", "2026-11-14T12:00:00.000Z", "2026-11-14T12:00:03.500Z", id="a"),
            observation(
                "ChatOpenAI", "2026-11-14T12:00:01.500Z", "2026-11-14T12:00:02.500Z", id="c", parentObservationId="b", type="GENERATION", model="m1",
                usageDetails={"input": 20, "output": 7}, totalCost=0.002, input="call 13812345678", output="ok", level="ERROR", statusMessage="boom",
            ),
        ]
        headers = self.login()
        with patch.object(traces, "langfuse_configured", return_value=True), patch.object(traces, "fetch_observations", return_value=observations) as fetch:
            detail = self.client.get(f"/api/v1/traces/{run.id}", headers=headers).json()
        self.assertEqual(fetch.call_args.args[0], detail["trace_id"])
        self.assertEqual(detail["spans_source"], "langfuse")
        self.assertEqual([s["name"] for s in detail["spans"]], ["assistant.run", "oc.execute", "ChatOpenAI"])
        root, execute, generation = detail["spans"]
        self.assertEqual((root["start_ms"], root["duration_ms"], execute["start_ms"], execute["duration_ms"]), (0, 3500, 1000, 2000))
        self.assertEqual((generation["kind"], generation["status"], generation["model"], generation["tokens_in"], generation["tokens_out"], generation["cost"]), ("generation", "error", "m1", 20, 7, 0.002))
        self.assertEqual((generation["parent_id"], generation["message"], generation["output"]), ("b", "boom", "ok"))
        self.assertNotIn("13812345678", generation["input"])
        self.assertEqual([e["type"] for e in detail["timeline"]], ["run.started"])

    def test_without_langfuse_the_detail_falls_back_to_the_run_timeline(self) -> None:
        run = self.make_run()
        expired = self.make_run(linked=False)
        with self.session_factory.begin() as db:
            db.add(RunEvent(tenant_id=run.tenant_id, run_id=run.id, seq=1, type="run.started", data={}, created_at=NOW))
        headers = self.login()
        with patch.object(traces, "langfuse_configured", return_value=True), patch.object(traces, "fetch_observations", side_effect=lf.TracingError("down")):
            down = self.client.get(f"/api/v1/traces/{run.id}", headers=headers).json()
        self.assertEqual((down["spans_source"], down["spans"], [e["type"] for e in down["timeline"]]), ("unavailable", [], ["run.started"]))
        self.assertIn("暂时不可用", down["notice"])
        gone = self.client.get(f"/api/v1/traces/{expired.id}", headers=headers).json()
        self.assertEqual((gone["spans_source"], gone["trace_available"]), ("unavailable", False))
        self.assertIn("保留期", gone["notice"])

    # -- Langfuse client contract ---------------------------------------------------

    def configured(self):
        return patch.multiple(settings, langfuse_host="https://lf.test", langfuse_public_key="pk", langfuse_secret_key="sk")

    def test_observations_are_read_with_the_v2_contract_and_cursor_paging(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            first = "cursor" not in request.url.params
            return httpx.Response(200, json={"data": [{"id": "1" if first else "2"}], "meta": {"cursor": "next"} if first else {}})

        with self.configured():
            found = lf.fetch_observations("t" * 32, transport=httpx.MockTransport(handler))
        self.assertEqual([o["id"] for o in found], ["1", "2"])
        self.assertEqual(calls[0].url.path, "/api/public/v2/observations")
        self.assertEqual((calls[0].url.params["traceId"], calls[0].url.params["fields"]), ("t" * 32, lf.OBSERVATION_FIELDS))
        self.assertEqual(calls[1].url.params["cursor"], "next")
        self.assertTrue(calls[0].headers["authorization"].startswith("Basic "))

    def test_langfuse_failures_become_tracing_errors(self) -> None:
        with self.configured():
            for status in (401, 500):
                with self.assertRaises(lf.TracingError):
                    lf.fetch_observations("t", transport=httpx.MockTransport(lambda r, s=status: httpx.Response(s)))

            def refuse(request):
                raise httpx.ConnectError("no route")

            with self.assertRaises(lf.TracingError):
                lf.delete_traces(["t"], transport=httpx.MockTransport(refuse))
        with self.assertRaises(lf.TracingError):  # not configured
            lf.fetch_observations("t")

    def test_traces_are_deleted_in_one_batch_call(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"message": "Traces deleted successfully"})

        with self.configured():
            lf.delete_traces(["a", "b"], transport=httpx.MockTransport(handler))
            lf.delete_traces([], transport=httpx.MockTransport(handler))  # nothing to do, no call
        self.assertEqual((len(seen), seen[0].method, seen[0].url.path, json.loads(seen[0].content)), (1, "DELETE", "/api/public/traces", {"traceIds": ["a", "b"]}))

    # -- retention --------------------------------------------------------------

    def test_retention_is_the_shorter_of_the_plan_and_the_conversation_retention(self) -> None:
        with self.session_factory() as db:
            enterprise, standard = db.get(Tenant, self.ids["enterprise"]["tenant"]), db.get(Tenant, self.ids["standard"]["tenant"])
            self.assertEqual((traces.trace_retention_days(db, standard), traces.trace_retention_days(db, enterprise)), (7, 90))
            with patch.object(traces, "get_policy") as policy:
                policy.return_value.retention_days = 30
                self.assertEqual(traces.trace_retention_days(db, enterprise), 30)
                self.assertEqual(traces.trace_retention_days(db, standard), 7)

    def test_expired_traces_are_deleted_remotely_and_unlinked_with_an_audit_row(self) -> None:
        old_standard = self.make_run("standard", created=NOW - timedelta(days=8))
        fresh_standard = self.make_run("standard", created=NOW - timedelta(days=6))
        enterprise = self.make_run("enterprise", created=NOW - timedelta(days=8))  # still inside 90 days
        with self.session_factory() as db, patch.object(traces, "langfuse_configured", return_value=True), patch.object(traces, "delete_traces") as delete:
            purged = traces.purge_expired_traces(db, NOW)
        self.assertEqual(purged, {self.ids["standard"]["tenant"]: 1})
        self.assertEqual(delete.call_args.args[0], [traces.trace_id_of(old_standard)])
        with self.session_factory() as db:
            linked = {r.id: r.trace_url is not None for r in db.scalars(select(Run))}
            audit = db.scalars(select(AuditEvent).where(AuditEvent.action == "trace.retention_purged")).all()
        self.assertEqual(linked, {old_standard.id: False, fresh_standard.id: True, enterprise.id: True})
        self.assertEqual([(a.tenant_id, a.details["traces"], a.details["retention_days"]) for a in audit], [(self.ids["standard"]["tenant"], "1", "7")])

    def test_a_failed_remote_delete_keeps_the_links_for_the_next_day(self) -> None:
        old = self.make_run("standard", created=NOW - timedelta(days=8))
        with self.session_factory() as db, patch.object(traces, "langfuse_configured", return_value=True), patch.object(traces, "delete_traces", side_effect=lf.TracingError("down")):
            self.assertEqual(traces.purge_expired_traces(db, NOW), {})
        with self.session_factory() as db:
            self.assertIsNotNone(db.get(Run, old.id).trace_url)
            self.assertEqual(db.scalars(select(AuditEvent).where(AuditEvent.action == "trace.retention_purged")).all(), [])
        with self.session_factory() as db, patch.object(traces, "langfuse_configured", return_value=True), patch.object(traces, "delete_traces"):
            self.assertEqual(traces.purge_expired_traces(db, NOW), {self.ids["standard"]["tenant"]: 1})

    def test_the_daily_schedule_includes_trace_retention(self) -> None:
        from beework.worker import scheduler
        from beework.worker.queue import HANDLERS

        self.assertIn("traces.retention", HANDLERS)
        with self.session_factory.begin() as db:
            scheduler.enqueue_scheduled_jobs(db, now=NOW)
        from beework.models.jobs import Job

        with self.session_factory() as db:
            self.assertIn("traces.retention", {job.kind for job in db.scalars(select(Job))})


def traces_id(run_id: UUID) -> str:
    return uuid4().hex  # a stored link ends with a 32-character trace id


if __name__ == "__main__":
    unittest.main()
