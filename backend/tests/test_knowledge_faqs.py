"""T-032: FAQ lifecycle, pushing to the knowledge engine, expiry, hit counting and CSV import.

The engine is the in-process fake RAGFlow / Dify of ``scripts/fake_ragflow.py`` (real HTTP, real clients); the model is
scripted. A published FAQ really becomes a document of the knowledge base's FAQ dataset and is found again by search.
"""

import json
import unittest
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import httpx
from sqlalchemy import select

from beework import faqs as faq_service
from beework.integrations.dify import DifyClient
from beework.integrations.ragflow import RAGFlowClient
from beework.models import AuditEvent, Membership, User
from beework.models.faq import Faq, FaqHit
from beework.models.jobs import Job
from beework.models.knowledge import KnowledgeBase
from beework.security import hash_password
from test_knowledge_retrieval import _RetrievalHarness, _Transport
from test_runtime import ScriptedChat

QUESTION = "怎么申请退货？"
ANSWER = "购买后 7 天内可在订单页申请无理由退货。"
ASK = "怎么申请退货"


class EngineFaqContractTests(unittest.TestCase):
    def test_dify_pushes_one_faq_as_a_single_chunk_document(self) -> None:
        transport = _Transport(lambda request: httpx.Response(200, json={"document": {"id": "doc1"}, "batch": "b1"}))
        with DifyClient("http://dify/v1", "key", transport=transport) as client:
            ref = client.add_faq("ds1", QUESTION, ["如何退货", "退款流程"], ANSWER)
        self.assertEqual((ref.document_id, ref.batch_id), ("doc1", "b1"))
        request = transport.requests[0]
        self.assertEqual(request.url.path, "/v1/datasets/ds1/document/create-by-text")
        body = json.loads(request.content)
        self.assertEqual(body["text"], f"{QUESTION}\n如何退货；退款流程\n\n答：{ANSWER}")
        self.assertEqual((body["doc_form"], body["name"]), ("text_model", f"FAQ · {QUESTION}"))
        self.assertEqual(body["process_rule"]["rules"]["segmentation"]["max_tokens"], 1000)

    def test_ragflow_pushes_tab_separated_question_answer_lines(self) -> None:
        uploads: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/documents") and request.method == "POST":
                request.read()
                uploads.append(request.content)
                return httpx.Response(200, json={"code": 0, "data": [{"id": "d1"}]})
            return httpx.Response(200, json={"code": 0})

        with RAGFlowClient("http://ragflow", "key", transport=_Transport(handler)) as client:
            ref = client.add_faq("ds1", "问题\t一", ["相似\n问法"], "答案\n第二行")
        self.assertEqual(ref.document_id, "d1")
        body = uploads[0].decode()
        self.assertIn("问题 一\t答案 第二行", body)
        self.assertIn("相似 问法\t答案 第二行", body)

    def test_dify_search_reports_the_document_a_hit_came_from(self) -> None:
        transport = _Transport(lambda request: httpx.Response(200, json={"records": [
            {"segment": {"id": "s1", "content": "c", "document": {"id": "doc9", "name": "FAQ · q"}}, "score": 0.9}]}))
        with DifyClient("http://dify/v1", "key", transport=transport) as client:
            found = client.search(["ds1"], "q", top_k=3, threshold=0.5)
        self.assertEqual((found[0].document_id, found[0].document_name), ("doc9", "FAQ · q"))


