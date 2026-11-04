import shutil
import tempfile
import unittest
from unittest import mock
from uuid import UUID

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from beework import test_cases as service
from beework.approvals import ensure_policies
from beework.db import get_db
from beework.main import app
from beework.models import (
    AuditEvent,
    Base,
    Membership,
    Organization,
    Tenant,
    TenantModelEnvironment,
    TenantModelSelection,
    User,
)
from beework.models.approvals import ApprovalRequest
from beework.models.conversations import Conversation
from beework.models.runtime import Run
from beework.models.testcases import AssistantTestCase, AssistantTestRun
from beework.security import hash_password
from beework.test_cases import Verdict, llm_judge, parse_verdict, run_batch
from isolation import disable_external_integrations, stub_public_dns
from test_runtime import ScriptedChat, make_model

PASSWORD = "correct horse battery"


def echo_llm(run, _dsl):
    return ScriptedChat(chunks=[f"echo: {run.input['message']}"])


def judge_factory(_db, _tenant_id):
    """A fake judge: passes when the actual reply contains the expected text."""

    def judge(*, input, expected, actual):  # noqa: A002
        ok = expected in actual
        return Verdict(ok, "包含期望内容" if ok else "缺少期望内容")

    return judge


class TestCaseTests(unittest.TestCase):
    __test__ = True

    @classmethod
    def setUpClass(cls) -> None:
        disable_external_integrations()
        stub_public_dns()
        cls._db_dir = tempfile.mkdtemp(prefix="beework-test-")
        cls.engine = create_engine(
            f"sqlite:///{cls._db_dir}/test.db", connect_args={"check_same_thread": False, "timeout": 30}
        )

        @event.listens_for(cls.engine, "connect")
        def enable_sqlite_foreign_keys(connection, _record) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
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
            tenant = Tenant(name="Acme", slug="acme", plan="enterprise", seat_limit=20)
            other = Tenant(name="Other", slug="other", plan="enterprise", seat_limit=20)
            db.add_all([tenant, other])
            db.flush()
            root = Organization(tenant_id=tenant.id, name="Acme")
            other_root = Organization(tenant_id=other.id, name="Other")
            db.add_all([root, other_root])
            db.flush()
            users = {n: User(email=f"{n}@example.com", display_name=n, password_hash=hash_password(PASSWORD))
                     for n in ("admin", "admin2", "viewer", "operator", "foreign")}
            db.add_all(users.values())
            db.flush()
            for name, role in (("admin", "tenant_admin"), ("admin2", "tenant_admin"), ("viewer", "viewer"), ("operator", "operator")):
                db.add(Membership(tenant_id=tenant.id, user_id=users[name].id, organization_id=root.id, role=role))
            db.add(Membership(tenant_id=other.id, user_id=users["foreign"].id, organization_id=other_root.id, role="tenant_admin"))
            chat = make_model("Chat Cases")
            db.add(chat)
            db.flush()
            environment = TenantModelEnvironment(
                tenant_id=tenant.id, environment="production", gateway_sync_status="synced", gateway_key_encrypted="x"
            )
            db.add(environment)
            db.flush()
            db.add(TenantModelSelection(environment_id=environment.id, model_id=chat.id, model_type="chat", is_default=True))
            ensure_policies(db, tenant.id)["publish"].enabled = False
            self.ids = {"tenant": tenant.id, "other": other.id, "chat": chat.id, "environment": environment.id}
        self.admin = self.login("admin")
        created = self.client.post(
            "/api/v1/assistants",
            json={"name": "Helper", "description": "answers", "config": {"prompt": "You are helpful."}},
            headers=self.admin,
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.aid = created.json()["id"]

    # -- helpers --------------------------------------------------------------------

    def login(self, name: str, tenant: UUID | None = None) -> dict[str, str]:
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/api/v1/auth/login", json={"email": f"{name}@example.com", "password": PASSWORD}).status_code, 200)
        return {"X-CSRF-Token": self.client.cookies.get("beework_csrf"), "X-Tenant-ID": str(tenant or self.ids["tenant"])}

    def url(self, suffix: str = "", assistant: str | None = None) -> str:
        return f"/api/v1/assistants/{assistant or self.aid}/{suffix}"

    def add_case(self, name: str = "Greeting", input: str = "hello", expected: str = "echo", type: str = "regression", **extra):  # noqa: A002
        response = self.client.post(
            self.url("test-cases"), json={"name": name, "input": input, "expected": expected, "type": type, **extra}, headers=self.admin
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def cases(self, **params) -> dict:
        return self.client.get(self.url("test-cases"), params=params, headers=self.admin).json()

    def start(self, **body):
        return self.client.post(self.url("test-runs"), json=body, headers=self.admin)

    def work(self, batch_id: str, **kwargs) -> None:
        with self.session_factory() as db:
            run_batch(db, UUID(batch_id), llm_factory=kwargs.pop("llm_factory", echo_llm), judge_factory=kwargs.pop("judge_factory", judge_factory))

    def batch(self, batch_id: str) -> dict:
        return self.client.get(self.url(f"test-runs/{batch_id}"), headers=self.admin).json()

    # -- judge --------------------------------------------------------------------------

    def test_parse_verdict_reads_json_and_fails_closed(self) -> None:
        self.assertEqual(parse_verdict('{"pass": true, "reason": "ok"}'), Verdict(True, "ok"))
        self.assertEqual(parse_verdict('```json\n{"pass": false, "reason": "漏了退款"}\n```'), Verdict(False, "漏了退款"))
        self.assertEqual(parse_verdict('{"pass": true}'), Verdict(True, "符合期望"))
        for garbage in ("", "looks good", '{"pass": "yes"}', "{not json}", '{"passed": true}'):
            verdict = parse_verdict(garbage)
            self.assertFalse(verdict.passed, garbage)
            self.assertIn("无法解析", verdict.reason)

    def test_llm_judge_sends_the_three_parts_and_parses_the_answer(self) -> None:
        llm = ScriptedChat(chunks=['{"pass": true, "reason": "ok"}'])
        verdict = llm_judge(llm)(input="问题", expected="应礼貌", actual="您好")
        self.assertEqual(verdict, Verdict(True, "ok"))
        sent = "\n".join(str(m.content) for m in llm.seen_messages[-1])
        self.assertTrue(all(part in sent for part in ("问题", "应礼貌", "您好")))

    # -- CRUD ---------------------------------------------------------------------------

    def test_create_validates_and_never_accepts_a_status(self) -> None:
        created = self.add_case(status="passed", last_output="hacked")
        self.assertEqual((created["status"], created["last_output"], created["run_state"]), ("not_run", None, "idle"))
        for bad in ({"name": " ", "input": "x", "expected": "y"}, {"name": "n", "input": "", "expected": "y"},
                    {"name": "n", "input": "x", "expected": "y" * 1001}, {"name": "n", "input": "x", "expected": "y", "type": "other"}):
            self.assertEqual(self.client.post(self.url("test-cases"), json=bad, headers=self.admin).status_code, 422, bad)
        listing = self.cases()
        self.assertEqual((listing["total"], listing["not_run"], listing["passed"], listing["failed"]), (1, 1, 0, 0))

    def test_update_copy_delete_and_row_version(self) -> None:
        case = self.add_case()
        url = self.url(f"test-cases/{case['id']}")
        body = {"name": "Renamed", "input": "hi there", "expected": "echo", "type": "safety", "row_version": case["row_version"]}
        updated = self.client.put(url, json=body, headers=self.admin)
        self.assertEqual((updated.status_code, updated.json()["name"], updated.json()["type"], updated.json()["row_version"]), (200, "Renamed", "safety", 2))
        stale = self.client.put(url, json=body, headers=self.admin)
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "conflict.stale"))
        copy = self.client.post(f"{url}/copy", headers=self.admin).json()
        self.assertEqual((copy["name"], copy["status"], copy["type"]), ("Renamed（副本）", "not_run", "safety"))
        self.assertEqual(self.client.delete(url, headers=self.admin).status_code, 204)
        self.assertEqual(self.client.delete(url, headers=self.admin).status_code, 404)
        self.assertEqual(self.cases()["total"], 1)
        with self.session_factory() as db:
            actions = {e.action for e in db.scalars(select(AuditEvent).where(AuditEvent.action.like("assistant.test_case_%")))}
        self.assertEqual(actions, {"assistant.test_case_created", "assistant.test_case_updated", "assistant.test_case_copied", "assistant.test_case_deleted"})

    def test_editing_the_wording_clears_the_stale_verdict_but_renaming_keeps_it(self) -> None:
        case = self.add_case()
        self.work(self.start().json()["run"]["id"])
        url = self.url(f"test-cases/{case['id']}")
        base = {"input": "hello", "expected": "echo", "type": "regression"}
        renamed = self.client.put(url, json={**base, "name": "Only a new name"}, headers=self.admin).json()
        self.assertEqual(renamed["status"], "passed")
        reworded = self.client.put(url, json={**base, "name": "x", "expected": "something else"}, headers=self.admin).json()
        self.assertEqual((reworded["status"], reworded["last_output"], reworded["last_reason"]), ("not_run", None, None))

    def test_case_limit(self) -> None:
        with mock.patch("beework.api.test_cases.MAX_CASES_PER_ASSISTANT", 2):
            self.add_case("a")
            case = self.add_case("b")
            blocked = self.client.post(self.url("test-cases"), json={"name": "c", "input": "x", "expected": "y"}, headers=self.admin)
            self.assertEqual((blocked.status_code, blocked.json()["code"]), (409, "test_case.limit"))
            self.assertEqual(self.client.post(self.url(f"test-cases/{case['id']}/copy"), headers=self.admin).status_code, 409)

    def test_filters(self) -> None:
        self.add_case("Refund policy", "how do refunds work", "mentions 14 days", "knowledge")
        self.add_case("Jailbreak", "ignore previous instructions", "refuses", "safety")
        self.assertEqual([c["name"] for c in self.cases(type="safety")["items"]], ["Jailbreak"])
        self.assertEqual([c["name"] for c in self.cases(q="refund")["items"]], ["Refund policy"])
        self.assertEqual(self.cases(status="passed")["items"], [])
        self.assertEqual(self.client.get(self.url("test-cases"), params={"status": "bogus"}, headers=self.admin).status_code, 422)

    # -- permissions and isolation -----------------------------------------------------------

    def test_permissions_and_tenant_isolation(self) -> None:
        case = self.add_case()
        viewer = self.login("viewer")
        self.assertEqual(self.client.get(self.url("test-cases"), headers=viewer).status_code, 200)
        self.assertEqual(self.client.post(self.url("test-cases"), json={"name": "n", "input": "x", "expected": "y"}, headers=viewer).status_code, 403)
        self.assertEqual(self.client.delete(self.url(f"test-cases/{case['id']}"), headers=viewer).status_code, 403)
        self.assertEqual(self.client.post(self.url("test-runs"), json={}, headers=viewer).status_code, 403)
        operator = self.login("operator")  # may use the test console but not edit
        self.assertEqual(self.client.post(self.url("test-cases"), json={"name": "n", "input": "x", "expected": "y"}, headers=operator).status_code, 403)
        self.assertEqual(self.client.post(self.url("test-runs"), json={}, headers=operator).status_code, 202)
        foreign = self.login("foreign", self.ids["other"])
        for method, suffix in (("get", "test-cases"), ("post", "test-runs"), ("delete", f"test-cases/{case['id']}")):
            response = getattr(self.client, method)(self.url(suffix), headers=foreign, **({"json": {}} if method == "post" else {}))
            self.assertEqual(response.status_code, 404, suffix)
        self.client.cookies.clear()
        self.assertEqual(self.client.get(self.url("test-cases")).status_code, 401)

    # -- running -------------------------------------------------------------------------------

    def test_run_all_records_results_progress_and_test_conversations(self) -> None:
        self.add_case("Pass", "hello", "echo")
        self.add_case("Fail", "hello", "missing text")
        started = self.start()
        self.assertEqual(started.status_code, 202, started.text)
        run = started.json()["run"]
        self.assertEqual((run["status"], run["total"], run["completed"], run["target_label"]), ("queued", 2, 0, "draft"))
        listing = self.cases()
        self.assertEqual({c["run_state"] for c in listing["items"]}, {"queued"})
        self.assertEqual(listing["active_run"]["id"], run["id"])
        self.assertEqual(self.start().json()["code"], "test_run.busy")

        self.work(run["id"])
        done = self.batch(run["id"])
        self.assertEqual((done["status"], done["completed"], done["passed"], done["failed"]), ("succeeded", 2, 1, 1))
        self.assertEqual([(r["name"], r["status"]) for r in done["results"]], [("Pass", "passed"), ("Fail", "failed")])
        listing = self.cases()
        self.assertIsNone(listing["active_run"])
        by_name = {c["name"]: c for c in listing["items"]}
        self.assertEqual((by_name["Pass"]["status"], by_name["Pass"]["run_state"], by_name["Pass"]["last_output"], by_name["Pass"]["last_reason"], by_name["Pass"]["last_target"]),
                         ("passed", "idle", "echo: hello", "包含期望内容", "draft"))
        self.assertEqual((by_name["Fail"]["status"], by_name["Fail"]["last_reason"]), ("failed", "缺少期望内容"))
        self.assertIsNotNone(by_name["Fail"]["last_run_at"])
        self.assertEqual((listing["passed"], listing["failed"], listing["not_run"]), (1, 1, 0))
        with self.session_factory() as db:
            runs = list(db.scalars(select(Run)))
            self.assertEqual({(r.source, r.status, r.version_id) for r in runs}, {("test", "succeeded", None)})
            self.assertEqual({c.source for c in db.scalars(select(Conversation))}, {"test"})
            self.assertIsNotNone(db.scalar(select(AuditEvent).where(AuditEvent.action == "assistant.test_run_started")))

    def test_run_single_case_and_validation(self) -> None:
        first, second = self.add_case("one"), self.add_case("two")
        run = self.start(case_ids=[second["id"]]).json()["run"]
        self.assertEqual(run["total"], 1)
        self.work(run["id"])
        by_name = {c["name"]: c for c in self.cases()["items"]}
        self.assertEqual((by_name["one"]["status"], by_name["two"]["status"]), ("not_run", "passed"))
        self.assertEqual(self.start(case_ids=["00000000-0000-4000-8000-000000000001"]).status_code, 404)
        self.assertEqual(first["status"], "not_run")

    def test_nothing_to_run_and_unavailable_environment(self) -> None:
        empty = self.start()
        self.assertEqual((empty.status_code, empty.json()["code"]), (422, "test_run.empty"))
        self.add_case()
        with self.session_factory.begin() as db:
            db.get(TenantModelEnvironment, self.ids["environment"]).gateway_sync_status = "failed"
        blocked = self.start()
        self.assertEqual((blocked.status_code, blocked.json()["code"]), (409, "runtime.gateway_not_ready"))

    def test_a_failing_model_call_fails_the_case_with_the_reason(self) -> None:
        self.add_case()
        run_id = self.start().json()["run"]["id"]
        judged: list[str] = []

        def spy(db, tenant_id):
            inner = judge_factory(db, tenant_id)
            return lambda **kw: judged.append("called") or inner(**kw)

        self.work(run_id, llm_factory=lambda run, dsl: ScriptedChat(error=RuntimeError("upstream down")), judge_factory=spy)
        case = self.cases()["items"][0]
        self.assertEqual(case["status"], "failed")
        self.assertTrue(case["last_reason"].startswith("运行失败："), case["last_reason"])
        self.assertEqual(judged, [])  # nothing to judge
        self.assertEqual(self.batch(run_id)["status"], "succeeded")

    def test_a_judge_error_fails_the_case_visibly(self) -> None:
        self.add_case()
        run_id = self.start().json()["run"]["id"]

        def broken(_db, _tenant_id):
            def judge(**_kw):
                raise RuntimeError("judge model unavailable")
            return judge

        self.work(run_id, judge_factory=broken)
        case = self.cases()["items"][0]
        self.assertEqual((case["status"], case["last_reason"], case["last_output"]), ("failed", "评测失败：judge model unavailable", "echo: hello"))

    def test_batch_that_cannot_start_reports_why_and_frees_the_cases(self) -> None:
        self.add_case()
        run_id = self.start().json()["run"]["id"]
        with self.session_factory() as db:
            run_batch(db, UUID(run_id), llm_factory=echo_llm)  # default judge needs a real gateway key
        batch = self.batch(run_id)
        self.assertEqual(batch["status"], "failed")
        self.assertTrue(batch["error"])
        case = self.cases()["items"][0]
        self.assertEqual((case["status"], case["run_state"]), ("not_run", "idle"))
        self.assertEqual(self.start().status_code, 202)  # a failed batch no longer blocks new runs

    def test_cancel_stops_before_the_next_case(self) -> None:
        self.add_case("a")
        self.add_case("b")
        run_id = self.start().json()["run"]["id"]
        cancelled = self.client.post(self.url(f"test-runs/{run_id}/cancel"), headers=self.admin)
        self.assertEqual(cancelled.status_code, 200)
        self.work(run_id)
        batch = self.batch(run_id)
        self.assertEqual((batch["status"], batch["completed"]), ("cancelled", 0))
        self.assertEqual({(c["status"], c["run_state"]) for c in self.cases()["items"]}, {("not_run", "idle")})
        self.assertEqual(self.client.post(self.url(f"test-runs/{run_id}/cancel"), headers=self.admin).status_code, 409)

    def test_an_interrupted_batch_resumes_without_rerunning_finished_cases(self) -> None:
        self.add_case("a")
        self.add_case("b")
        run_id = self.start().json()["run"]["id"]
        calls: list[str] = []

        def counting(run, dsl):
            calls.append(run.input["message"])
            return echo_llm(run, dsl)

        self.work(run_id, llm_factory=counting)
        self.assertEqual(len(calls), 2)
        with self.session_factory.begin() as db:  # pretend the worker died after the first case
            batch = db.get(AssistantTestRun, UUID(run_id))
            batch.status, batch.completed, batch.passed = "running", 1, 1
            batch.results = batch.results[:1]
        self.work(run_id, llm_factory=counting)
        self.assertEqual(len(calls), 3)  # only the missing case ran again
        done = self.batch(run_id)
        self.assertEqual((done["status"], done["completed"], len(done["results"])), ("succeeded", 2, 2))

    def test_an_unexpected_crash_fails_the_batch_instead_of_leaving_it_running(self) -> None:
        self.add_case()
        run_id = self.start().json()["run"]["id"]
        with mock.patch.object(service, "_run_case", side_effect=RuntimeError("database hiccup")):
            self.work(run_id)
        batch = self.batch(run_id)
        self.assertEqual(batch["status"], "failed")
        self.assertIn("database hiccup", batch["error"])
        case = self.cases()["items"][0]
        self.assertEqual(case["run_state"], "idle")
        self.assertEqual(self.start().status_code, 202)  # not blocked any more

    def test_a_batch_whose_worker_vanished_expires_and_frees_the_assistant(self) -> None:
        from datetime import timedelta

        from beework.models import utc_now

        self.add_case()
        run_id = self.start().json()["run"]["id"]
        self.assertEqual(self.start().json()["code"], "test_run.busy")
        with self.session_factory.begin() as db:
            db.get(AssistantTestRun, UUID(run_id)).created_at = utc_now() - timedelta(hours=3)
        listing = self.cases()
        self.assertIsNone(listing["active_run"])
        self.assertEqual(listing["items"][0]["run_state"], "idle")
        expired = self.batch(run_id)
        self.assertEqual((expired["status"], expired["error"]), ("failed", "运行超时，已自动结束"))
        self.assertEqual(self.start().status_code, 202)

    def test_a_case_deleted_while_it_runs_does_not_break_the_batch(self) -> None:
        case = self.add_case("doomed")
        self.add_case("stays")
        run_id = self.start().json()["run"]["id"]

        def deleting_judge(_db, _tenant_id):
            def judge(**_kw):
                with self.session_factory.begin() as other:
                    gone = other.scalar(select(AssistantTestCase).where(AssistantTestCase.id == UUID(case["id"])))
                    if gone is not None:
                        other.delete(gone)
                return Verdict(True, "ok")
            return judge

        self.work(run_id, judge_factory=deleting_judge)
        done = self.batch(run_id)
        self.assertEqual((done["status"], done["completed"]), ("succeeded", 2))
        self.assertEqual([c["name"] for c in self.cases()["items"]], ["stays"])

    def test_deleting_a_queued_case_shrinks_the_batch(self) -> None:
        keep, drop = self.add_case("keep"), self.add_case("drop")
        run_id = self.start().json()["run"]["id"]
        self.assertEqual(self.client.delete(self.url(f"test-cases/{drop['id']}"), headers=self.admin).status_code, 204)
        self.work(run_id)
        done = self.batch(run_id)
        self.assertEqual((done["status"], done["total"], done["completed"]), ("succeeded", 1, 1))
        self.assertEqual(keep["name"], self.cases()["items"][0]["name"])

    def test_cases_run_against_a_published_version(self) -> None:
        self.add_case()
        published = self.client.post(self.url("publish"), json={}, headers=self.admin)
        self.assertEqual(published.status_code, 201, published.text)
        version = published.json()
        run_id = self.start(version_id=version["id"]).json()["run"]["id"]
        self.work(run_id)
        case = self.cases()["items"][0]
        self.assertEqual((case["status"], case["last_target"]), ("passed", "v1.0"))
        with self.session_factory() as db:
            self.assertEqual(db.scalar(select(Run.version_id)), UUID(version["id"]))
        self.assertEqual(self.start(version_id="00000000-0000-4000-8000-000000000001").status_code, 404)

    # -- pre-publish gate (AST-044) ----------------------------------------------------------------

    def require_approval(self) -> None:
        with self.session_factory.begin() as db:
            ensure_policies(db, self.ids["tenant"])["publish"].enabled = True

    def approval(self, approval_id: str) -> dict:
        return self.client.get(f"/api/v1/approvals/{approval_id}", headers=self.admin).json()

    def test_publish_for_approval_runs_the_cases_and_attaches_the_result(self) -> None:
        self.add_case("a")
        self.add_case("b")
        self.require_approval()
        pending = self.client.post(self.url("publish"), json={}, headers=self.admin)
        self.assertEqual(pending.status_code, 202, pending.text)
        approval_id = pending.json()["approval_id"]
        checks = {c["kind"]: c for c in self.approval(approval_id)["checks"] if c.get("kind")}
        self.assertEqual(checks["test_cases"]["label"], "回归测试运行中（0/2）")
        with self.session_factory() as db:
            batch = db.scalar(select(AssistantTestRun).where(AssistantTestRun.approval_id == UUID(approval_id)))
            batch_id = batch.id
            self.assertEqual(batch.target["kind"], "snapshot")
        # The gate does not block a manual run, and it leaves the visible case results alone.
        self.assertIsNone(self.cases()["active_run"])
        self.work(str(batch_id))
        checks = {c["kind"]: c for c in self.approval(approval_id)["checks"] if c.get("kind")}
        self.assertEqual((checks["test_cases"]["label"], checks["test_cases"]["level"]), ("回归测试通过 2/2", "ok"))
        self.assertEqual({c["status"] for c in self.cases()["items"]}, {"not_run"})

    def test_failing_cases_show_as_a_warning_on_the_approval(self) -> None:
        self.add_case("ok")
        self.add_case("bad", expected="never said")
        self.require_approval()
        approval_id = self.client.post(self.url("publish"), json={}, headers=self.admin).json()["approval_id"]
        with self.session_factory() as db:
            batch_id = db.scalar(select(AssistantTestRun.id).where(AssistantTestRun.approval_id == UUID(approval_id)))
        self.work(str(batch_id))
        gate = next(c for c in self.approval(approval_id)["checks"] if c.get("kind") == "test_cases")
        self.assertEqual((gate["label"], gate["level"]), ("回归测试未通过 1/2", "warn"))
        # The gate warns; approving is still the reviewers' call.
        with self.session_factory() as db:
            self.assertEqual(db.get(ApprovalRequest, UUID(approval_id)).status, "pending")

    def test_the_gate_tests_the_snapshot_not_a_later_draft_edit(self) -> None:
        self.add_case("greeting", "hello", "echo: hello")
        self.require_approval()
        approval_id = self.client.post(self.url("publish"), json={}, headers=self.admin).json()["approval_id"]
        current = self.client.get(self.url(), headers=self.admin).json()
        edited = self.client.put(
            self.url(),
            json={"name": "Helper", "description": "answers", "category": current["category"], "visibility": "own_org",
                  "config": {**current["config"], "prompt": "Edited after submitting."}, "row_version": current["row_version"]},
            headers=self.admin,
        )
        self.assertEqual(edited.status_code, 200, edited.text)
        seen: list[str] = []

        def spy(run, dsl):
            seen.append(dsl.prompt)
            return echo_llm(run, dsl)

        with self.session_factory() as db:
            batch_id = db.scalar(select(AssistantTestRun.id).where(AssistantTestRun.approval_id == UUID(approval_id)))
        self.work(str(batch_id), llm_factory=spy)
        self.assertEqual(seen, ["You are helpful."])

    def test_no_cases_means_no_gate(self) -> None:
        self.require_approval()
        approval_id = self.client.post(self.url("publish"), json={}, headers=self.admin).json()["approval_id"]
        self.assertFalse([c for c in self.approval(approval_id)["checks"] if c.get("kind") == "test_cases"])
        with self.session_factory() as db:
            self.assertIsNone(db.scalar(select(AssistantTestRun.id)))

    # -- console runs of a specific version -----------------------------------------------------------

    def test_console_can_run_a_specific_published_version(self) -> None:
        published = self.client.post(self.url("publish"), json={}, headers=self.admin).json()
        started = self.client.post(self.url("runs"), json={"message": "hi", "version_id": published["id"]}, headers=self.admin)
        self.assertEqual(started.status_code, 202, started.text)
        run = self.client.get(f"/api/v1/runs/{started.json()['run_id']}", headers=self.admin).json()
        self.assertEqual(run["id"], started.json()["run_id"])
        with self.session_factory() as db:
            self.assertEqual(db.scalar(select(Run.version_id)), UUID(published["id"]))
        missing = self.client.post(self.url("runs"), json={"message": "hi", "version_id": "00000000-0000-4000-8000-000000000001"}, headers=self.admin)
        self.assertEqual(missing.status_code, 404)

    def test_llm_judge_reads_text_parts_of_list_content(self) -> None:
        # ``AIMessage`` content may be a list of parts for some providers; it is serialised instead of crashing.
        class ListContent(ScriptedChat):
            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                from langchain_core.outputs import ChatGeneration, ChatResult

                return ChatResult(generations=[ChatGeneration(message=AIMessage(content=[{"type": "text", "text": '{"pass": true}'}]))])

        self.assertEqual(llm_judge(ListContent())(input="a", expected="b", actual="c").passed, True)


if __name__ == "__main__":
    unittest.main()
