import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from beework import conversations as conv
from beework import memory as mem
from beework import memory_jobs
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
from beework.models.assistants import Assistant
from beework.models.conversations import Conversation, EndUser
from beework.models.jobs import Job
from beework.models.memory import EndUserMemory
from beework.models.runtime import Run
from beework.runtime.oc import OCConfig
from beework.security import hash_password
from beework.worker.queue import claim_jobs, execute_job
from isolation import disable_external_integrations, stub_public_dns
from test_runtime import ScriptedChat, make_model

PASSWORD = "correct horse battery"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def reply(**payload) -> ScriptedChat:
    return ScriptedChat(chunks=[json.dumps(payload, ensure_ascii=False)])


class MemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        disable_external_integrations()
        stub_public_dns()
        cls.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

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
            for email in ("admin@example.com", "second@example.com"):
                users[email] = User(email=email, display_name=email.split("@")[0], password_hash=hash_password(PASSWORD))
                db.add(users[email])
            db.flush()
            db.add_all([
                Membership(tenant_id=tenant.id, user_id=users["admin@example.com"].id, organization_id=root.id, role="tenant_admin"),
                Membership(tenant_id=tenant.id, user_id=users["second@example.com"].id, organization_id=root.id, role="tenant_admin"),
            ])
            chat = make_model("Chat Mem")
            db.add(chat)
            db.flush()
            environment = TenantModelEnvironment(
                tenant_id=tenant.id, environment="production", gateway_sync_status="synced", gateway_key_encrypted="x"
            )
            db.add(environment)
            db.flush()
            db.add(TenantModelSelection(environment_id=environment.id, model_id=chat.id, model_type="chat", is_default=True))
            assistants = []
            for name in ("Helper", "Second Helper"):
                assistant = Assistant(
                    tenant_id=tenant.id, organization_id=root.id, name=name, description="d",
                    draft_config={"prompt": "You are helpful.", "model_id": str(chat.id)},
                )
                db.add(assistant)
                assistants.append(assistant)
            foreign_assistant = Assistant(tenant_id=other.id, organization_id=other_root.id, name="Foreign", draft_config={})
            db.add(foreign_assistant)
            db.flush()
            end_users = {}
            for key, tenant_id, external in (("a", tenant.id, "u-a"), ("b", tenant.id, "u-b"), ("foreign", other.id, "u-f")):
                end_users[key] = EndUser(tenant_id=tenant_id, channel_type="web", external_id=external, name=key)
                db.add(end_users[key])
            db.flush()
            self.ids = {
                "tenant": tenant.id, "other": other.id,
                "assistant": assistants[0].id, "assistant2": assistants[1].id, "foreign_assistant": foreign_assistant.id,
                "user_a": end_users["a"].id, "user_b": end_users["b"].id, "user_foreign": end_users["foreign"].id,
            }

    # -- helpers ----------------------------------------------------------------

    def add_memory(self, *, file="memory", text="likes tea", end_user="user_a", assistant="assistant", tenant="tenant",
                   age_days=0.0, expires_in_days: float | None = 100, kind="preference") -> UUID:
        with self.session_factory.begin() as db:
            row = EndUserMemory(
                tenant_id=self.ids[tenant], end_user_id=self.ids[end_user],
                assistant_id=None if file == "profile" else self.ids[assistant],
                file=file, type=kind if file == "memory" else None, content=text,
                created_at=NOW - timedelta(days=age_days),
                expires_at=None if expires_in_days is None else NOW + timedelta(days=expires_in_days),
            )
            db.add(row)
            db.flush()
            return row.id

    def load(self, query="tea", *, end_user="user_a", assistant="assistant", oc: OCConfig | None = None, tenant="tenant"):
        with self.session_factory() as db:
            return mem.load_memory_context(
                db, tenant_id=self.ids[tenant], end_user_id=self.ids[end_user], assistant_id=self.ids[assistant],
                oc=oc or OCConfig(), query=query, now=NOW,
            )

    def ended_conversation(self, end_user="user_a", turns=1, with_run=True, oc: dict | None = None) -> UUID:
        with self.session_factory.begin() as db:
            conversation = Conversation(
                tenant_id=self.ids["tenant"], assistant_id=self.ids["assistant"], end_user_id=self.ids[end_user],
                turns=turns, status="resolved", ended_at=NOW,
            )
            db.add(conversation)
            db.flush()
            for seq, (role, text) in enumerate((("user", "I prefer short answers, call me on 13812345678"), ("assistant", "Noted")), 1):
                db.add(conv.Message(tenant_id=self.ids["tenant"], conversation_id=conversation.id, seq=seq, role=role, content=text))
            if with_run:
                db.add(Run(
                    tenant_id=self.ids["tenant"], assistant_id=self.ids["assistant"], conversation_id=conversation.id,
                    status="succeeded", config={"prompt": "p", "model_alias": "bw-x", "oc": oc or {}},
                ))
            return conversation.id

    def extract(self, conversation_id: UUID, llm: ScriptedChat):
        with self.session_factory() as db:
            return mem.extract_conversation_memory(db, conversation_id, llm_factory=lambda *_: llm, now=NOW)

    def rows(self, **filters) -> list[EndUserMemory]:
        with self.session_factory() as db:
            statement = select(EndUserMemory).order_by(EndUserMemory.file, EndUserMemory.content)
            for key, value in filters.items():
                statement = statement.where(getattr(EndUserMemory, key) == value)
            return list(db.scalars(statement))

    # -- retrieval -----------------------------------------------------------------

    def test_relevance_beats_recency_but_recency_breaks_ties(self) -> None:
        self.add_memory(text="prefers green tea in the morning", age_days=20)
        self.add_memory(text="owns a red bicycle", age_days=1)
        self.add_memory(text="likes quiet hotels", age_days=2)
        ranked = [m["text"] for m in self.load("what tea do they drink")]
        self.assertEqual(ranked[0], "prefers green tea in the morning")
        self.assertEqual(ranked[1:], ["owns a red bicycle", "likes quiet hotels"])  # no overlap: newest first

    def test_chinese_text_is_matched_with_character_bigrams(self) -> None:
        self.add_memory(text="用户喜欢乌龙茶，不喜欢咖啡")
        self.add_memory(text="用户住在上海浦东")
        self.assertEqual(self.load("推荐一款乌龙茶")[0]["text"], "用户喜欢乌龙茶，不喜欢咖啡")

    def test_top_k_expiry_and_ordering_of_files(self) -> None:
        for index in range(6):
            self.add_memory(text=f"memory {index}", age_days=index)
        self.add_memory(text="expired fact", expires_in_days=-1)
        self.add_memory(file="profile", text="is a travel agent")
        self.add_memory(file="session", text="asked about tea last time")
        context = self.load("memory")
        self.assertEqual([m["file"] for m in context], ["UserProfile", "UserMemory", "UserMemory", "UserMemory", "Session"])
        self.assertNotIn("expired fact", [m["text"] for m in context])
        oc = OCConfig.model_validate({"memory": {"long_term": {"user_memory": {"top_k": 1}}}})
        self.assertEqual([m["file"] for m in self.load("memory", oc=oc)].count("UserMemory"), 1)

    def test_time_decay_halves_relevance_every_thirty_days(self) -> None:
        self.add_memory(text="likes tea", age_days=0)
        self.add_memory(text="likes tea too", age_days=60)
        with self.session_factory() as db:
            scores = [i.score for i in mem.KeywordMemoryRetriever().retrieve(
                db, tenant_id=self.ids["tenant"], end_user_id=self.ids["user_a"], assistant_id=self.ids["assistant"],
                file="memory", query="tea", top_k=5, now=NOW)]
        self.assertAlmostEqual(scores[1] / scores[0], 0.25 * 1.0, delta=0.05)  # two half-lives

    # -- isolation -----------------------------------------------------------------

    def test_memory_is_isolated_by_user_tenant_and_assistant(self) -> None:
        self.add_memory(text="a private to assistant one", assistant="assistant")
        self.add_memory(text="b belongs to user b", end_user="user_b")
        self.add_memory(file="profile", text="a shared profile")
        self.add_memory(text="foreign tenant secret", tenant="other", end_user="user_foreign", assistant="foreign_assistant")
        texts = lambda **kw: [m["text"] for m in self.load("a", **kw)]  # noqa: E731
        self.assertEqual(sorted(texts()), ["a private to assistant one", "a shared profile"])
        self.assertEqual(texts(assistant="assistant2"), ["a shared profile"])  # profile is shared, memory is private
        self.assertEqual([m["text"] for m in self.load("b", end_user="user_b")], ["b belongs to user b"])
        # Asking with the foreign tenant id never returns Acme's rows, and Acme's id never returns foreign rows.
        self.assertEqual(self.load("a", end_user="user_a", tenant="other"), [])
        self.assertEqual(self.load("secret", end_user="user_foreign", assistant="foreign_assistant", tenant="other"), [
            {"file": "UserMemory", "text": "foreign tenant secret"}])
        self.assertEqual([m for m in self.load("secret") if "secret" in m["text"]], [])

    def test_oc_switches_control_what_is_injected(self) -> None:
        self.add_memory(file="profile", text="profile fact")
        self.add_memory(text="memory fact")
        self.add_memory(file="session", text="session fact")

        def files(config: dict) -> list[str]:
            return [m["file"] for m in self.load("fact", oc=OCConfig.model_validate({"memory": {"long_term": config}}))]

        self.assertEqual(files({}), ["UserProfile", "UserMemory", "Session"])
        self.assertEqual(files({"enabled": False}), [])
        self.assertEqual(files({"user_profile": {"subscribe_enabled": False}}), ["UserMemory", "Session"])
        self.assertEqual(files({"user_memory": {"enabled": False}, "session": {"enabled": False}}), ["UserProfile"])
        self.assertEqual(self.load("fact", end_user="user_a", oc=OCConfig()), self.load("fact"))
        with self.session_factory() as db:
            self.assertEqual(mem.load_memory_context(db, tenant_id=self.ids["tenant"], end_user_id=None,
                                                     assistant_id=self.ids["assistant"], oc=OCConfig(), query="x"), [])

    # -- extraction ----------------------------------------------------------------

    def test_extraction_writes_whitelisted_types_redacted_with_the_right_scope(self) -> None:
        conversation_id = self.ended_conversation()
        llm = reply(
            user_memory=[
                {"type": "preference", "text": "prefers short answers, phone 13812345678"},
                {"type": "history", "text": "asked about refunds twice"},
                {"type": "fact", "text": "is travelling this week"},  # not in the default whitelist
            ],
            profile=[{"text": "travel agent, call me Ada"}],
            session_summary="Short refund chat.",
        )
        self.assertEqual(self.extract(conversation_id, llm), {"memory": 2, "profile": 1, "session": 1})
        by_file = {f: self.rows(file=f) for f in ("memory", "profile", "session")}
        self.assertEqual([r.content for r in by_file["memory"]], ["asked about refunds twice", "prefers short answers, phone [手机号]"])
        self.assertEqual({r.type for r in by_file["memory"]}, {"preference", "history"})
        self.assertTrue(all(r.assistant_id == self.ids["assistant"] for r in by_file["memory"] + by_file["session"]))
        self.assertIsNone(by_file["profile"][0].assistant_id)  # shared across assistants
        self.assertTrue(all(r.end_user_id == self.ids["user_a"] and r.source_conversation_id == conversation_id
                            for rows in by_file.values() for r in rows))
        self.assertEqual(by_file["memory"][0].expires_at.replace(tzinfo=UTC), NOW + timedelta(days=180))
        self.assertEqual(by_file["profile"][0].expires_at.replace(tzinfo=UTC), NOW + timedelta(days=365))
        self.assertEqual(by_file["session"][0].expires_at.replace(tzinfo=UTC), NOW + timedelta(days=30))

    def test_extraction_is_idempotent_and_replaces_its_own_output(self) -> None:
        conversation_id = self.ended_conversation()
        first = reply(user_memory=[{"type": "preference", "text": "likes tea"}], session_summary="chat one")
        self.extract(conversation_id, first)
        self.extract(conversation_id, first)
        self.assertEqual(len(self.rows()), 2)  # no duplicates after a retry
        self.extract(conversation_id, reply(user_memory=[{"type": "preference", "text": "likes coffee"}], session_summary="chat two"))
        self.assertEqual([r.content for r in self.rows()], ["likes coffee", "chat two"])
        # Another conversation's memories are not touched by re-extraction.
        other_conversation = self.ended_conversation()
        self.extract(other_conversation, reply(user_memory=[{"type": "preference", "text": "likes biking"}]))
        self.extract(conversation_id, first)
        self.assertEqual(sorted(r.content for r in self.rows(file="memory")), ["likes biking", "likes tea"])

    def test_oc_settings_gate_what_extraction_writes(self) -> None:
        payload = dict(user_memory=[{"type": "preference", "text": "p"}], profile=[{"text": "who"}], session_summary="s")
        off = self.ended_conversation(oc={"memory": {"long_term": {"user_profile": {"write_enabled": False}}}})
        self.assertEqual(self.extract(off, reply(**payload)), {"memory": 1, "session": 1})  # no "profile" key: not written
        self.assertEqual(self.rows(file="profile"), [])
        no_short = self.ended_conversation(oc={"memory": {"short_term": {"enabled": False}}})
        self.assertNotIn("memory", self.extract(no_short, reply(**payload)))  # short-term memory off: nothing written to UserMemory
        disabled = self.ended_conversation(oc={"memory": {"long_term": {"enabled": False}}})
        before = len(self.rows())
        self.assertEqual(self.extract(disabled, reply(**payload)), {})
        self.assertEqual(len(self.rows()), before)
        facts = self.ended_conversation(oc={"memory": {"short_term": {"type_whitelist": ["fact"]}}})
        self.assertEqual(self.extract(facts, reply(user_memory=[{"type": "fact", "text": "f"}, {"type": "preference", "text": "p"}]))["memory"], 1)

    def test_extraction_skips_when_there_is_nothing_to_learn_from(self) -> None:
        llm = reply(session_summary="x")
        no_turns = self.ended_conversation(turns=0)
        no_run = self.ended_conversation(with_run=False)
        self.assertEqual(self.extract(no_turns, llm), {})
        self.assertEqual(self.extract(no_run, llm), {})
        with self.session_factory() as db:
            self.assertEqual(mem.extract_conversation_memory(db, uuid4(), llm_factory=lambda *_: llm), {})
        self.assertEqual(self.rows(), [])

    def test_unparsable_output_raises_so_the_job_is_retried(self) -> None:
        conversation_id = self.ended_conversation()
        with self.assertRaises(ValueError):
            self.extract(conversation_id, ScriptedChat(chunks=["sorry, no JSON here"]))
        self.assertEqual(self.rows(), [])
        tolerant = ScriptedChat(chunks=['Sure! ```json\n{"session_summary": "fine"}\n```'])
        self.assertEqual(self.extract(conversation_id, tolerant)["session"], 1)

    # -- hook, job, retention -------------------------------------------------------

    def test_ending_a_conversation_queues_extraction_and_the_job_writes_memory(self) -> None:
        with self.session_factory.begin() as db:
            conversation = Conversation(
                tenant_id=self.ids["tenant"], assistant_id=self.ids["assistant"], end_user_id=self.ids["user_a"],
                turns=1, last_active_at=datetime.now(UTC) - timedelta(hours=1),
            )
            db.add(conversation)
            db.flush()
            db.add(conv.Message(tenant_id=self.ids["tenant"], conversation_id=conversation.id, seq=1, role="user", content="I like tea"))
            db.add(Run(tenant_id=self.ids["tenant"], assistant_id=self.ids["assistant"], conversation_id=conversation.id,
                       status="succeeded", config={"prompt": "p", "model_alias": "bw-x"}))
            conversation_id = conversation.id
        with self.session_factory() as db:
            self.assertEqual(conv.end_inactive_conversations(db), 1)
        with self.session_factory() as db:
            job = db.scalar(select(Job).where(Job.kind == "memory.extract"))
            self.assertEqual(job.payload, {"conversation_id": str(conversation_id)})
            self.assertTrue(job.idempotency_key.startswith(f"memory.extract:{conversation_id}:"))
            self.assertEqual(job.tenant_id, self.ids["tenant"])
            claimed = claim_jobs(db, "w")
            db.commit()
            job_id = claimed[0].id
        llm = reply(user_memory=[{"type": "preference", "text": "likes tea"}], session_summary="tea chat")
        with mock.patch.object(memory_jobs, "gateway_extraction_llm", lambda *_: llm), self.session_factory() as db:
            execute_job(db, job_id)
            self.assertEqual(db.get(Job, job_id).status, "succeeded")
        self.assertEqual([r.content for r in self.rows()], ["likes tea", "tea chat"])

    def test_extraction_job_failure_is_retried_not_lost(self) -> None:
        conversation_id = self.ended_conversation()
        with self.session_factory.begin() as db:
            memory_jobs.enqueue_extraction(db, db.get(Conversation, conversation_id))
        with self.session_factory() as db:
            job_id = claim_jobs(db, "w")[0].id
            db.commit()
        with mock.patch.object(memory_jobs, "gateway_extraction_llm", lambda *_: ScriptedChat(chunks=["nope"])), \
                self.session_factory() as db:
            execute_job(db, job_id)
            job = db.get(Job, job_id)
            self.assertEqual((job.status, job.attempts), ("queued", 1))
            self.assertIn("JSON", job.last_error)

    def test_conversations_without_an_end_user_or_turns_are_not_queued(self) -> None:
        with self.session_factory.begin() as db:
            empty = Conversation(tenant_id=self.ids["tenant"], assistant_id=self.ids["assistant"], end_user_id=self.ids["user_a"], turns=0)
            anonymous = Conversation(tenant_id=self.ids["tenant"], assistant_id=self.ids["assistant"], turns=2)
            db.add_all([empty, anonymous])
            db.flush()
            memory_jobs.enqueue_extraction(db, empty)
            memory_jobs.enqueue_extraction(db, anonymous)
        with self.session_factory() as db:
            self.assertIsNone(db.scalar(select(Job).where(Job.kind == "memory.extract")))

    def test_retention_purges_expired_entries(self) -> None:
        keep = self.add_memory(text="keep", expires_in_days=5)
        self.add_memory(text="gone", expires_in_days=-1)
        forever = self.add_memory(text="forever", expires_in_days=None)
        with self.session_factory() as db:
            self.assertEqual(mem.purge_expired_memories(db, NOW), 1)
            self.assertEqual(mem.purge_expired_memories(db, NOW), 0)
        self.assertEqual({r.id for r in self.rows()}, {keep, forever})
        with self.session_factory() as db:
            self.assertEqual(len(list(db.scalars(select(AuditEvent).where(AuditEvent.action == "memory.retention_purged")))), 1)

    # -- deletion ------------------------------------------------------------------

    def test_deleting_an_end_users_memory_removes_only_theirs_and_is_audited(self) -> None:
        self.add_memory(text="a one")
        self.add_memory(file="profile", text="a profile")
        self.add_memory(file="session", text="a session", assistant="assistant2")
        self.add_memory(text="b one", end_user="user_b")
        self.add_memory(text="foreign", tenant="other", end_user="user_foreign", assistant="foreign_assistant")
        with self.session_factory() as db:
            self.assertEqual(mem.delete_end_user_memories(db, self.ids["other"], self.ids["user_a"]), 0)  # wrong tenant
        self.assertEqual(len(self.rows()), 5)
        with self.session_factory() as db:
            self.assertEqual(mem.delete_end_user_memories(db, self.ids["tenant"], self.ids["user_a"]), 3)
        self.assertEqual(sorted(r.content for r in self.rows()), ["b one", "foreign"])
        self.assertEqual(self.load("a"), [])  # nothing is injected after deletion
        with self.session_factory() as db:
            audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "memory.user_deleted", AuditEvent.tenant_id == self.ids["tenant"]))
            self.assertEqual((audit.details["entries"], audit.details["end_user_id"]), ("3", str(self.ids["user_a"])))

    # -- runtime integration -------------------------------------------------------

    def login(self, email: str) -> dict[str, str]:
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).status_code, 200)
        return {"X-CSRF-Token": self.client.cookies.get("beework_csrf"), "X-Tenant-ID": str(self.ids["tenant"])}

    def run_message(self, headers: dict, message: str, llm: ScriptedChat, assistant: UUID | None = None) -> dict:
        from beework.runtime.executor import execute_run

        response = self.client.post(
            f"/api/v1/assistants/{assistant or self.ids['assistant']}/runs", json={"message": message}, headers=headers
        )
        self.assertEqual(response.status_code, 202, response.text)
        with self.session_factory() as db:
            execute_run(db, UUID(response.json()["run_id"]), llm_factory=lambda *_: llm)
        return response.json()

    def test_a_run_injects_the_callers_own_memory_into_the_system_prompt(self) -> None:
        headers = self.login("admin@example.com")
        first = self.run_message(headers, "hello", ScriptedChat(chunks=["hi"]))
        with self.session_factory() as db:
            console_user = db.get(Conversation, UUID(first["conversation_id"])).end_user_id
        with self.session_factory.begin() as db:
            now = datetime.now(UTC)
            db.add_all([
                EndUserMemory(tenant_id=self.ids["tenant"], end_user_id=console_user, assistant_id=self.ids["assistant"],
                              file="memory", type="preference", content="likes oolong tea", created_at=now),
                EndUserMemory(tenant_id=self.ids["tenant"], end_user_id=console_user, assistant_id=None,
                              file="profile", content="works as a travel agent", created_at=now),
                EndUserMemory(tenant_id=self.ids["tenant"], end_user_id=self.ids["user_b"], assistant_id=self.ids["assistant"],
                              file="memory", type="preference", content="someone else's secret", created_at=now),
            ])
        llm = ScriptedChat(chunks=["ok"])
        self.run_message(headers, "recommend a tea", llm)
        system = llm.seen_messages[0][0].content
        self.assertIn("# 记忆", system)
        self.assertIn("[UserProfile] works as a travel agent", system)
        self.assertIn("[UserMemory] likes oolong tea", system)
        self.assertNotIn("someone else's secret", system)
        # The second assistant sees the shared profile but not the private UserMemory.
        llm2 = ScriptedChat(chunks=["ok"])
        self.run_message(headers, "hi", llm2, assistant=self.ids["assistant2"])
        system2 = llm2.seen_messages[0][0].content
        self.assertIn("works as a travel agent", system2)
        self.assertNotIn("oolong", system2)
        # A different console user has a different end user, so none of it applies to them.
        other_headers = self.login("second@example.com")
        llm3 = ScriptedChat(chunks=["ok"])
        self.run_message(other_headers, "hi", llm3)
        self.assertNotIn("# 记忆", llm3.seen_messages[0][0].content)

    def test_memory_disabled_in_oc_injects_nothing(self) -> None:
        headers = self.login("admin@example.com")
        first = self.run_message(headers, "hello", ScriptedChat(chunks=["hi"]))
        with self.session_factory.begin() as db:
            console_user = db.get(Conversation, UUID(first["conversation_id"])).end_user_id
            db.add(EndUserMemory(tenant_id=self.ids["tenant"], end_user_id=console_user, assistant_id=self.ids["assistant"],
                                 file="memory", type="preference", content="likes oolong tea"))
            assistant = db.get(Assistant, self.ids["assistant"])
            assistant.draft_config = {**assistant.draft_config, "oc": {"memory": {"long_term": {"enabled": False}}}}
        llm = ScriptedChat(chunks=["ok"])
        self.run_message(headers, "tea?", llm)
        self.assertNotIn("oolong", llm.seen_messages[0][0].content)


if __name__ == "__main__":
    unittest.main()