class _FaqHarness(_RetrievalHarness):
    def setUp(self) -> None:
        super().setUp()
        with self.session_factory.begin() as db:
            operator = User(email="operator@example.com", display_name="Operator", password_hash=hash_password("correct horse battery"))
            db.add(operator)
            db.flush()
            root = db.scalar(select(Membership.organization_id).where(Membership.tenant_id == self.tenant_id).limit(1))
            db.add(Membership(tenant_id=self.tenant_id, user_id=operator.id, organization_id=root, role="operator"))
        self.kb_id = self.kb(self.admin, name="客服知识库")

    # -- helpers ----------------------------------------------------------------

    def mk(self, headers=None, **overrides):
        body = {"question": QUESTION, "answer": ANSWER, **overrides}
        return self.client.post(f"/api/v1/knowledge-bases/{self.kb_id}/faqs", json=body, headers=headers or self.admin)

    def faq(self, faq_id: str) -> dict:
        return self.client.get(f"/api/v1/knowledge-bases/{self.kb_id}/faqs/{faq_id}", headers=self.admin).json()

    def act(self, faq_id: str, action: str, headers=None):
        return self.client.post(f"/api/v1/knowledge-bases/{self.kb_id}/faqs/{faq_id}/{action}", headers=headers or self.admin)

    def row(self, faq_id: str) -> Faq:
        with self.session_factory() as db:
            return db.get(Faq, UUID(faq_id))

    def faq_documents(self, kb_id: str | None = None) -> list[dict]:
        with self.session_factory() as db:
            dataset = db.get(KnowledgeBase, UUID(kb_id or self.kb_id)).engine_faq_dataset_id
        return [d for d in self.fake.documents.values() if d["dataset"] == dataset]

    def published(self, **overrides) -> str:
        faq_id = self.mk(status="published", **overrides).json()["id"]
        self.drain()
        return faq_id

    def jobs(self, kind: str) -> list[Job]:
        with self.session_factory() as db:
            return list(db.scalars(select(Job).where(Job.kind == kind)))


