import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from sqlalchemy import select

from beework import documents, file_store, knowledge
from beework.config import settings
from beework.entitlements import get_subscription, quota_usage
from beework.errors import ApiError
from beework.integrations.knowledge_engine import KnowledgeEngineError
from beework.models.jobs import Job
from beework.models.knowledge import KnowledgeBase, KnowledgeDocument
from test_knowledge import _Harness

MARKDOWN = ("# 退货政策\n\n" + "购买后 7 天内可申请无理由退货，商品需保持完好。\n" * 30).encode()
PDF = b"%PDF-1.4\n" + b"content " * 200 + b"\n%%EOF"


def make_docx() -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<w:document>hello</w:document>")
    return buffer.getvalue()


class _DocumentHarness(_Harness):
    kb_engine = "ragflow"

    def setUp(self) -> None:
        super().setUp()
        self.configure(True, self.kb_engine)
        self.storage = tempfile.mkdtemp()
        self._storage_setting = settings.file_storage_dir
        settings.file_storage_dir = self.storage
        self.fake.PARSE_SECONDS = 0  # parsing finishes as soon as it is asked
        self._max = documents.MAX_FILE_BYTES

    def tearDown(self) -> None:
        documents.MAX_FILE_BYTES = self._max
        settings.file_storage_dir = self._storage_setting
        shutil.rmtree(self.storage, ignore_errors=True)
        self.fake.PARSE_SECONDS = 1
        super().tearDown()

    def engine_documents(self) -> list[dict]:
        return [d for d in self.fake.documents.values() if d["engine"] == self.kb_engine]

    def drain(self, rounds: int = 12) -> None:
        """Run the Worker until the queue is empty, treating every delayed (backed-off) job as due."""
        for _ in range(rounds):
            with self.session_factory.begin() as db:
                for job in db.scalars(select(Job).where(Job.status == "queued")):
                    job.run_after = datetime.now(UTC) - timedelta(seconds=1)
            if self.worker() == 0:
                return

    def kb(self, headers, **overrides) -> str:
        body = self.create(headers, **overrides).json()
        self.worker()
        return body["id"]

    def upload(self, headers, kb_id, filename, data, **params):
        return self.client.post(
            f"/api/v1/knowledge-bases/{kb_id}/documents", params={"filename": filename, **params}, headers=headers,
            content=data,
        )

    def doc(self, document_id: str) -> KnowledgeDocument:
        with self.session_factory() as db:
            return db.get(KnowledgeDocument, UUID(document_id))


