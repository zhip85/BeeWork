import time
import unittest
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import openai
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from beework.db import get_db
from beework.main import app
from beework.models import (
    AuditEvent,
    Base,
    Membership,
    Organization,
    PlatformModel,
    Tenant,
    TenantModelEnvironment,
    TenantModelSelection,
    User,
)
from beework.models.assistants import Assistant
from beework.models.jobs import Job, RunEvent
from beework.models.runtime import Run
from beework.runtime import compiler
from beework.runtime.compiler import (
    STAGE_ORDER,
    RuntimeDeps,
    StageClock,
    compile_assistant,
    register_stage,
)
from beework.runtime.dsl import AssistantDSLv1, dsl_from_draft
from beework.runtime.errors import RuntimeFailure
from beework.runtime.executor import execute_run
from beework.runtime.llm import build_chat_model
from beework.runtime.retrieval import RetrievalResult, RetrievedChunk, StubRetriever
from beework.security import hash_password
from beework.worker.queue import claim_jobs, execute_job
from isolation import disable_external_integrations, stub_public_dns

PASSWORD = "correct horse battery"


class ScriptedChat(BaseChatModel):
    """Streams scripted chunks; optional hook runs before each chunk (to cancel, sleep or fail)."""

    chunks: list[str] = []
    usage: tuple[int, int] | None = (11, 5)
    before_chunk: Any = None
    error: Any = None
    seen_messages: list = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        # Non-streaming calls (e.g. memory extraction) get the scripted chunks joined into one message.
        chunks = list(self._stream(messages, stop=stop, run_manager=run_manager, **kwargs))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="".join(c.message.content for c in chunks)))])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs) -> Iterator[ChatGenerationChunk]:
        self.seen_messages.append(messages)
        if self.error is not None:
            raise self.error
        for index, text in enumerate(self.chunks):
            if self.before_chunk:
                self.before_chunk(index)
            last = index == len(self.chunks) - 1
            message = AIMessageChunk(content=text)
            if last and self.usage:
                message = AIMessageChunk(
                    content=text,
                    usage_metadata={"input_tokens": self.usage[0], "output_tokens": self.usage[1],
                                    "total_tokens": sum(self.usage)},
                )
            yield ChatGenerationChunk(message=message)


class RecordingRetriever:
    def __init__(self, chunks: list[RetrievedChunk] | None = None) -> None:
        self.calls: list[dict] = []
        self.chunks = chunks or []

    def retrieve(self, *, tenant_id, query, dataset_ids, top_k) -> RetrievalResult:
        self.calls.append({"tenant_id": tenant_id, "query": query, "dataset_ids": list(dataset_ids), "top_k": top_k})
        return RetrievalResult(chunks=self.chunks, mode="ragflow" if self.chunks else "stub")


def make_model(name: str) -> PlatformModel:
    return PlatformModel(
        display_name=name, provider="private", model_type="chat", availability="all_plans",
        input_price_per_million="1", output_price_per_million="2",
        input_points_per_thousand="1", output_points_per_thousand="2", status="healthy",
        provider_model_id=name.lower().replace(" ", "-"), endpoint_url="https://models.example.test/v1",
        encrypted_api_key="encrypted", api_key_last_four="cret", region="ap_singapore",
    )