class LifecycleTests(_FaqHarness):
    def test_a_draft_stays_out_of_the_engine_until_it_is_published(self) -> None:
        created = self.mk()
        self.assertEqual(created.status_code, 201, created.text)
        faq = created.json()
        self.assertEqual((faq["status"], faq["sync_status"], faq["hits_30d"], faq["created_by"]), ("draft", "none", 0, "Admin"))
        self.drain()
        self.assertEqual(self.faq_documents(), [])
        published = self.act(faq["id"], "publish")
        self.assertEqual((published.status_code, published.json()["status"], published.json()["sync_status"]), (200, "published", "pending"))
        self.drain()
        done = self.faq(faq["id"])
        self.assertEqual((done["sync_status"], done["sync_error"]), ("synced", None))
        documents = self.faq_documents()
        self.assertEqual(len(documents), 1)
        self.assertIn(QUESTION, documents[0]["text"])
        self.assertIn(ANSWER, documents[0]["text"])
        # Unpublishing removes it again and keeps the FAQ as a draft.
        down = self.act(faq["id"], "unpublish")
        self.assertEqual((down.json()["status"], down.json()["sync_status"]), ("draft", "none"))
        self.drain()
        self.assertEqual(self.faq_documents(), [])

    def test_the_knowledge_base_counts_and_status_follow_published_faqs(self) -> None:
        kb = lambda: self.client.get(f"/api/v1/knowledge-bases/{self.kb_id}", headers=self.admin).json()  # noqa: E731
        self.assertEqual((kb()["faq_count"], kb()["status"]), (0, "draft"))
        faq_id = self.mk().json()["id"]
        self.assertEqual((kb()["faq_count"], kb()["status"]), (1, "draft"))  # a draft does not make it searchable
        self.act(faq_id, "publish")
        self.drain()
        self.assertEqual((kb()["faq_count"], kb()["status"]), (1, "indexed"))

    def test_editing_a_published_faq_replaces_its_engine_document(self) -> None:
        faq = self.client.get(f"/api/v1/knowledge-bases/{self.kb_id}/faqs/{self.published()}", headers=self.admin).json()
        before = self.faq_documents()[0]["id"]
        body = {"question": QUESTION, "answer": "新的答案：15 天内可退。", "similar_questions": ["如何退货"], "row_version": faq["row_version"]}
        edited = self.client.put(f"/api/v1/knowledge-bases/{self.kb_id}/faqs/{faq['id']}", json=body, headers=self.admin)
        self.assertEqual(edited.status_code, 200, edited.text)
        self.drain()
        documents = self.faq_documents()
        self.assertEqual(len(documents), 1)  # the old one was removed, not left behind
        self.assertNotEqual(documents[0]["id"], before)
        self.assertIn("15 天内可退", documents[0]["text"])
        self.assertIn("如何退货", documents[0]["text"])
        self.assertEqual(self.faq(faq["id"])["sync_status"], "synced")
        # Stale edits are rejected (RUL-005).
        stale = self.client.put(f"/api/v1/knowledge-bases/{self.kb_id}/faqs/{faq['id']}", json=body, headers=self.admin)
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "conflict.stale"))

    def test_deleting_a_faq_removes_it_from_the_engine_and_the_list(self) -> None:
        faq_id = self.published()
        self.assertEqual(self.client.delete(f"/api/v1/knowledge-bases/{self.kb_id}/faqs/{faq_id}", headers=self.admin).status_code, 204)
        self.drain()
        self.assertEqual(self.faq_documents(), [])
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{self.kb_id}/faqs/{faq_id}", headers=self.admin).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{self.kb_id}/faqs", headers=self.admin).json()["total"], 0)

    def test_invalid_transitions_and_validation(self) -> None:
        faq_id = self.mk().json()["id"]
        self.assertEqual((self.act(faq_id, "unpublish").status_code, self.act(faq_id, "unpublish").json()["code"]), (409, "faq.invalid_transition"))
        self.assertEqual(self.act(faq_id, "publish").status_code, 200)
        self.assertEqual(self.act(faq_id, "publish").status_code, 409)
        self.assertEqual(self.act(faq_id, "retry").status_code, 409)  # nothing failed
        for overrides in ({"question": "  "}, {"question": "问" * 201}, {"answer": ""}, {"answer": "答" * 2001},
                          {"similar_questions": [f"问法{i}" for i in range(11)]}, {"similar_questions": ["x" * 201]}):
            response = self.mk(**overrides)
            self.assertEqual((response.status_code, response.json()["code"]), (422, "faq.invalid"), overrides)
        deduped = self.mk(question="唯一问题", similar_questions=[" 问法A ", "问法A", "", "唯一问题"]).json()
        self.assertEqual(deduped["similar_questions"], ["问法A"])

    def test_list_search_filter_and_counts(self) -> None:
        self.mk(question="退货", answer="a")
        self.mk(question="发票", answer="b")
        self.published(question="物流", answer="c")
        listing = self.client.get(f"/api/v1/knowledge-bases/{self.kb_id}/faqs", headers=self.admin).json()
        self.assertEqual((listing["total"], listing["counts"]), (3, {"draft": 2, "published": 1, "expired": 0, "all": 3}))
        for query, expected in (("q=退货", ["退货"]), ("status=published", ["物流"]), ("q=b", ["发票"])):
            found = self.client.get(f"/api/v1/knowledge-bases/{self.kb_id}/faqs?{query}", headers=self.admin).json()
            self.assertEqual([i["question"] for i in found["items"]], expected, query)
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{self.kb_id}/faqs?status=bogus", headers=self.admin).status_code, 422)

    def test_copying_a_faq_makes_a_draft(self) -> None:
        faq_id = self.published()
        copy = self.act(faq_id, "copy")
        self.assertEqual(copy.status_code, 201, copy.text)
        self.assertEqual((copy.json()["question"], copy.json()["status"], copy.json()["sync_status"]), (f"{QUESTION}（副本）", "draft", "none"))
        self.drain()
        self.assertEqual(len(self.faq_documents()), 1)

    def test_audit_rows_are_written_for_every_change(self) -> None:
        faq_id = self.mk().json()["id"]
        self.act(faq_id, "publish")
        self.act(faq_id, "unpublish")
        self.client.delete(f"/api/v1/knowledge-bases/{self.kb_id}/faqs/{faq_id}", headers=self.admin)
        with self.session_factory() as db:
            actions = [a.action for a in db.scalars(select(AuditEvent).where(AuditEvent.action.like("faq.%")).order_by(AuditEvent.created_at))]
        self.assertEqual(actions, ["faq.created", "faq.published", "faq.unpublished", "faq.deleted"])