class DocumentTests(_DocumentHarness):
    def test_a_file_goes_from_processing_to_indexed_and_the_knowledge_base_follows(self) -> None:
        headers = self.login("admin@example.com")
        kb_id = self.kb(headers)
        uploaded = self.upload(headers, kb_id, "退货政策.md", MARKDOWN)
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        body = uploaded.json()
        self.assertEqual((body["status"], body["format"], body["name"], body["owner_name"]), ("processing", "markdown", "退货政策.md", "Admin"))
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=headers).json()["status"], "processing")
        self.drain()
        done = self.client.get(f"/api/v1/knowledge-bases/{kb_id}/documents/{body['id']}", headers=headers).json()
        self.assertEqual(done["status"], "indexed")
        self.assertGreater(done["chunks"], 0)
        self.assertEqual(len(self.engine_documents()), 1)
        knowledge_base = self.client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=headers).json()
        self.assertEqual((knowledge_base["status"], knowledge_base["docs_count"]), ("indexed", 1))
        listed = self.client.get(f"/api/v1/knowledge-bases/{kb_id}/documents", headers=headers).json()
        self.assertEqual((listed["total"], listed["counts"]), (1, {"processing": 0, "indexed": 1, "failed": 0, "all": 1}))
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{kb_id}/documents?q=退货", headers=headers).json()["total"], 1)
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{kb_id}/documents?q=nope", headers=headers).json()["total"], 0)
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{kb_id}/documents?status=failed", headers=headers).json()["total"], 0)

    def test_every_supported_format_is_accepted(self) -> None:
        headers = self.login("admin@example.com")
        kb_id = self.kb(headers)
        for name, data, expected in (
            ("a.pdf", PDF, "pdf"), ("b.docx", make_docx(), "docx"), ("c.html", b"<html><body>hi</body></html>", "html"),
            ("d.htm", b"<p>hi</p>", "html"), ("e.markdown", b"# hi", "markdown"), ("f.txt", "中文内容".encode("gb18030"), "txt"),
        ):
            response = self.upload(headers, kb_id, name, data)
            self.assertEqual((response.status_code, response.json().get("format")), (201, expected), (name, response.text))

    def test_uploads_are_validated_by_extension_content_size_and_name(self) -> None:
        headers = self.login("admin@example.com")
        kb_id = self.kb(headers)
        cases = [
            ("run.exe", b"MZ\x90\x00", 422, "knowledge.unsupported_file"),
            ("noextension", b"hello", 422, "knowledge.unsupported_file"),
            ("fake.pdf", b"<html>this is not a pdf</html>", 422, "knowledge.content_mismatch"),
            ("fake.docx", b"plain text, not a zip", 422, "knowledge.content_mismatch"),
            ("empty.docx", make_docx()[:20], 422, "knowledge.content_mismatch"),
            ("binary.txt", b"abc\x00\x01\x02def", 422, "knowledge.content_mismatch"),
            ("empty.txt", b"", 422, "knowledge.empty_file"),
            ("...", b"hello", 422, "knowledge.invalid_filename"),
        ]
        for name, data, status_code, code in cases:
            response = self.upload(headers, kb_id, name, data)
            self.assertEqual((response.status_code, response.json()["code"]), (status_code, code), name)
        documents.MAX_FILE_BYTES = 1000
        too_big = self.upload(headers, kb_id, "big.txt", b"x" * 1001)
        self.assertEqual((too_big.status_code, too_big.json()["code"]), (413, "knowledge.file_too_large"))
        self.assertEqual(self.upload(headers, kb_id, "ok.txt", b"x" * 1000).status_code, 201)
        # Path components in the name never reach the file system or the stored name.
        sneaky = self.upload(headers, kb_id, "../../etc/passwd.txt", b"hello")
        self.assertEqual((sneaky.status_code, sneaky.json()["name"]), (201, "passwd.txt"))
        self.assertEqual(self.upload(headers, kb_id, "C:\\\\Windows\\\\evil.txt", b"hello").json()["name"], "evil.txt")
        with self.session_factory() as db:
            keys = [d.file_key for d in db.scalars(select(KnowledgeDocument))]
        self.assertTrue(all(key.count("/") == 2 and ".." not in key for key in keys))

    def test_same_name_asks_then_overwrites_or_keeps_both(self) -> None:
        headers = self.login("admin@example.com")
        kb_id = self.kb(headers)
        first = self.upload(headers, kb_id, "faq.md", MARKDOWN).json()
        self.drain()
        ask = self.upload(headers, kb_id, "FAQ.md", MARKDOWN)
        self.assertEqual((ask.status_code, ask.json()["code"]), (409, "knowledge.duplicate_name"))
        both = self.upload(headers, kb_id, "faq.md", MARKDOWN, on_conflict="keep_both")
        self.assertEqual(both.json()["name"], "faq (2).md")
        replaced = self.upload(headers, kb_id, "faq.md", b"# new\ncontent", on_conflict="overwrite").json()
        self.drain()
        self.assertNotEqual(replaced["id"], first["id"])
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{kb_id}/documents/{first['id']}", headers=headers).status_code, 404)
        names = sorted(item["name"] for item in self.client.get(f"/api/v1/knowledge-bases/{kb_id}/documents", headers=headers).json()["items"])
        self.assertEqual(names, ["faq (2).md", "faq.md"])
        self.assertEqual(len(self.engine_documents()), 2)  # the replaced one was removed from the engine too

    def test_access_rules(self) -> None:
        admin = self.login("admin@example.com")
        kb_id = self.kb(admin)
        faq_id = self.kb(admin, name="FAQ only", kb_type="faq")
        document = self.upload(admin, kb_id, "a.md", MARKDOWN).json()
        refused = self.upload(admin, faq_id, "a.md", MARKDOWN)
        self.assertEqual((refused.status_code, refused.json()["code"]), (422, "knowledge.faq_no_documents"))
        without_csrf = {k: v for k, v in admin.items() if k != "X-CSRF-Token"}
        self.assertEqual(self.upload(without_csrf, kb_id, "b.md", MARKDOWN).status_code, 403)
        self.client.post("/api/v1/auth/logout", headers=admin)
        viewer = self.login("viewer@example.com")
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{kb_id}/documents", headers=viewer).status_code, 200)
        self.assertEqual(self.upload(viewer, kb_id, "c.md", MARKDOWN).status_code, 403)
        self.assertEqual(self.client.delete(f"/api/v1/knowledge-bases/{kb_id}/documents/{document['id']}", headers=viewer).status_code, 403)
        self.assertEqual(self.client.post(f"/api/v1/knowledge-bases/{kb_id}/documents/{document['id']}/reindex", headers=viewer).status_code, 403)
        self.client.post("/api/v1/auth/logout", headers=viewer)
        other = self.login("other@example.com", self.other_id)
        for path in ("", f"/{document['id']}", f"/{document['id']}/download", f"/{document['id']}/chunks"):
            self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{kb_id}/documents{path}", headers=other).status_code, 404, path)
        self.assertEqual(self.upload(other, kb_id, "x.md", MARKDOWN).status_code, 404)
        self.client.post("/api/v1/auth/logout", headers=other)
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{kb_id}/documents").status_code, 401)

    def test_a_document_of_another_knowledge_base_is_not_reachable_through_this_one(self) -> None:
        headers = self.login("admin@example.com")
        first, second = self.kb(headers), self.kb(headers, name="Second")
        document = self.upload(headers, first, "a.md", MARKDOWN).json()
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{second}/documents/{document['id']}", headers=headers).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/v1/knowledge-bases/{second}/documents/{document['id']}", headers=headers).status_code, 404)

    def test_engine_failure_shows_its_reason_and_reindex_retries(self) -> None:
        headers = self.login("admin@example.com")
        kb_id = self.kb(headers)
        document = self.upload(headers, kb_id, "will-fail.txt", MARKDOWN).json()
        self.drain()
        failed = self.client.get(f"/api/v1/knowledge-bases/{kb_id}/documents/{document['id']}", headers=headers).json()
        self.assertEqual(failed["status"], "failed")
        self.assertIn("Unsupported content", failed["error_message"])
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{kb_id}/documents?status=failed", headers=headers).json()["counts"]["failed"], 1)
        again = self.client.post(f"/api/v1/knowledge-bases/{kb_id}/documents/{document['id']}/reindex", headers=headers)
        self.assertEqual((again.status_code, again.json()["status"], again.json()["error_message"]), (200, "processing", None))
        self.assertEqual(self.client.post(f"/api/v1/knowledge-bases/{kb_id}/documents/{document['id']}/reindex", headers=headers).status_code, 409)
        self.drain()
        self.assertEqual(self.doc(document["id"]).status, "failed")  # still the same bad content
        self.assertEqual(len(self.engine_documents()), 1)  # the failed engine copy was replaced, not piled up

    def test_polling_is_idempotent_and_a_stale_poll_is_ignored_after_reindex(self) -> None:
        headers = self.login("admin@example.com")
        kb_id = self.kb(headers)
        document = self.upload(headers, kb_id, "a.md", MARKDOWN).json()
        self.worker()  # uploads and queues the first poll
        stored = self.doc(document["id"])
        payload = {"document_id": document["id"], "engine_document_id": stored.engine_document_id, "attempt": 0}
        with self.session_factory() as db:
            documents.poll_document_job(db, payload)
            documents.poll_document_job(db, payload)  # a second delivery of the same poll
        done = self.doc(document["id"])
        self.assertEqual((done.status, done.chunks > 0), ("indexed", True))
        chunks = done.chunks
        reindexed = self.client.post(f"/api/v1/knowledge-bases/{kb_id}/documents/{document['id']}/reindex", headers=headers)
        self.assertEqual(reindexed.status_code, 200)
        with self.session_factory() as db:
            documents.poll_document_job(db, payload)  # the old engine document id: dropped
        self.assertEqual(self.doc(document["id"]).status, "processing")
        self.drain()
        self.assertEqual((self.doc(document["id"]).status, self.doc(document["id"]).chunks), ("indexed", chunks))

    def test_a_slow_document_keeps_polling_with_backoff_and_a_stuck_one_is_swept(self) -> None:
        headers = self.login("admin@example.com")
        kb_id = self.kb(headers)
        self.fake.PARSE_SECONDS = 3600  # never finishes
        document = self.upload(headers, kb_id, "slow.md", MARKDOWN).json()
        self.drain(rounds=4)
        self.assertEqual(self.doc(document["id"]).status, "processing")
        with self.session_factory() as db:
            polls = list(db.scalars(select(Job).where(Job.kind == "knowledge.poll_document")))
            self.assertGreaterEqual(len(polls), 3)
            self.assertEqual(len({job.idempotency_key for job in polls}), len(polls))  # one per attempt, none duplicated
            delays = [(job.run_after - job.created_at).total_seconds() for job in polls if job.status == "queued"]
            self.assertTrue(delays and all(delay >= 4 for delay in delays))
            stored = db.get(KnowledgeDocument, UUID(document["id"]))
            stored.processing_started_at = datetime.now(UTC) - documents.PROCESSING_TIMEOUT - timedelta(minutes=1)
            db.commit()
            documents.sweep_documents_job(db, {})
        swept = self.doc(document["id"])
        self.assertEqual(swept.status, "failed")
        self.assertIn("超时", swept.error_message)

    def test_unconfigured_engine_fails_the_document_with_a_hint(self) -> None:
        headers = self.login("admin@example.com")
        kb_id = self.kb(headers)
        document = self.upload(headers, kb_id, "a.md", MARKDOWN).json()
        self.configure(False, self.kb_engine)
        self.drain()
        failed = self.doc(document["id"])
        self.assertEqual(failed.status, "failed")
        self.assertIn("知识引擎未配置", failed.error_message)
        self.configure(True, self.kb_engine)
        self.client.post(f"/api/v1/knowledge-bases/{kb_id}/documents/{document['id']}/reindex", headers=headers)
        self.drain()
        self.assertEqual(self.doc(document["id"]).status, "indexed")

    def test_the_upload_waits_for_the_dataset_to_exist(self) -> None:
        headers = self.login("admin@example.com")
        kb_id = self.kb(headers)
        document = self.upload(headers, kb_id, "a.md", MARKDOWN).json()
        with self.session_factory() as db:
            db.get(KnowledgeBase, UUID(kb_id)).engine_dataset_id = None
            db.commit()
            with self.assertRaises(KnowledgeEngineError):
                documents.upload_document_job(db, {"document_id": document["id"]})
        self.assertEqual(self.doc(document["id"]).status, "processing")  # retried later, not failed

    # -- delete, download, preview ----------------------------------------------------------------------------

    def test_delete_removes_the_file_and_the_engine_copy_but_keeps_the_row(self) -> None:
        headers = self.login("admin@example.com")
        kb_id = self.kb(headers)
        document = self.upload(headers, kb_id, "a.md", MARKDOWN).json()
        self.drain()
        key = self.doc(document["id"]).file_key
        self.assertTrue(file_store.exists(key))
        deleted = self.client.delete(f"/api/v1/knowledge-bases/{kb_id}/documents/{document['id']}", headers=headers)
        self.assertEqual(deleted.status_code, 204)
        self.assertFalse(file_store.exists(key))
        self.drain()
        self.assertEqual(self.engine_documents(), [])
        self.assertIsNotNone(self.doc(document["id"]).deleted_at)
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{kb_id}/documents/{document['id']}", headers=headers).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=headers).json()["docs_count"], 0)

    def test_deleting_while_processing_stops_the_pipeline(self) -> None:
        headers = self.login("admin@example.com")
        kb_id = self.kb(headers)
        document = self.upload(headers, kb_id, "a.md", MARKDOWN).json()
        self.client.delete(f"/api/v1/knowledge-bases/{kb_id}/documents/{document['id']}", headers=headers)
        self.drain()
        self.assertEqual(self.engine_documents(), [])  # the queued upload found it deleted and did nothing

    def test_download_returns_the_original_as_an_attachment(self) -> None:
        headers = self.login("admin@example.com")
        kb_id = self.kb(headers)
        document = self.upload(headers, kb_id, "页面.html", b"<script>alert(1)</script>").json()
        response = self.client.get(f"/api/v1/knowledge-bases/{kb_id}/documents/{document['id']}/download", headers=headers)
        self.assertEqual(response.content, b"<script>alert(1)</script>")
        self.assertEqual(response.headers["content-type"], "application/octet-stream")
        self.assertTrue(response.headers["content-disposition"].startswith("attachment; filename*=UTF-8''"))
        self.assertEqual((response.headers["x-content-type-options"], response.headers["content-security-policy"]), ("nosniff", "sandbox"))

    def test_chunk_preview_comes_from_the_engine_once_indexed(self) -> None:
        headers = self.login("admin@example.com")
        kb_id = self.kb(headers)
        document = self.upload(headers, kb_id, "a.md", MARKDOWN).json()
        url = f"/api/v1/knowledge-bases/{kb_id}/documents/{document['id']}/chunks"
        early = self.client.get(url, headers=headers)
        self.assertEqual((early.status_code, early.json()["code"]), (409, "knowledge.not_indexed"))
        self.drain()
        page = self.client.get(url, headers=headers, params={"size": 2}).json()
        self.assertEqual(len(page["items"]), 2)
        self.assertGreater(page["total"], 2)
        self.assertTrue(page["items"][0].startswith("# 退货政策"))
        second = self.client.get(url, headers=headers, params={"size": 2, "page": 2}).json()
        self.assertNotEqual(second["items"], page["items"])

    # -- URL documents ------------------------------------------------------------------------------------------

    def test_urls_must_be_public_https_and_one_bad_address_rejects_the_batch(self) -> None:
        headers = self.login("admin@example.com")
        kb_id = self.kb(headers)
        url = f"/api/v1/knowledge-bases/{kb_id}/documents/urls"
        for bad in ("http://example.com/a", "https://127.0.0.1/a", "https://10.0.0.5/a", "https://169.254.169.254/latest", "https://localhost/a", "ftp://example.com/a", "javascript:alert(1)"):
            response = self.client.post(url, headers=headers, json={"urls": ["https://example.com/ok", bad]})
            self.assertEqual((response.status_code, response.json()["code"]), (400, "security.invalid_url"), bad)
        with self.session_factory() as db:
            self.assertEqual(db.scalar(select(KnowledgeDocument.id)), None)  # nothing was created for the rejected batches
        self.assertEqual(self.client.post(url, headers=headers, json={"urls": []}).status_code, 422)
        self.assertEqual(self.client.post(url, headers=headers, json={"urls": [f"https://example.com/{i}" for i in range(21)]}).status_code, 422)

    def test_a_url_is_fetched_stored_and_indexed(self) -> None:
        headers = self.login("admin@example.com")
        kb_id = self.kb(headers)
        original = documents.fetch_url
        documents.fetch_url = lambda _url: (b"<html><body>" + "产品说明 ".encode() * 80 + b"</body></html>", "html")
        try:
            created = self.client.post(f"/api/v1/knowledge-bases/{kb_id}/documents/urls", headers=headers, json={"urls": ["https://example.com/guide", "https://example.com/guide"]})
            self.assertEqual(created.status_code, 201, created.text)
            self.assertEqual(len(created.json()), 1)  # duplicates in one request collapse
            item = created.json()[0]
            self.assertEqual((item["format"], item["source_url"], item["name"], item["can_download"]), ("url", "https://example.com/guide", "example.com/guide", False))
            self.drain()
        finally:
            documents.fetch_url = original
        done = self.doc(item["id"])
        self.assertEqual((done.status, done.chunks > 0, done.size_bytes > 0), ("indexed", True, True))
        link = self.client.get(f"/api/v1/knowledge-bases/{kb_id}/documents/{item['id']}/download", headers=headers)
        self.assertEqual((link.status_code, link.json()["url"]), (409, "https://example.com/guide"))  # the UI opens the link

    def test_a_failed_fetch_is_reported_and_retry_fetches_again(self) -> None:
        headers = self.login("admin@example.com")
        kb_id = self.kb(headers)
        original = documents.fetch_url

        def broken(_url):
            raise ApiError(502, "knowledge.fetch_failed", "网页返回 404")

        documents.fetch_url = broken
        try:
            item = self.client.post(f"/api/v1/knowledge-bases/{kb_id}/documents/urls", headers=headers, json={"urls": ["https://example.com/gone"]}).json()[0]
            self.drain()
            failed = self.doc(item["id"])
            self.assertEqual(failed.status, "failed")
            self.assertIn("网页返回 404", failed.error_message)
            documents.fetch_url = lambda _url: (b"<p>now it exists</p>", "html")
            self.client.post(f"/api/v1/knowledge-bases/{kb_id}/documents/{item['id']}/reindex", headers=headers)
            self.drain()
        finally:
            documents.fetch_url = original
        self.assertEqual(self.doc(item["id"]).status, "indexed")

    def test_fetching_follows_redirects_only_to_public_https_and_caps_the_size(self) -> None:
        from beework.api import conventions

        conventions.default_public_url_resolver = lambda _url: ["93.184.216.34"]
        pages = {
            "https://example.com/redirect-internal": httpx.Response(302, headers={"location": "https://10.0.0.5/secret"}),
            "https://example.com/redirect-http": httpx.Response(302, headers={"location": "http://example.com/x"}),
            "https://example.com/loop": httpx.Response(302, headers={"location": "https://example.com/loop"}),
            "https://example.com/hop": httpx.Response(302, headers={"location": "/page"}),
            "https://example.com/page": httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=b"<p>hello</p>"),
            "https://example.com/pdf": httpx.Response(200, headers={"content-type": "application/pdf"}, content=PDF),
            "https://example.com/image": httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG"),
            "https://example.com/huge": httpx.Response(200, headers={"content-type": "text/html"}, content=b"x" * 4000),
            "https://example.com/missing": httpx.Response(404),
        }
        real_client = httpx.Client

        def mocked(**kwargs):
            return real_client(transport=httpx.MockTransport(lambda request: pages[str(request.url)]), **kwargs)

        documents.httpx.Client = mocked
        documents.MAX_URL_BYTES = 3000
        try:
            for url, message in (
                ("https://example.com/redirect-internal", "内网"), ("https://example.com/redirect-http", "https"),
                ("https://example.com/loop", "重定向"), ("https://example.com/image", "不支持"),
                ("https://example.com/huge", "20 MB"), ("https://example.com/missing", "404"),
            ):
                with self.assertRaises(ApiError, msg=url) as caught:
                    documents.fetch_url(url)
                self.assertIn(message, caught.exception.detail, url)
            self.assertEqual(documents.fetch_url("https://example.com/hop"), (b"<p>hello</p>", "html"))
            self.assertEqual(documents.fetch_url("https://example.com/pdf"), (PDF, "pdf"))
        finally:
            documents.httpx.Client = real_client
            documents.MAX_URL_BYTES = 20 * 1024 * 1024

    # -- quota, rebuild, copy, delete ---------------------------------------------------------------------------

    def test_storage_quota_counts_bytes_and_blocks_hard_limits(self) -> None:
        headers = self.login("admin@example.com")
        kb_id = self.kb(headers)
        self.assertEqual(self.upload(headers, kb_id, "a.md", MARKDOWN).status_code, 201)
        with self.session_factory.begin() as db:
            subscription = get_subscription(db, self.tenant_id)
            self.assertEqual(quota_usage(db, self.tenant_id, "storage_gb"), 1)  # rounded up to whole GB
            subscription.quotas = {**subscription.quotas, "storage_gb": 0}
        blocked = self.upload(headers, kb_id, "b.md", MARKDOWN)
        self.assertEqual((blocked.status_code, blocked.json()["code"]), (409, "plan.quota_exceeded"))
        with self.session_factory() as db:
            self.assertEqual(db.scalar(select(KnowledgeDocument.id).where(KnowledgeDocument.name == "b.md")), None)
        self.assertFalse(any(self.storage_files("b.md")))

    def storage_files(self, _name: str):
        import os

        return [files for _, _, files in os.walk(self.storage) if any(f.endswith(".part") for f in files)]

    def test_changing_the_chunking_re_indexes_every_document_into_the_new_dataset(self) -> None:
        headers = self.login("admin@example.com")
        kb_id = self.kb(headers)
        document = self.upload(headers, kb_id, "a.md", MARKDOWN).json()
        self.drain()
        before = self.doc(document["id"])
        body = self.client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=headers).json()
        url = f"/api/v1/knowledge-bases/{kb_id}"
        refused = self.client.put(url, headers=headers, json=self.settings_payload(body, chunking="fixed_512"))
        self.assertEqual(refused.json()["code"], "knowledge.reindex_required")
        saved = self.client.put(url, headers=headers, json=self.settings_payload(body, chunking="fixed_512", confirm_reindex=True))
        self.assertEqual(saved.status_code, 200, saved.text)
        self.drain()
        after = self.doc(document["id"])
        self.assertEqual(after.status, "indexed")
        self.assertNotEqual(after.engine_dataset_id, before.engine_dataset_id)
        self.assertEqual(after.engine_dataset_id, self.row(kb_id).engine_dataset_id)
        self.assertEqual(len(self.engine_documents()), 1)  # the old dataset, with its copy, is gone
        self.assertEqual(self.client.get(url, headers=headers).json()["status"], "indexed")

    def test_copying_a_knowledge_base_copies_its_documents_and_files(self) -> None:
        headers = self.login("admin@example.com")
        kb_id = self.kb(headers)
        original = self.upload(headers, kb_id, "a.md", MARKDOWN).json()
        self.drain()
        copy = self.client.post(f"/api/v1/knowledge-bases/{kb_id}/copy", headers=headers).json()
        self.assertEqual((copy["status"], copy["docs_count"]), ("processing", 1))
        self.drain()
        items = self.client.get(f"/api/v1/knowledge-bases/{copy['id']}/documents", headers=headers).json()["items"]
        self.assertEqual([(i["name"], i["status"]) for i in items], [("a.md", "indexed")])
        self.assertNotEqual(items[0]["id"], original["id"])
        self.assertNotEqual(self.doc(items[0]["id"]).file_key, self.doc(original["id"]).file_key)
        self.assertEqual(len(self.engine_documents()), 2)
        self.client.delete(f"/api/v1/knowledge-bases/{kb_id}/documents/{original['id']}", headers=headers)
        self.assertTrue(file_store.exists(self.doc(items[0]["id"]).file_key))  # the copy has its own file

    def test_deleting_a_knowledge_base_removes_its_files(self) -> None:
        headers = self.login("admin@example.com")
        kb_id = self.kb(headers)
        document = self.upload(headers, kb_id, "a.md", MARKDOWN).json()
        self.drain()
        key = self.doc(document["id"]).file_key
        self.client.delete(f"/api/v1/knowledge-bases/{kb_id}", headers=headers)
        self.drain()
        self.assertFalse(file_store.exists(key))
        self.assertIsNotNone(self.doc(document["id"]).deleted_at)
        self.assertEqual(self.engine_documents(), [])

    def test_a_knowledge_base_with_documents_cannot_become_an_faq_one(self) -> None:
        headers = self.login("admin@example.com")
        kb_id = self.kb(headers)
        self.upload(headers, kb_id, "a.md", MARKDOWN)
        body = self.client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=headers).json()
        blocked = self.client.put(f"/api/v1/knowledge-bases/{kb_id}", headers=headers, json=self.settings_payload(body, kb_type="faq"))
        self.assertEqual((blocked.status_code, blocked.json()["code"]), (409, "knowledge.has_documents"))

    def test_a_failed_overwrite_keeps_the_original_file_and_row(self) -> None:
        headers = self.login("admin@example.com")
        kb_id = self.kb(headers)
        first = self.upload(headers, kb_id, "a.md", MARKDOWN).json()
        key = self.doc(first["id"]).file_key
        with self.session_factory.begin() as db:
            subscription = get_subscription(db, self.tenant_id)
            subscription.quotas = {**subscription.quotas, "storage_gb": 0}
        refused = self.upload(headers, kb_id, "a.md", MARKDOWN, on_conflict="overwrite")
        self.assertEqual((refused.status_code, refused.json()["code"]), (409, "plan.quota_exceeded"))
        self.assertTrue(file_store.exists(key))  # the rollback restored the row, so the file must still be there
        self.assertIsNone(self.doc(first["id"]).deleted_at)
        self.client.delete(f"/api/v1/knowledge-bases/{kb_id}/documents/{first['id']}", headers=headers)
        self.assertFalse(file_store.exists(key))  # and a committed delete does remove it

    def test_a_rebuild_that_fails_midway_leaves_the_documents_untouched(self) -> None:
        headers = self.login("admin@example.com")
        kb_id = self.kb(headers)
        document = self.upload(headers, kb_id, "a.md", MARKDOWN).json()
        self.drain()
        before = self.doc(document["id"])
        body = self.client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=headers).json()

        def failing_hook(_db, _kb, _new):
            raise KnowledgeEngineError("engine went away")

        knowledge.REINDEX_HOOKS.append(failing_hook)
        try:
            self.client.put(
                f"/api/v1/knowledge-bases/{kb_id}", headers=headers,
                json=self.settings_payload(body, chunking="fixed_512", confirm_reindex=True),
            )
            self.worker()
        finally:
            knowledge.REINDEX_HOOKS.remove(failing_hook)
        after = self.doc(document["id"])
        self.assertEqual((after.status, after.engine_dataset_id, after.engine_document_id), ("indexed", before.engine_dataset_id, before.engine_document_id))
        self.assertEqual(self.row(kb_id).sync_status, "failed")
        with self.session_factory() as db:
            uploads = list(db.scalars(select(Job).where(Job.kind == "knowledge.upload_document")))
            self.assertEqual(len(uploads), 1)  # only the original upload; the failed rebuild queued nothing

    def test_file_store_keeps_every_path_inside_its_root(self) -> None:
        with self.assertRaises(ValueError):
            file_store.read("../../etc/passwd")
        with self.assertRaises(ValueError):
            file_store.save("/etc/evil", b"x")


class DifyDocumentTests(DocumentTests):
    """The same behaviour against the Dify API (fake)."""

    kb_engine = "dify"


del _DocumentHarness  # only the concrete test classes are collected

if __name__ == "__main__":
    unittest.main()
