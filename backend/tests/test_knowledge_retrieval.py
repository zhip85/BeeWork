"""T-033: mounting knowledge bases on assistants and retrieving from them at run time.

The engine side is the in-process fake RAGFlow / Dify of ``scripts/fake_ragflow.py`` (real HTTP, real clients), so a
document is genuinely uploaded, indexed and searched; only the model is scripted.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

import test_knowledge_documents as knowledge_documents_tests
from beework.approvals import ensure_policies
from beework.db import get_db
from beework.integrations.dify import DifyClient
from beework.integrations.knowledge_engine import EngineChunk, KnowledgeEngineError
from beework.integrations.ragflow import RAGFlowClient
from beework.main import app
from beework.models import Organization, TenantModelEnvironment, TenantModelSelection
from beework.models.assistants import Assistant
from beework.models.conversations import Message
from beework.models.knowledge import KnowledgeBase
from beework.models.runtime import Run
from beework.runtime.dsl import KnowledgeBaseRef, dsl_from_draft
from beework.runtime.executor import execute_run
from beework.runtime.retrieval import EngineRetriever
from test_knowledge_documents import MARKDOWN
from test_runtime import ScriptedChat, make_model

QUESTION = "购买后 7 天内可申请无理由退货吗"


class _Transport(httpx.BaseTransport):
    def __init__(self, handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


class EngineSearchContractTests(unittest.TestCase):
    def test_ragflow_search_sends_the_dataset_ids_and_filters_by_threshold(self) -> None:
        transport = _Transport(lambda request: httpx.Response(200, json={"code": 0, "data": {"chunks": [
            {"id": "c1", "content": "A", "document_keyword": "a.md", "dataset_id": "ds1", "similarity": 0.9},
            {"id": "c2", "content": "B", "document_keyword": "b.md", "dataset_id": "ds1", "similarity": 0.4},
            {"id": "c3", "content": "C", "document_keyword": "c.md", "dataset_id": "ds2", "similarity": 0.7},
        ]}}))
        with RAGFlowClient("http://ragflow", "key", transport=transport) as client:
            found = client.search(["ds1", "ds2"], "q", top_k=5, threshold=0.6)
        self.assertEqual([(c.chunk_id, c.dataset_id, c.score) for c in found], [("c1", "ds1", 0.9), ("c3", "ds2", 0.7)])
        body = json.loads(transport.requests[0].content)
        self.assertEqual((body["dataset_ids"], body["question"], body["similarity_threshold"]), (["ds1", "ds2"], "q", 0.6))
        self.assertEqual(RAGFlowClient("http://ragflow", "key", transport=transport).search([], "q", top_k=3, threshold=0.5), [])

    def test_dify_search_queries_each_knowledge_base_and_merges_by_score(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            dataset = request.url.path.split("/")[-2]
            score = {"d1": 0.8, "d2": 0.95}[dataset]
            return httpx.Response(200, json={"records": [
                {"segment": {"id": f"{dataset}-s", "content": f"text {dataset}", "document": {"name": f"{dataset}.md"}}, "score": score},
                {"segment": {"id": f"{dataset}-low", "content": "x", "document": {"name": "x"}}, "score": 0.1},
            ]})

        transport = _Transport(handler)
        with DifyClient("http://dify/v1", "key", transport=transport) as client:
            found = client.search(["d1", "d2"], "q", top_k=5, threshold=0.5)
        self.assertEqual([(c.chunk_id, c.document_name) for c in found], [("d2-s", "d2.md"), ("d1-s", "d1.md")])
        sent = json.loads(transport.requests[0].content)
        self.assertEqual((sent["query"], sent["retrieval_model"]["score_threshold"], sent["retrieval_model"]["top_k"]), ("q", 0.5, 5))
        self.assertEqual([r.url.path for r in transport.requests], ["/v1/datasets/d1/retrieve", "/v1/datasets/d2/retrieve"])

    def test_an_engine_error_surfaces_as_a_knowledge_engine_error(self) -> None:
        transport = _Transport(lambda request: httpx.Response(500, json={"message": "boom"}))
        with DifyClient("http://dify/v1", "key", transport=transport) as client, self.assertRaises(KnowledgeEngineError):
            client.search(["d1"], "q", top_k=3, threshold=0.5)


class _FakeClient:
    def __init__(self, hits: dict[str, list[EngineChunk]], log: list) -> None:
        self.hits, self.log = hits, log

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def search(self, dataset_ids, query, *, top_k, threshold):
        self.log.append((list(dataset_ids), query, top_k, threshold))
        return [chunk for dataset_id in dataset_ids for chunk in self.hits.get(dataset_id, [])]


class EngineRetrieverTests(unittest.TestCase):
    def retriever(self, hits: dict[str, list[EngineChunk]]):
        log: list = []
        opened: list[str] = []

        def open_client(engine: str):
            opened.append(engine)
            return _FakeClient(hits, log)

        return EngineRetriever(open_client), log, opened

    def base(self, **overrides) -> KnowledgeBaseRef:
        return KnowledgeBaseRef(kb_id=uuid4(), name="KB", dataset_id="docs", faq_dataset_id="faq", threshold=0.6, **overrides)

    def test_no_base_mounted_means_no_search_and_mode_none(self) -> None:
        retriever, log, opened = self.retriever({})
        result = retriever.retrieve(tenant_id=uuid4(), query="q", bases=[], top_k=5)
        self.assertEqual((result.chunks, result.mode, log, opened), ([], "none", [], []))

    def test_a_confident_faq_match_is_the_answer_and_documents_are_not_used(self) -> None:
        hits = {
            "faq": [EngineChunk("faq", "f1", "FAQ", "Q/A", 0.9)],
            "docs": [EngineChunk("docs", "d1", "manual.md", "passage", 0.95)],
        }
        retriever, _, _ = self.retriever(hits)
        result = retriever.retrieve(tenant_id=uuid4(), query="q", bases=[self.base()], top_k=3)
        self.assertEqual([(c.kind, c.chunk_id) for c in result.chunks], [("faq", "f1")])
        self.assertEqual(result.mode, "dify")  # the default engine of a knowledge base reference

    def test_documents_are_the_fallback_when_no_faq_matches(self) -> None:
        retriever, log, _ = self.retriever({"docs": [
            EngineChunk("docs", "d1", "manual.md", "one", 0.7), EngineChunk("docs", "d2", "manual.md", "two", 0.9),
        ]})
        result = retriever.retrieve(tenant_id=uuid4(), query="q", bases=[self.base()], top_k=1)
        self.assertEqual([(c.kind, c.chunk_id, c.kb_name) for c in result.chunks], [("document", "d2", "KB")])
        self.assertIn((["faq"], "q", 1, 0.6), log)  # FAQ searched first, with the knowledge base's threshold
        self.assertIn((["docs"], "q", 1, 0.6), log)

    def test_only_the_given_bases_datasets_are_searched_across_engines(self) -> None:
        retriever, log, opened = self.retriever({"a-docs": [EngineChunk("a-docs", "x", "a.md", "t", 0.8)]})
        bases = [
            KnowledgeBaseRef(kb_id=uuid4(), engine="ragflow", dataset_id="a-docs", threshold=0.5),
            KnowledgeBaseRef(kb_id=uuid4(), engine="dify", dataset_id="b-docs", threshold=0.5),
        ]
        result = retriever.retrieve(tenant_id=uuid4(), query="q", bases=bases, top_k=5)
        self.assertEqual(sorted(opened), ["dify", "ragflow"])
        self.assertEqual({tuple(dataset_ids) for dataset_ids, *_ in log}, {("a-docs",), ("b-docs",)})
        self.assertEqual(result.mode, "ragflow+dify")


# The base class is deleted from its module (so only concrete tests are collected there): take it from a subclass.
class _RetrievalHarness(knowledge_documents_tests.DocumentTests.__mro__[1]):
    kb_engine = "ragflow"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        # Graph nodes use their own sessions in worker threads: a file database, not one shared connection.
        cls._db_dir = tempfile.mkdtemp(prefix="beework-test-")
        cls.engine.dispose()
        cls.engine = create_engine(f"sqlite:///{cls._db_dir}/test.db", connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(cls.engine, "connect")
        def pragmas(connection, _record) -> None:
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
        super().tearDownClass()
        shutil.rmtree(cls._db_dir, ignore_errors=True)

    def setUp(self) -> None:
        # A fresh database file per test: each pooled connection has its own foreign-key pragma, so the harness's
        # drop_all would trip over the previous test's rows.
        self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self._db_dir}/test.db{suffix}").unlink(missing_ok=True)
        super().setUp()
        with self.session_factory.begin() as db:
            ensure_policies(db, self.tenant_id)["publish"].enabled = False  # these tests publish directly
            root = db.scalar(select(Organization).where(Organization.tenant_id == self.tenant_id, Organization.name == "Acme"))
            chat = make_model("Chat Know")
            db.add(chat)
            db.flush()
            environment = TenantModelEnvironment(tenant_id=self.tenant_id, environment="production", gateway_sync_status="synced", gateway_key_encrypted="x")
            db.add(environment)
            db.flush()
            db.add(TenantModelSelection(environment_id=environment.id, model_id=chat.id, model_type="chat", is_default=True))
            self.chat_id, self.root_id = chat.id, root.id
        self.admin = self.login("admin@example.com")

    # -- helpers ----------------------------------------------------------------

    def indexed_kb(self, name: str = "产品知识库", text: bytes = MARKDOWN) -> str:
        kb_id = self.kb(self.admin, name=name)
        self.assertEqual(self.upload(self.admin, kb_id, f"{name}.md", text).status_code, 201)
        self.drain()
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=self.admin).json()["status"], "indexed")
        return kb_id

    def assistant(self, kb_ids: list[str] | None = None, name: str = "Helper"):
        return self.client.post(
            "/api/v1/assistants", headers=self.admin,
            json={"name": name, "description": "d", "config": {"prompt": "You are helpful.", "mounts": {"kb": kb_ids or []}}},
        )

    def ask(self, assistant_id: str, message: str = QUESTION, llm: ScriptedChat | None = None, target: str = "draft"):
        started = self.client.post(
            f"/api/v1/assistants/{assistant_id}/runs", json={"message": message, "target": target}, headers=self.admin
        )
        self.assertEqual(started.status_code, 202, started.text)
        run_id = UUID(started.json()["run_id"])
        with self.session_factory() as db:
            execute_run(db, run_id, llm_factory=lambda *_: llm or ScriptedChat(chunks=["ok"]))
        return started.json()

    def run_row(self, run_id: str) -> Run:
        with self.session_factory() as db:
            return db.get(Run, UUID(run_id))


class MountingTests(_RetrievalHarness):
    def test_only_indexed_knowledge_bases_are_offered_and_mountable(self) -> None:
        indexed = self.indexed_kb("已索引库")
        empty = self.kb(self.admin, name="空库")
        candidates = self.client.get("/api/v1/capabilities/candidates?kind=kb", headers=self.admin).json()["items"]
        self.assertEqual([(c["id"], c["name"], c["kb_type"]) for c in candidates], [(indexed, "已索引库", "standard")])
        self.assertEqual(self.client.get("/api/v1/capabilities/candidates?kind=kb&q=没有", headers=self.admin).json()["items"], [])
        self.assertEqual(self.client.get("/api/v1/capabilities/candidates?kind=agent", headers=self.admin).status_code, 422)
        ok = self.assistant([indexed])
        self.assertEqual(ok.status_code, 201, ok.text)
        self.assertEqual(ok.json()["config"]["mounts"]["kb"], [indexed])
        draft = self.assistant([empty], name="Second")
        self.assertEqual((draft.status_code, draft.json()["code"]), (422, "assistant.mount_unavailable"))
        self.assertEqual(self.assistant([str(uuid4())], name="Third").status_code, 422)
        viewer = self.login("viewer@example.com")
        self.assertEqual(self.client.get("/api/v1/capabilities/candidates?kind=kb", headers=viewer).status_code, 403)

    def test_another_tenants_or_organizations_knowledge_base_cannot_be_mounted(self) -> None:
        other = self.login("other@example.com", self.other_id)
        foreign = self.client.post("/api/v1/knowledge-bases", headers=other, json={"name": "外部库"}).json()["id"]
        self.admin = self.login("admin@example.com")
        self.assertEqual(self.assistant([foreign]).status_code, 422)
        listed = self.client.get("/api/v1/capabilities/candidates?kind=kb", headers=self.admin).json()["items"]
        self.assertNotIn(foreign, [c["id"] for c in listed])

    def test_the_mount_limit_and_other_kinds(self) -> None:
        ids = [str(uuid4()) for _ in range(11)]
        too_many = self.assistant(ids)
        self.assertEqual((too_many.status_code, too_many.json()["code"]), (422, "assistant.mount_limit"))
        other_kind = self.client.post(
            "/api/v1/assistants", headers=self.admin,
            json={"name": "X", "description": "d", "config": {"prompt": "p", "mounts": {"mcp": [str(uuid4())]}}},
        )
        self.assertEqual((other_kind.status_code, other_kind.json()["code"]), (422, "assistant.mount_unavailable"))

    def test_resolver_states_and_a_stale_mount_does_not_block_other_edits(self) -> None:
        kb_id = self.indexed_kb()
        created = self.assistant([kb_id]).json()
        mounts = self.client.get(f"/api/v1/assistants/{created['id']}/mounts", headers=self.admin).json()
        self.assertEqual(mounts["mountable_kinds"], ["kb"])
        item = mounts["groups"][0]["items"][0]
        self.assertEqual((item["name"], item["state"], item["meta"]["kb_type"], item["meta"]["documents"]), ("产品知识库", "available", "standard", 1))
        # The knowledge base is deleted: the mount is flagged (GLB-083) but the assistant can still be edited.
        self.assertEqual(self.client.delete(f"/api/v1/knowledge-bases/{kb_id}", headers=self.admin).status_code, 204)
        stale = self.client.get(f"/api/v1/assistants/{created['id']}/mounts", headers=self.admin).json()["groups"][0]["items"][0]
        self.assertEqual(stale["state"], "deleted")
        checks = self.client.get(f"/api/v1/assistants/{created['id']}/checks", headers=self.admin).json()
        self.assertTrue(any(issue["severity"] == "high" for issue in checks["issues"]))
        current = self.client.get(f"/api/v1/assistants/{created['id']}", headers=self.admin).json()
        edited = self.client.put(
            f"/api/v1/assistants/{created['id']}", headers=self.admin,
            json={"name": current["name"], "description": "new description", "config": current["config"], "row_version": current["row_version"]},
        )
        self.assertEqual(edited.status_code, 200, edited.text)
        # ...but a deleted knowledge base cannot be mounted again on another assistant.
        self.assertEqual(self.assistant([kb_id], name="Again").status_code, 422)

    def test_references_count_draft_and_live_mounts(self) -> None:
        kb_id = self.indexed_kb()
        created = self.assistant([kb_id]).json()

        def references() -> int:
            return self.client.get(f"/api/v1/knowledge-bases/{kb_id}/references", headers=self.admin).json()["count"]

        self.assertEqual(references(), 1)
        self.assertEqual(self.client.post(f"/api/v1/assistants/{created['id']}/publish", json={}, headers=self.admin).status_code, 201)
        current = self.client.get(f"/api/v1/assistants/{created['id']}", headers=self.admin).json()
        config = {**current["config"], "mounts": {**current["config"]["mounts"], "kb": []}}
        self.assertEqual(self.client.put(
            f"/api/v1/assistants/{created['id']}", headers=self.admin,
            json={"name": current["name"], "description": current["description"], "config": config, "row_version": current["row_version"]},
        ).status_code, 200)
        self.assertEqual(references(), 1)  # unmounted from the draft, still served by the live version
        self.assertEqual(self.assistant([kb_id], name="Other").status_code, 201)
        self.assertEqual(references(), 2)


class RuntimeRetrievalTests(_RetrievalHarness):
    def check_answer_with_citations(self) -> None:
        kb_id = self.indexed_kb()
        assistant = self.assistant([kb_id]).json()
        llm = ScriptedChat(chunks=["可以退货"])
        started = self.ask(assistant["id"], llm=llm)
        system = llm.seen_messages[0][0].content
        self.assertIn("# 参考资料", system)
        self.assertIn("7 天内可申请无理由退货", system)
        run = self.run_row(started["run_id"])
        self.assertEqual((run.status, run.retrieval_mode), ("succeeded", self.kb_engine))
        citations = run.output["retrieval"]["citations"]
        self.assertTrue(citations)
        self.assertEqual((citations[0]["document_name"], citations[0]["kind"], citations[0]["kb_name"]), ("产品知识库.md", "document", "产品知识库"))
        self.assertGreaterEqual(citations[0]["score"], 0.62)
        with self.session_factory() as db:
            reply = db.scalar(select(Message).where(Message.run_id == run.id, Message.role == "assistant"))
            self.assertEqual(reply.citations[0]["document_name"], "产品知识库.md")
        detail = self.client.get(f"/api/v1/conversations/{started['conversation_id']}", headers=self.admin).json()
        self.assertEqual(detail["messages"][-1]["citations"][0]["document_name"], "产品知识库.md")
        self.assertIsNone(detail["messages"][0]["citations"])
        events = self.client.get(f"/api/v1/runs/{started['run_id']}/events", headers=self.admin).text
        self.assertIn('"mode": "' + self.kb_engine + '"', events)

    def test_upload_index_ask_and_get_a_cited_answer(self) -> None:
        self.check_answer_with_citations()

    def test_a_question_the_knowledge_base_cannot_answer_cites_nothing(self) -> None:
        assistant = self.assistant([self.indexed_kb()]).json()
        llm = ScriptedChat(chunks=["不知道"])
        started = self.ask(assistant["id"], message="qwertyuiop 完全无关 zxcvbnm", llm=llm)
        self.assertNotIn("# 参考资料", llm.seen_messages[0][0].content)
        run = self.run_row(started["run_id"])
        self.assertEqual((run.status, run.output["retrieval"]["citations"]), ("succeeded", []))

    def test_an_assistant_without_knowledge_does_not_touch_the_engine(self) -> None:
        assistant = self.assistant([]).json()
        self.fake.fail_next = 5  # any engine call would now fail
        started = self.ask(assistant["id"])
        run = self.run_row(started["run_id"])
        self.assertEqual((run.status, run.retrieval_mode), ("succeeded", "none"))

    def test_an_engine_failure_fails_the_run_at_the_execute_stage(self) -> None:
        assistant = self.assistant([self.indexed_kb()]).json()
        self.fake.fail_next = 5
        started = self.ask(assistant["id"])
        self.fake.fail_next = 0
        run = self.run_row(started["run_id"])
        self.assertEqual((run.status, run.error_code, run.error_stage), ("failed", "runtime.knowledge_error", "execute"))

    def test_a_deleted_knowledge_base_is_no_longer_searched(self) -> None:
        kb_id = self.indexed_kb()
        assistant = self.assistant([kb_id]).json()
        self.assertEqual(self.client.delete(f"/api/v1/knowledge-bases/{kb_id}", headers=self.admin).status_code, 204)
        llm = ScriptedChat(chunks=["ok"])
        started = self.ask(assistant["id"], llm=llm)
        self.assertNotIn("# 参考资料", llm.seen_messages[0][0].content)
        self.assertEqual(self.run_row(started["run_id"]).retrieval_mode, "none")

    def test_the_published_snapshot_records_the_datasets_and_runs_use_the_current_ones(self) -> None:
        from beework import knowledge_mounts  # imported here: the API package must be loaded first

        kb_id = self.indexed_kb()
        assistant = self.assistant([kb_id]).json()
        published = self.client.post(f"/api/v1/assistants/{assistant['id']}/publish", json={}, headers=self.admin).json()
        recorded = published["snapshot"]["dsl"]["knowledge"]["bases"][0]
        with self.session_factory() as db:
            kb = db.get(KnowledgeBase, UUID(kb_id))
            self.assertEqual((recorded["kb_id"], recorded["engine"], recorded["dataset_id"]), (kb_id, kb.engine, kb.engine_dataset_id))
            self.assertEqual(recorded["threshold"], float(kb.retrieval_threshold))
            old_dataset = kb.engine_dataset_id
            kb.engine_dataset_id = "rebuilt-dataset"  # a rebuild swaps the datasets; the snapshot still names the old one
            db.commit()
            current = knowledge_mounts.resolve_knowledge_bases(db, kb.tenant_id, [kb.id])
        self.assertEqual([(ref.dataset_id, ref.name) for ref in current], [("rebuilt-dataset", "产品知识库")])
        self.assertNotEqual(old_dataset, "rebuilt-dataset")
        # The tenant's rows decide: another tenant resolves nothing for this id.
        with self.session_factory() as db:
            self.assertEqual(knowledge_mounts.resolve_knowledge_bases(db, self.other_id, [UUID(kb_id)]), [])

    def test_datasets_without_indexed_content_are_not_searched(self) -> None:
        from beework import knowledge_mounts

        kb_id = self.indexed_kb()
        empty_id = self.kb(self.admin, name="空知识库")
        with self.session_factory() as db:
            refs = knowledge_mounts.resolve_knowledge_bases(db, self.tenant_id, [UUID(kb_id), UUID(empty_id)])
        self.assertEqual([r.kb_id for r in refs], [UUID(kb_id)])  # nothing to find in the empty one
        self.assertIsNotNone(refs[0].dataset_id)
        self.assertIsNone(refs[0].faq_dataset_id)  # no published FAQ: its (empty) FAQ dataset is skipped

    def test_live_runs_retrieve_too_and_the_dsl_needs_a_session_for_mounts(self) -> None:
        kb_id = self.indexed_kb()
        assistant = self.assistant([kb_id]).json()
        self.assertEqual(self.client.post(f"/api/v1/assistants/{assistant['id']}/publish", json={}, headers=self.admin).status_code, 201)
        llm = ScriptedChat(chunks=["ok"])
        started = self.ask(assistant["id"], llm=llm, target="live")
        self.assertIn("7 天内可申请无理由退货", llm.seen_messages[0][0].content)
        self.assertEqual(self.run_row(started["run_id"]).retrieval_mode, self.kb_engine)
        with self.session_factory() as db:
            row = db.get(Assistant, UUID(assistant["id"]))
            with self.assertRaises(ValueError):
                dsl_from_draft(row)
            self.assertEqual([b.kb_id for b in dsl_from_draft(row, db).knowledge.bases], [UUID(kb_id)])


class DifyRuntimeRetrievalTests(RuntimeRetrievalTests):
    kb_engine = "dify"

    def setUp(self) -> None:
        super().setUp()


if __name__ == "__main__":
    unittest.main()