class ExpiryTests(_FaqHarness):
    def test_expired_faqs_leave_the_engine_and_the_job_is_idempotent(self) -> None:
        tomorrow = date.today() + timedelta(days=1)
        faq_id = self.published(expires_at=tomorrow.isoformat())
        self.assertEqual(len(self.faq_documents()), 1)
        later = tomorrow + timedelta(days=1)
        with self.session_factory() as db:
            self.assertEqual(faq_service.expire_due(db, later), 1)
        with self.session_factory() as db:
            self.assertEqual(faq_service.expire_due(db, later), 0)  # nothing left to do
        self.drain()
        expired = self.faq(faq_id)
        self.assertEqual((expired["status"], expired["sync_status"]), ("expired", "none"))
        self.assertEqual(self.faq_documents(), [])
        self.assertEqual(len(self.jobs("knowledge.delete_engine_documents")), 1)  # one removal, not one per run
        # The last day itself still counts; only the day after expires it.
        today_faq = self.published(question="今天到期", expires_at=date.today().isoformat())
        with self.session_factory() as db:
            self.assertEqual(faq_service.expire_due(db, date.today()), 0)
        self.assertEqual(self.faq(today_faq)["status"], "published")

    def test_an_expired_faq_can_be_published_again_after_moving_its_date(self) -> None:
        faq_id = self.published(expires_at=(date.today() + timedelta(days=1)).isoformat())
        with self.session_factory() as db:
            faq_service.expire_due(db, date.today() + timedelta(days=2))
        self.drain()
        stale = self.faq(faq_id)
        self.assertEqual(stale["status"], "expired")
        # Without moving the date the FAQ is still past it.
        with self.session_factory.begin() as db:
            db.get(Faq, UUID(faq_id)).expires_at = date.today() - timedelta(days=1)
        rejected = self.act(faq_id, "publish")
        self.assertEqual((rejected.status_code, rejected.json()["code"]), (422, "faq.expired_date"))
        body = {"question": QUESTION, "answer": ANSWER, "expires_at": (date.today() + timedelta(days=30)).isoformat(),
                "status": "published", "row_version": self.faq(faq_id)["row_version"]}
        revived = self.client.put(f"/api/v1/knowledge-bases/{self.kb_id}/faqs/{faq_id}", json=body, headers=self.admin)
        self.assertEqual((revived.status_code, revived.json()["status"]), (200, "published"), revived.text)
        self.drain()
        self.assertEqual((self.faq(faq_id)["sync_status"], len(self.faq_documents())), ("synced", 1))

    def test_a_past_expiry_date_cannot_be_published(self) -> None:
        past = (date.today() - timedelta(days=1)).isoformat()
        self.assertEqual(self.mk(status="published", expires_at=past).json()["code"], "faq.expired_date")
        draft = self.mk(expires_at=past)
        self.assertEqual(draft.status_code, 201)  # drafts may carry any date
        self.assertEqual(self.act(draft.json()["id"], "publish").status_code, 422)

    def test_the_hourly_job_expires_and_marks_stuck_syncs(self) -> None:
        faq_id = self.published(expires_at=(date.today() + timedelta(days=1)).isoformat())
        with self.session_factory.begin() as db:
            db.get(Faq, UUID(faq_id)).expires_at = date.today() - timedelta(days=1)
            stuck = Faq(tenant_id=self.tenant_id, kb_id=UUID(self.kb_id), question="卡住", answer="a", status="published",
                        sync_status="processing", sync_started_at=datetime.now(UTC) - timedelta(hours=2))
            db.add(stuck)
            db.flush()
            stuck_id = str(stuck.id)
        with self.session_factory() as db:
            faq_service.expire_faqs_job(db, {})
        self.assertEqual(self.faq(faq_id)["status"], "expired")
        failed = self.faq(stuck_id)
        self.assertEqual((failed["sync_status"], "超时" in failed["sync_error"]), ("failed", True))