class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        disable_external_integrations()
        stub_public_dns()
        cls.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )

        @event.listens_for(cls.engine, "connect")
        def enable_sqlite_foreign_keys(connection, _record) -> None:
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

    def setUp(self) -> None:
        raw = self.engine.raw_connection()
        raw.cursor().execute("PRAGMA foreign_keys=OFF")
        raw.close()
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        self.client = TestClient(app)
        self._stages = dict(compiler.STAGE_FACTORIES)
        with self.session_factory.begin() as db:
            tenant = Tenant(name="Acme", slug="acme", plan="enterprise", seat_limit=20)
            other = Tenant(name="Other", slug="other", plan="enterprise", seat_limit=20)
            db.add_all([tenant, other])
            db.flush()
            root = Organization(tenant_id=tenant.id, name="Acme")
            other_root = Organization(tenant_id=other.id, name="Other")
            db.add_all([root, other_root])
            db.flush()
            users = {}
            for email, name in (
                ("admin@example.com", "Admin"), ("viewer@example.com", "Viewer"), ("other@example.com", "OtherAdmin"),
            ):
                users[email] = User(email=email, display_name=name, password_hash=hash_password(PASSWORD))
                db.add(users[email])
            db.flush()
            db.add_all([
                Membership(tenant_id=tenant.id, user_id=users["admin@example.com"].id, organization_id=root.id, role="tenant_admin"),
                Membership(tenant_id=tenant.id, user_id=users["viewer@example.com"].id, organization_id=root.id, role="viewer"),
                Membership(tenant_id=other.id, user_id=users["other@example.com"].id, organization_id=other_root.id, role="tenant_admin"),
            ])
            chat = make_model("Chat One")
            db.add(chat)
            db.flush()
            environment = TenantModelEnvironment(
                tenant_id=tenant.id, environment="production", gateway_sync_status="synced",
                gateway_key_encrypted="unused-in-tests",
            )
            db.add(environment)
            db.flush()
            db.add(TenantModelSelection(environment_id=environment.id, model_id=chat.id, model_type="chat", is_default=True))
            assistant = Assistant(
                tenant_id=tenant.id, organization_id=root.id, name="Helper", description="d",
                draft_config={"prompt": "You are helpful.", "model_id": str(chat.id), "greeting": "Hi"},
            )
            other_assistant = Assistant(tenant_id=other.id, organization_id=other_root.id, name="Foreign", draft_config={})
            db.add_all([assistant, other_assistant])
            db.flush()
            self.ids = {
                "tenant": tenant.id, "other": other.id, "assistant": assistant.id,
                "other_assistant": other_assistant.id, "chat": chat.id, "environment": environment.id,
            }

    # -- helpers ----------------------------------------------------------------

    def login(self, email: str, tenant: UUID | None = None) -> dict[str, str]:
        self.client.cookies.clear()
        response = self.client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return {"X-CSRF-Token": self.client.cookies.get("beework_csrf"), "X-Tenant-ID": str(tenant or self.ids["tenant"])}

    def start_run(self, headers: dict, message: str = "hello", assistant_id: UUID | None = None):
        return self.client.post(
            f"/api/v1/assistants/{assistant_id or self.ids['assistant']}/runs",
            json={"message": message}, headers=headers,
        )

    def run_in_worker(self, run_id: str, llm: BaseChatModel | None = None, retriever=None) -> None:
        with self.session_factory() as db:
            execute_run(db, UUID(run_id), llm_factory=lambda _run, _dsl: llm or ScriptedChat(chunks=["Hel", "lo"]), retriever=retriever)

    def events(self, run_id: str) -> list[RunEvent]:
        with self.session_factory() as db:
            return list(db.scalars(select(RunEvent).where(RunEvent.run_id == UUID(run_id)).order_by(RunEvent.seq)))

    def run_row(self, run_id: str) -> Run:
        with self.session_factory() as db:
            return db.get(Run, UUID(run_id))

    # -- DSL / compiler ---------------------------------------------------------

    def test_dsl_from_draft_validates_and_aliases_model(self) -> None:
        with self.session_factory() as db:
            assistant = db.get(Assistant, self.ids["assistant"])
            dsl = dsl_from_draft(assistant)
            self.assertEqual(dsl.model_alias, f"bw-{self.ids['chat']}")
            self.assertEqual((dsl.dsl_version, dsl.knowledge.dataset_ids), (1, []))
            assistant.draft_config = {"prompt": "x"}
            with self.assertRaises(RuntimeFailure) as caught:
                dsl_from_draft(assistant)
            self.assertEqual(caught.exception.code, "runtime.model_unavailable")
            assistant.draft_config = {"model_id": str(self.ids["chat"]), "prompt": " "}
            with self.assertRaises(RuntimeFailure):
                dsl_from_draft(assistant)
        with self.assertRaises(ValueError):
            AssistantDSLv1.model_validate({"dsl_version": 2, "prompt": "x", "model_alias": "a"})

    def test_compiled_graph_has_the_ten_oc_stages_in_fixed_order(self) -> None:
        dsl = AssistantDSLv1(prompt="p", model_alias="bw-x")
        graph = compile_assistant(
            dsl,
            RuntimeDeps(
                llm=ScriptedChat(chunks=["ok"]), retriever=StubRetriever(), tenant_id=uuid4(),
                allowed_dataset_ids=lambda ids: [],
            ),
        )
        self.assertEqual(
            STAGE_ORDER,
            ("ingress", "admission", "pii_input", "memory", "glossary_rewrite", "route", "execute",
             "components", "pii_output", "egress"),
        )
        self.assertTrue(set(compiler.STAGE_FACTORIES) <= set(STAGE_ORDER))  # unregistered stages pass through
        nodes = [name for name in graph.get_graph().nodes if not name.startswith("__")]
        self.assertEqual(nodes, list(STAGE_ORDER))
        state = graph.invoke({"input": " hi ", "history": []})
        self.assertEqual((state["text"], state["retrieval_mode"], state["usage"]), ("ok", "stub", {"input": 11, "output": 5}))

    def test_input_guard_rejects_empty_and_oversized_messages(self) -> None:
        dsl = AssistantDSLv1(prompt="p", model_alias="bw-x", limits={"max_input_chars": 5})
        graph = compile_assistant(
            dsl,
            RuntimeDeps(llm=ScriptedChat(chunks=["ok"]), retriever=StubRetriever(), tenant_id=uuid4(), allowed_dataset_ids=lambda ids: []),
        )
        for message in ("   ", "123456"):
            with self.assertRaises(RuntimeFailure) as caught:
                graph.invoke({"input": message, "history": []})
            self.assertEqual(caught.exception.code, "runtime.input_rejected")

    def test_retrieve_only_passes_tenant_owned_dataset_ids_and_injects_context(self) -> None:
        retriever = RecordingRetriever([
            RetrievedChunk("Handbook.pdf", "c1", "Refunds take 5 days.", 0.9),
            RetrievedChunk("Handbook.pdf", "c2", "low score", 0.1),
        ])
        llm = ScriptedChat(chunks=["ok"])
        dsl = AssistantDSLv1(prompt="p", model_alias="bw-x", knowledge={"dataset_ids": ["ds-mine", "ds-forged"]})
        tenant_id = uuid4()
        graph = compile_assistant(
            dsl,
            RuntimeDeps(
                llm=llm, retriever=retriever, tenant_id=tenant_id,
                allowed_dataset_ids=lambda requested: [d for d in requested if d == "ds-mine"],
            ),
        )
        state = graph.invoke({"input": "refund?", "history": []})
        self.assertEqual(retriever.calls[0]["dataset_ids"], ["ds-mine"])
        self.assertEqual(retriever.calls[0]["tenant_id"], tenant_id)
        self.assertEqual([c["chunk_id"] for c in state["context"]], ["c1"])  # below min_score is dropped
        system_prompt = llm.seen_messages[0][0].content
        self.assertIn("Refunds take 5 days.", system_prompt)
        self.assertNotIn("low score", system_prompt)

    def test_forged_dataset_ids_never_reach_the_retriever_through_the_executor(self) -> None:
        headers = self.login("admin@example.com")
        run_id = self.start_run(headers).json()["run_id"]
        with self.session_factory.begin() as db:
            run = db.get(Run, UUID(run_id))
            run.config = {**run.config, "knowledge": {"dataset_ids": ["other-tenants-dataset"]}}
        retriever = RecordingRetriever()
        self.run_in_worker(run_id, retriever=retriever)
        self.assertEqual(retriever.calls[0]["dataset_ids"], [])  # no knowledge base can be proven to be the tenant's yet
        self.assertEqual(self.run_row(run_id).status, "succeeded")

    def test_chat_model_targets_litellm_with_virtual_key_and_metadata(self) -> None:
        dsl = AssistantDSLv1(prompt="p", model_alias="bw-abc")
        model = build_chat_model(dsl, api_key="sk-virtual", metadata={"tenant_id": "t1", "run_id": "r1"})
        self.assertEqual(model.model_name, "bw-abc")
        self.assertTrue(model.streaming)
        self.assertEqual(model.extra_body, {"metadata": {"tenant_id": "t1", "run_id": "r1"}})
        self.assertEqual(model.openai_api_key.get_secret_value(), "sk-virtual")
        self.assertTrue(str(model.openai_api_base).endswith("/v1"))

    # -- API: admission ---------------------------------------------------------

    def test_start_run_enqueues_job_and_first_event_atomically(self) -> None:
        headers = self.login("admin@example.com")
        response = self.start_run(headers)
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()["run_id"]
        run = self.run_row(run_id)
        self.assertEqual((run.status, run.source, run.environment, run.draft_row_version), ("queued", "debug", "production", 1))
        self.assertEqual(run.config["model_alias"], f"bw-{self.ids['chat']}")  # frozen DSL, not a reference to the draft
        self.assertEqual([e.type for e in self.events(run_id)], ["queued"])
        with self.session_factory() as db:
            job = db.scalar(select(Job).where(Job.kind == "runtime.execute"))
            self.assertEqual(job.payload, {"run_id": run_id})
            self.assertEqual(job.tenant_id, self.ids["tenant"])
            audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "assistant.run_started"))
            self.assertEqual(audit.details["run_id"], run_id)

    def test_admission_rejections(self) -> None:
        headers = self.login("admin@example.com")
        self.assertEqual(self.client.post(
            f"/api/v1/assistants/{self.ids['assistant']}/runs", json={"message": ""}, headers=headers).status_code, 422)
        self.assertEqual(self.client.post(
            f"/api/v1/assistants/{self.ids['assistant']}/runs", json={"message": "x" * 4001}, headers=headers).status_code, 422)

        def code_after(mutate) -> tuple[int, str]:
            with self.session_factory.begin() as db:
                mutate(db)
            response = self.start_run(headers)
            return response.status_code, response.json().get("code", "")

        with self.session_factory.begin() as db:
            db.get(Assistant, self.ids["assistant"]).status = "disabled"
        self.assertEqual(code_after(lambda db: None), (409, "runtime.assistant_disabled"))
        with self.session_factory.begin() as db:
            db.get(Assistant, self.ids["assistant"]).status = "draft"
        self.assertEqual(
            code_after(lambda db: setattr(db.get(TenantModelEnvironment, self.ids["environment"]), "gateway_sync_status", "failed")),
            (409, "runtime.gateway_not_ready"),
        )
        with self.session_factory.begin() as db:
            db.get(TenantModelEnvironment, self.ids["environment"]).gateway_sync_status = "synced"
        self.assertEqual(
            code_after(lambda db: db.query(TenantModelSelection).delete()), (409, "runtime.model_unavailable")
        )

    def test_permissions_and_tenant_isolation(self) -> None:
        self.assertEqual(self.client.post(
            f"/api/v1/assistants/{self.ids['assistant']}/runs", json={"message": "hi"}).status_code, 401)
        viewer = self.login("viewer@example.com")
        self.assertEqual(self.start_run(viewer).status_code, 403)  # viewers cannot test assistants
        admin = self.login("admin@example.com")
        run_id = self.start_run(admin).json()["run_id"]

        other = self.login("other@example.com", tenant=self.ids["other"])
        self.assertEqual(self.start_run(other).status_code, 404)  # foreign assistant
        self.assertEqual(self.client.get(f"/api/v1/runs/{run_id}", headers=other).status_code, 404)
        self.assertEqual(self.client.post(f"/api/v1/runs/{run_id}/cancel", headers=other).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/runs/{run_id}/events", headers=other).status_code, 404)

        viewer = self.login("viewer@example.com")
        self.assertEqual(self.client.get(f"/api/v1/runs/{run_id}", headers=viewer).status_code, 200)
        self.assertEqual(self.client.post(f"/api/v1/runs/{run_id}/cancel", headers=viewer).status_code, 403)

    def test_read_only_tenant_cannot_start_runs(self) -> None:
        headers = self.login("admin@example.com")
        with self.session_factory.begin() as db:
            db.get(Tenant, self.ids["tenant"]).status = "read_only"
        self.assertEqual(self.start_run(headers).status_code, 403)

    # -- execution --------------------------------------------------------------

    def test_successful_run_streams_events_and_records_result(self) -> None:
        headers = self.login("admin@example.com")
        run_id = self.start_run(headers, "hello there").json()["run_id"]
        self.run_in_worker(run_id, ScriptedChat(chunks=["Hel", "lo ", "world"]))
        events = self.events(run_id)
        types = [e.type for e in events]
        self.assertEqual(types[0], "queued")
        self.assertEqual(types[1], "running")
        self.assertEqual(types[-1], "done")
        # The default policy redacts personal information, so token events are dropped once the run
        # is finished and the ``done`` event carries the (redacted) answer.
        self.assertNotIn("token", types)
        steps = [(e.data["name"], e.data["status"]) for e in events if e.type == "step"]
        self.assertEqual(steps, [(n, s) for n in STAGE_ORDER for s in ("started", "completed")])
        seqs = [e.seq for e in events]
        self.assertEqual(seqs, sorted(set(seqs)))  # strictly increasing (dropped token events leave gaps)
        done = events[-1].data
        self.assertEqual((done["text"], done["usage"], done["retrieval"]["mode"]), ("Hello world", {"input": 11, "output": 5}, "stub"))
        run = self.run_row(run_id)
        self.assertEqual((run.status, run.tokens_in, run.tokens_out, run.retrieval_mode), ("succeeded", 11, 5, "stub"))
        self.assertEqual(run.output["text"], "Hello world")
        self.assertIsNotNone(run.latency_ms)
        detail = self.client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
        self.assertEqual((detail["status"], detail["retrieval_mode"]), ("succeeded", "stub"))
        streamed = self.client.get(f"/api/v1/runs/{run_id}/events", headers=headers)
        self.assertEqual(streamed.status_code, 200)
        self.assertIn("event: done", streamed.text)

    def test_execution_is_idempotent(self) -> None:
        headers = self.login("admin@example.com")
        run_id = self.start_run(headers).json()["run_id"]
        self.run_in_worker(run_id)
        before = len(self.events(run_id))
        self.run_in_worker(run_id)
        self.assertEqual(len(self.events(run_id)), before)

    def test_worker_job_runs_the_registered_handler(self) -> None:
        from beework.runtime import jobs  # noqa: F401  registers runtime.execute

        headers = self.login("admin@example.com")
        run_id = self.start_run(headers).json()["run_id"]
        with self.session_factory() as db:
            claimed = claim_jobs(db, "test-worker")
            db.commit()
            self.assertEqual([j.kind for j in claimed], ["runtime.execute"])
            job_id = claimed[0].id
        # The gateway key is "unused-in-tests" and cannot be decrypted, so the run fails cleanly
        # instead of crashing the job.
        with self.session_factory() as db:
            execute_job(db, job_id)
            self.assertEqual(db.get(Job, job_id).status, "succeeded")
        run = self.run_row(run_id)
        self.assertEqual((run.status, run.error_code), ("failed", "runtime.gateway_not_ready"))
        self.assertEqual(self.events(run_id)[-1].type, "error")

    def test_model_errors_become_failed_runs_without_leaking_details(self) -> None:
        headers = self.login("admin@example.com")
        request = httpx.Request("POST", "https://gateway.test/v1/chat/completions")
        cases = [
            (openai.APIStatusError("sk-secret-key leaked", response=httpx.Response(401, request=request), body=None), "runtime.gateway_denied"),
            (openai.APIStatusError("slow down", response=httpx.Response(429, request=request), body=None), "runtime.rate_limited"),
            (openai.APIStatusError("boom sk-secret", response=httpx.Response(500, request=request), body=None), "runtime.model_error"),
            (openai.APITimeoutError(request=request), "runtime.model_timeout"),
            (openai.APIConnectionError(request=request), "runtime.gateway_unreachable"),
            (ValueError("unexpected"), "runtime.internal"),
        ]
        for error, code in cases:
            run_id = self.start_run(headers).json()["run_id"]
            self.run_in_worker(run_id, ScriptedChat(chunks=["x"], error=error))
            run = self.run_row(run_id)
            self.assertEqual((run.status, run.error_code), ("failed", code), code)
            last = self.events(run_id)[-1]
            self.assertEqual((last.type, last.data["code"]), ("error", code))
            self.assertNotIn("secret", f"{run.error} {last.data}")

    def test_input_rejection_fails_run_with_code(self) -> None:
        headers = self.login("admin@example.com")
        run_id = self.start_run(headers, "   hello").json()["run_id"]
        with self.session_factory.begin() as db:
            run = db.get(Run, UUID(run_id))
            run.input = {"message": "   "}
        self.run_in_worker(run_id)
        run = self.run_row(run_id)
        self.assertEqual((run.error_code, run.error_stage), ("runtime.input_rejected", "ingress"))
        self.assertEqual(self.events(run_id)[-1].data["stage"], "ingress")

    def test_run_times_out(self) -> None:
        headers = self.login("admin@example.com")
        run_id = self.start_run(headers).json()["run_id"]
        with self.session_factory.begin() as db:
            run = db.get(Run, UUID(run_id))
            run.config = {**run.config, "limits": {"timeout_seconds": 5, "max_tokens": 64, "max_input_chars": 100}}
        from beework.runtime import executor

        real_monotonic = time.monotonic
        clock = {"offset": 0.0}
        original = executor.time.monotonic
        executor.time.monotonic = lambda: real_monotonic() + clock["offset"]
        try:
            llm = ScriptedChat(chunks=["a", "b", "c"], before_chunk=lambda i: clock.__setitem__("offset", 100.0) if i == 1 else None)
            self.run_in_worker(run_id, llm)
        finally:
            executor.time.monotonic = original
        run = self.run_row(run_id)
        self.assertEqual((run.status, run.error_code), ("failed", "runtime.timeout"))
        self.assertEqual(self.events(run_id)[-1].type, "error")

    # -- OC pipeline ----------------------------------------------------------------

    def restore_stage(self, name: str) -> None:
        if name in self._stages:
            compiler.STAGE_FACTORIES[name] = self._stages[name]
        else:
            compiler.STAGE_FACTORIES.pop(name, None)

    def test_every_stage_records_timing_and_pipeline_overhead_excludes_model_time(self) -> None:
        headers = self.login("admin@example.com")
        run_id = self.start_run(headers).json()["run_id"]
        self.run_in_worker(run_id, ScriptedChat(chunks=["a", "b"], before_chunk=lambda i: time.sleep(0.05)))
        run = self.run_row(run_id)
        self.assertEqual(list(run.stage_ms), list(STAGE_ORDER))
        self.assertGreaterEqual(run.stage_ms["execute"], 100)  # the slow model chunks
        self.assertLess(run.pipeline_ms, run.stage_ms["execute"])  # ...are not pipeline overhead
        self.assertLessEqual(run.pipeline_ms, run.latency_ms)
        detail = self.client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
        self.assertEqual((detail["pipeline_ms"], list(detail["stage_ms"])), (run.pipeline_ms, list(STAGE_ORDER)))

    def test_stage_error_fails_the_run_and_names_the_stage(self) -> None:
        @register_stage("pii_input", replace=True)
        def failing(dsl, deps):
            def node(state):
                raise RuntimeFailure("runtime.pii_blocked", "输入被拦截")

            return node

        try:
            headers = self.login("admin@example.com")
            run_id = self.start_run(headers).json()["run_id"]
            self.run_in_worker(run_id)
        finally:
            self.restore_stage("pii_input")
        run = self.run_row(run_id)
        self.assertEqual((run.status, run.error_code, run.error_stage), ("failed", "runtime.pii_blocked", "pii_input"))
        events = self.events(run_id)
        self.assertEqual((events[-1].type, events[-1].data["stage"]), ("error", "pii_input"))
        steps = [(e.data["name"], e.data["status"]) for e in events if e.type == "step"]
        self.assertEqual(steps[-1], ("pii_input", "failed"))
        self.assertNotIn(("memory", "started"), steps)  # later stages never ran
        self.assertIn("pii_input", run.stage_ms)

    def test_unexpected_stage_exception_is_attributed_to_its_stage(self) -> None:
        @register_stage("glossary_rewrite", replace=True)
        def broken(dsl, deps):
            def node(state):
                raise ValueError("secret detail")

            return node

        try:
            headers = self.login("admin@example.com")
            run_id = self.start_run(headers).json()["run_id"]
            self.run_in_worker(run_id)
        finally:
            self.restore_stage("glossary_rewrite")
        run = self.run_row(run_id)
        self.assertEqual((run.error_code, run.error_stage), ("runtime.internal", "glossary_rewrite"))
        self.assertNotIn("secret", f"{run.error} {self.events(run_id)[-1].data}")

    def test_a_registered_memory_stage_injects_into_the_system_prompt(self) -> None:
        @register_stage("memory", replace=True)
        def memory_stage(dsl, deps):
            return lambda state: {"memory": [{"file": "UserMemory", "text": "prefers short answers"}]}

        llm = ScriptedChat(chunks=["ok"])
        try:
            graph = compile_assistant(
                AssistantDSLv1(prompt="base prompt", model_alias="bw-x"),
                RuntimeDeps(llm=llm, retriever=StubRetriever(), tenant_id=uuid4(), allowed_dataset_ids=lambda ids: []),
            )
            graph.invoke({"input": "hi", "history": []})
        finally:
            self.restore_stage("memory")
        system_prompt = llm.seen_messages[0][0].content
        self.assertTrue(system_prompt.startswith("base prompt"))
        self.assertIn("[UserMemory] prefers short answers", system_prompt)

    def test_stage_registry_rejects_unknown_and_duplicate_stages(self) -> None:
        with self.assertRaises(ValueError):
            register_stage("not_a_stage")(lambda dsl, deps: lambda state: {})
        with self.assertRaises(RuntimeError):
            register_stage("ingress")(lambda dsl, deps: lambda state: {})

    def test_stage_clock_separates_external_time(self) -> None:
        clock = StageClock()
        clock.stage_ms.update({"execute": 300, "ingress": 5})
        clock.external_seconds = 0.29
        self.assertEqual(clock.pipeline_ms, 15)

    def test_spans_are_opened_per_stage_with_fixed_names(self) -> None:
        opened: list[str] = []
        from contextlib import contextmanager

        @contextmanager
        def span(name):
            opened.append(name)
            yield

        graph = compile_assistant(
            AssistantDSLv1(prompt="p", model_alias="bw-x"),
            RuntimeDeps(
                llm=ScriptedChat(chunks=["ok"]), retriever=StubRetriever(), tenant_id=uuid4(),
                allowed_dataset_ids=lambda ids: [], span=span,
            ),
        )
        graph.invoke({"input": "hi", "history": []})
        self.assertEqual(opened, list(STAGE_ORDER))

    # -- OC configuration -----------------------------------------------------------

    def test_oc_config_defaults_bounds_and_unknown_keys(self) -> None:
        from pydantic import ValidationError

        from beework.runtime.oc import OCConfig

        config = OCConfig()
        self.assertEqual(config.oc_schema_version, 1)
        self.assertEqual(
            (config.memory.short_term.window_rounds, config.memory.short_term.max_tokens, config.memory.short_term.type_whitelist),
            (3, 5000, ["preference", "history"]),
        )
        self.assertEqual(
            (config.memory.long_term.user_profile.top_k, config.memory.long_term.user_memory.top_k,
             config.memory.long_term.session.retention_days),
            (2, 3, 30),
        )
        for bad in ({"pii": {"enabled": True, "typo": 1}}, {"memory": {"short_term": {"window_rounds": 31}}}, {"oc_schema_version": 2}, {"nope": {}}):
            with self.assertRaises(ValidationError):
                OCConfig.model_validate(bad)

    def test_draft_oc_is_frozen_into_the_run_config(self) -> None:
        with self.session_factory.begin() as db:
            assistant = db.get(Assistant, self.ids["assistant"])
            assistant.draft_config = {**assistant.draft_config, "oc": {"memory": {"short_term": {"window_rounds": 7}}}}
        headers = self.login("admin@example.com")
        run_id = self.start_run(headers).json()["run_id"]
        self.assertEqual(self.run_row(run_id).config["oc"]["memory"]["short_term"]["window_rounds"], 7)
        self.assertEqual(self.run_row(run_id).config["oc"]["oc_schema_version"], 1)

    # -- cancel -----------------------------------------------------------------

    def test_cancelling_a_queued_run_finishes_it_immediately(self) -> None:
        headers = self.login("admin@example.com")
        run_id = self.start_run(headers).json()["run_id"]
        cancelled = self.client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers)
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.assertEqual(self.events(run_id)[-1].type, "cancelled")
        self.assertEqual(self.client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers).status_code, 409)
        self.run_in_worker(run_id)  # a late worker leaves it alone
        self.assertEqual(self.run_row(run_id).status, "cancelled")
        with self.session_factory() as db:
            self.assertIsNotNone(db.scalar(select(AuditEvent).where(AuditEvent.action == "assistant.run_cancelled")))

    def test_cancelling_a_running_run_stops_streaming(self) -> None:
        headers = self.login("admin@example.com")
        run_id = self.start_run(headers).json()["run_id"]

        def request_cancel(index: int) -> None:
            if index == 1:
                with self.session_factory.begin() as db:
                    db.get(Run, UUID(run_id)).cancel_requested = True
                time.sleep(0.6)  # let the cancel poll interval elapse

        self.run_in_worker(run_id, ScriptedChat(chunks=["a", "b", "c", "d"], before_chunk=request_cancel))
        run = self.run_row(run_id)
        self.assertEqual(run.status, "cancelled")
        types = [e.type for e in self.events(run_id)]
        self.assertEqual(types[-1], "cancelled")
        self.assertNotIn("done", types)


if __name__ == "__main__":
    unittest.main()