class SyncFailureTests(_FaqHarness):
    def test_an_engine_outage_during_the_push_is_retried(self) -> None:
        faq_id = self.mk(status="published").json()["id"]
        self.fake.fail_next = 1  # the first engine call fails; the job backs off and tries again
        self.drain()
        self.assertEqual(self.faq(faq_id)["sync_status"], "synced")
        self.assertEqual(len(self.faq_documents()), 1)

    def test_an_indexing_failure_is_visible_and_can_be_retried(self) -> None:
        faq_id = self.mk(question="INDEX-FAIL 这条会失败", status="published").json()["id"]  # the fake fails names containing "fail"
        self.drain()
        failed = self.faq(faq_id)
        self.assertEqual(failed["sync_status"], "failed")
        self.assertTrue(failed["sync_error"])
        with self.session_factory.begin() as db:
            db.get(Faq, UUID(faq_id)).question = "现在正常"
        retried = self.act(faq_id, "retry")
        self.assertEqual((retried.status_code, retried.json()["sync_status"]), (200, "pending"))
        self.drain()
        self.assertEqual(self.faq(faq_id)["sync_status"], "synced")

    def test_publishing_without_a_configured_engine_is_reported_and_retryable(self) -> None:
        self.configure(False)
        faq_id = self.mk(status="published").json()["id"]
        self.drain()
        self.assertEqual(self.faq(faq_id)["sync_status"], "not_configured")
        self.configure(True, self.kb_engine)
        with self.session_factory.begin() as db:  # the knowledge base's datasets get created by the sync job
            from beework import knowledge

            knowledge.request_sync(db, db.get(KnowledgeBase, UUID(self.kb_id)))
        self.drain()
        self.assertEqual(self.act(faq_id, "retry").status_code, 200)
        self.drain()
        self.assertEqual(self.faq(faq_id)["sync_status"], "synced")

    def test_an_edit_during_the_push_does_not_leave_a_stale_document(self) -> None:
        faq_id = self.mk(status="published").json()["id"]  # push queued, not yet run
        current = self.faq(faq_id)
        self.client.put(
            f"/api/v1/knowledge-bases/{self.kb_id}/faqs/{faq_id}", headers=self.admin,
            json={"question": QUESTION, "answer": "更新后的答案", "row_version": current["row_version"]},
        )
        self.drain()
        documents = self.faq_documents()
        self.assertEqual(len(documents), 1)
        self.assertIn("更新后的答案", documents[0]["text"])


class KnowledgeBaseHooksTests(_FaqHarness):
    def test_copying_a_knowledge_base_copies_its_faqs_and_pushes_the_published_ones(self) -> None:
        self.published()
        self.mk(question="草稿问题", answer="草稿答案")
        copy = self.client.post(f"/api/v1/knowledge-bases/{self.kb_id}/copy", headers=self.admin)
        self.assertEqual(copy.status_code, 201, copy.text)
        copy_id = copy.json()["id"]
        self.drain()
        copied = self.client.get(f"/api/v1/knowledge-bases/{copy_id}/faqs", headers=self.admin).json()
        self.assertEqual((copied["counts"]["published"], copied["counts"]["draft"]), (1, 1))
        self.assertEqual({i["sync_status"] for i in copied["items"] if i["status"] == "published"}, {"synced"})
        self.assertEqual(len(self.faq_documents(copy_id)), 1)
        self.assertEqual(len(self.faq_documents()), 1)  # the original is untouched

    def test_a_rebuild_moves_the_published_faqs_into_the_new_dataset(self) -> None:
        faq_id = self.published()
        with self.session_factory() as db:
            old_dataset = db.get(KnowledgeBase, UUID(self.kb_id)).engine_faq_dataset_id
        saved = self.client.put(
            f"/api/v1/knowledge-bases/{self.kb_id}", headers=self.admin,
            json={**self.client.get(f"/api/v1/knowledge-bases/{self.kb_id}", headers=self.admin).json(), "chunking": "by_heading"},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.drain()
        with self.session_factory() as db:
            new_dataset = db.get(KnowledgeBase, UUID(self.kb_id)).engine_faq_dataset_id
        self.assertNotEqual(new_dataset, old_dataset)
        self.assertEqual([d["dataset"] for d in self.fake.documents.values()], [new_dataset])
        self.assertEqual(self.faq(faq_id)["sync_status"], "synced")

    def test_deleting_a_knowledge_base_deletes_its_faqs(self) -> None:
        faq_id = self.published()
        self.assertEqual(self.client.delete(f"/api/v1/knowledge-bases/{self.kb_id}", headers=self.admin).status_code, 204)
        self.drain()
        self.assertIsNotNone(self.row(faq_id).deleted_at)
        self.assertEqual(self.fake.documents, {})


class RetrievalTests(_FaqHarness):
    def mounted(self, llm: ScriptedChat):
        assistant = self.assistant([self.kb_id]).json()
        return self.ask(assistant["id"], message=ASK, llm=llm), assistant

    def test_only_published_faqs_are_found_and_they_answer_as_standard_answers(self) -> None:
        draft = self.mk(question="怎么开发票", answer="订单页申请发票。").json()
        self.drain()
        no_hit = ScriptedChat(chunks=["不知道"])
        self.assertEqual(self.assistant([self.kb_id]).status_code, 422)  # nothing published: not mountable yet
        self.published()
        started, _ = self.mounted(no_hit)
        system = no_hit.seen_messages[0][0].content
        self.assertIn("标准答案", system)
        self.assertIn(ANSWER, system)
        self.assertNotIn("订单页申请发票", system)  # the draft is invisible to retrieval
        run = self.run_row(started["run_id"])
        citation = run.output["retrieval"]["citations"][0]
        self.assertEqual((run.retrieval_mode, citation["kind"], citation["kb_name"]), (self.kb_engine, "faq", "客服知识库"))
        self.assertEqual(self.faq(draft["id"])["hits_30d"], 0)

    def test_hits_are_counted_per_answered_run_in_the_last_30_days(self) -> None:
        faq_id = self.published()
        llm = ScriptedChat(chunks=["ok"])
        assistant = self.assistant([self.kb_id]).json()
        self.ask(assistant["id"], message=ASK, llm=llm)
        self.ask(assistant["id"], message=ASK, llm=ScriptedChat(chunks=["ok"]))
        self.assertEqual(self.faq(faq_id)["hits_30d"], 2)
        listing = self.client.get(f"/api/v1/knowledge-bases/{self.kb_id}/faqs", headers=self.admin).json()
        self.assertEqual(listing["items"][0]["hits_30d"], 2)
        # Hits older than 30 days no longer count, and very old ones are purged.
        with self.session_factory.begin() as db:
            for hit in db.scalars(select(FaqHit)):
                hit.created_at = datetime.now(UTC) - timedelta(days=45)
        self.assertEqual(self.faq(faq_id)["hits_30d"], 0)
        with self.session_factory() as db:
            self.assertEqual(faq_service.purge_old_hits(db), 0)
        with self.session_factory.begin() as db:
            for hit in db.scalars(select(FaqHit)):
                hit.created_at = datetime.now(UTC) - timedelta(days=120)
        with self.session_factory() as db:
            self.assertEqual(faq_service.purge_old_hits(db), 2)

    def test_a_faq_match_wins_over_documents_of_the_same_knowledge_base(self) -> None:
        kb_id = self.indexed_kb("混合库", "# 退货政策\n\n购买后 7 天内可申请无理由退货，商品需保持完好。\n".encode() * 3)
        self.client.post(f"/api/v1/knowledge-bases/{kb_id}/faqs", headers=self.admin,
                         json={"question": "购买后 7 天内可申请无理由退货吗", "answer": "可以，FAQ 标准答案。", "status": "published"})
        self.drain()
        assistant = self.assistant([kb_id]).json()
        llm = ScriptedChat(chunks=["ok"])
        started = self.ask(assistant["id"], message="购买后 7 天内可申请无理由退货吗", llm=llm)
        kinds = {c["kind"] for c in self.run_row(started["run_id"]).output["retrieval"]["citations"]}
        self.assertEqual(kinds, {"faq"})
        self.assertIn("FAQ 标准答案", llm.seen_messages[0][0].content)

    def test_unpublishing_makes_the_faq_unfindable(self) -> None:
        faq_id = self.published()
        assistant = self.assistant([self.kb_id]).json()
        self.act(faq_id, "unpublish")
        self.drain()
        llm = ScriptedChat(chunks=["ok"])
        started = self.ask(assistant["id"], message=ASK, llm=llm)
        self.assertNotIn(ANSWER, llm.seen_messages[0][0].content)
        self.assertEqual(self.run_row(started["run_id"]).retrieval_mode, "none")  # nothing searchable is left


class ImportTests(_FaqHarness):
    CSV = "问题,答案,相似问法\n怎么退货,7 天内可退,如何退货;退款流程\n怎么开发票,订单页申请,\n".encode()

    def csv(self, data: bytes, headers=None, **params):
        return self.client.post(
            f"/api/v1/knowledge-bases/{self.kb_id}/faqs/import", params=params, content=data,
            headers={**(headers or self.admin), "Content-Type": "text/csv"},
        )

    def test_preview_counts_rows_without_creating_anything(self) -> None:
        preview = self.csv(self.CSV, dry_run="true")
        self.assertEqual(preview.status_code, 200, preview.text)
        body = preview.json()
        self.assertEqual((body["dry_run"], body["recognized"], body["created"], body["errors"]), (True, 2, 0, []))
        self.assertEqual(body["preview"][0]["similar_questions"], ["如何退货", "退款流程"])
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{self.kb_id}/faqs", headers=self.admin).json()["total"], 0)

    def test_import_creates_drafts_and_can_publish(self) -> None:
        done = self.csv(self.CSV).json()
        self.assertEqual((done["recognized"], done["created"]), (2, 2))
        listing = self.client.get(f"/api/v1/knowledge-bases/{self.kb_id}/faqs", headers=self.admin).json()
        self.assertEqual(listing["counts"], {"draft": 2, "published": 0, "expired": 0, "all": 2})
        self.drain()
        self.assertEqual(self.faq_documents(), [])
        more = self.csv("问题,答案\n再来一条,答案".encode(), publish="true").json()
        self.assertEqual(more["created"], 1)
        self.drain()
        self.assertEqual(len(self.faq_documents()), 1)

    def test_every_bad_row_is_reported_with_its_line_and_the_good_ones_still_import(self) -> None:
        data = ("问题,答案,相似问法\n"
                "好问题,好答案,\n"
                "只有一列\n"
                ",没有问题,\n"
                "没有答案,\n"
                f"{'长' * 201},答案,\n"
                "多列,答案,相似,多余\n"
                "好问题二,好答案二,甲;乙\n\n").encode()
        body = self.csv(data).json()
        self.assertEqual((body["recognized"], body["created"]), (2, 2))
        self.assertEqual([e["line"] for e in body["errors"]], [3, 4, 5, 6, 7])
        self.assertIn("两列", body["errors"][0]["message"])
        self.assertIn("问题", body["errors"][1]["message"])
        self.assertIn("答案", body["errors"][2]["message"])
        self.assertIn("200", body["errors"][3]["message"])
        self.assertIn("三列", body["errors"][4]["message"])

    def test_formats_encodings_and_limits(self) -> None:
        bom = b"\xef\xbb\xbf" + "问题,答案\nBOM 问题,答案".encode()
        self.assertEqual(self.csv(bom, dry_run="true").json()["recognized"], 1)
        gbk = "问题,答案\nGBK 问题,答案".encode("gb18030")
        self.assertEqual(self.csv(gbk, dry_run="true").json()["recognized"], 1)
        quoted = '"带逗号, 的问题","答案，也有逗号","甲；乙"\n'.encode()
        row = self.csv(quoted, dry_run="true").json()["preview"][0]
        self.assertEqual((row["question"], row["similar_questions"]), ("带逗号, 的问题", ["甲", "乙"]))  # no header is needed
        self.assertEqual(self.csv(b"   \n").status_code, 422)
        self.assertEqual(self.csv(b"\xff\xfe\x00\x80\x81" * 50).status_code, 422)
        many = "\n".join(f"问题{i},答案{i}" for i in range(501)).encode()
        self.assertEqual(self.csv(many).json()["code"], "faq.too_many_rows")
        self.assertEqual(self.csv(b"a,b\n" + b"x" * 1_100_000).status_code, 413)


class PermissionTests(_FaqHarness):
    def test_operators_edit_drafts_but_cannot_publish_or_delete(self) -> None:
        operator = self.login("operator@example.com")
        created = self.mk(operator)
        self.assertEqual(created.status_code, 201, created.text)
        faq_id = created.json()["id"]
        self.assertEqual(self.mk(operator, question="另一条", status="published").status_code, 403)
        self.assertEqual(self.act(faq_id, "publish", operator).status_code, 403)
        self.assertEqual(self.client.delete(f"/api/v1/knowledge-bases/{self.kb_id}/faqs/{faq_id}", headers=operator).status_code, 403)
        body = {"question": QUESTION, "answer": "运营改过", "status": "published", "row_version": created.json()["row_version"]}
        self.assertEqual(self.client.put(f"/api/v1/knowledge-bases/{self.kb_id}/faqs/{faq_id}", json=body, headers=operator).status_code, 403)
        edited = self.client.put(f"/api/v1/knowledge-bases/{self.kb_id}/faqs/{faq_id}", json={**body, "status": "draft"}, headers=operator)
        self.assertEqual((edited.status_code, edited.json()["answer"]), (200, "运营改过"))
        self.assertEqual(self.csv_import(operator, publish="true").status_code, 403)
        self.assertEqual(self.csv_import(operator).status_code, 200)

    def csv_import(self, headers, **params):
        return self.client.post(
            f"/api/v1/knowledge-bases/{self.kb_id}/faqs/import", params=params, content="问题,答案\n运营导入,答案".encode(),
            headers={**headers, "Content-Type": "text/csv"},
        )

    def test_viewers_read_only_and_unauthenticated_requests_are_rejected(self) -> None:
        faq_id = self.mk().json()["id"]
        viewer = self.login("viewer@example.com")
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{self.kb_id}/faqs", headers=viewer).status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{self.kb_id}/faqs/{faq_id}", headers=viewer).status_code, 200)
        self.assertEqual(self.mk(viewer).status_code, 403)
        self.assertEqual(self.act(faq_id, "publish", viewer).status_code, 403)
        self.assertEqual(self.client.delete(f"/api/v1/knowledge-bases/{self.kb_id}/faqs/{faq_id}", headers=viewer).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{self.kb_id}/faqs").status_code, 401)

    def test_other_tenants_and_out_of_scope_organizations_get_404_or_403(self) -> None:
        faq_id = self.mk().json()["id"]
        # An FAQ id of another knowledge base is not addressable through this one.
        created = self.client.post("/api/v1/knowledge-bases", headers=self.admin, json={"name": "另一个库"})
        self.assertEqual(created.status_code, 201, created.text)
        other_kb = created.json()["id"]
        base = f"/api/v1/knowledge-bases/{self.kb_id}/faqs"
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{other_kb}/faqs/{faq_id}", headers=self.admin).status_code, 404)
        other = self.login("other@example.com", self.other_id)
        for method, path in (("get", base), ("get", f"{base}/{faq_id}"), ("delete", f"{base}/{faq_id}"), ("post", f"{base}/{faq_id}/publish")):
            self.assertEqual(getattr(self.client, method)(path, headers=other).status_code, 404, path)
        self.assertEqual(self.mk(other).status_code, 404)


if __name__ == "__main__":
    unittest.main()
