"""T-163: the Dify adapter v2 — application parameters, the type check, conversation binding and stickiness, streaming, the user hash, retries."""

import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select

import test_flow_connections as base
from beework import flow_connection_service as svc
from beework.models.capability_registry import CapabilityEntry
from beework.models.flow_connections import DifyConversation, FlowConnection
from beework.tools.executor import ToolContext, invoke_tool

FORM = [
    {"text-input": {"label": "订单号", "variable": "order_no", "required": True, "max_length": 32}},
    {"select": {"label": "类型", "variable": "kind", "required": False, "options": ["退货", "换货"], "default": "退货"}},
    {"number": {"label": "数量", "variable": "qty", "required": False}},
    {"file": {"label": "附件", "variable": "doc", "required": False}},
]


def sse(*events: dict) -> bytes:
    return b"".join(b"data: " + json.dumps(e, ensure_ascii=False).encode() + b"\n\n" for e in events)


class DifyAdapterTests(base.FlowConnectionTests):
    CONVERSATION = uuid4()

    def ctx(self, name: str, **extra) -> ToolContext:
        return ToolContext(tenant_id=UUID(self.ids["tenant"]), allowed=frozenset({name}), run_id=uuid4(), end_user="user-1", **extra)

    def call(self, conn_id: str, args: dict, ctx: ToolContext | None = None):
        name = svc.tool_name(UUID(conn_id))
        with self.session_factory() as db, mock.patch.object(svc, "_conversation_of", lambda *_: self.CONVERSATION):
            result = invoke_tool(db, ctx or self.ctx(name), name, args)
            db.commit()
        return result

    # -- parameters and the type check -----------------------------------------------------------------

    def test_the_applications_variables_become_the_tools_schema_in_the_registry(self) -> None:
        self.server.form = FORM
        data = self.dify("chatflow", "With form")
        self.assertEqual([p["variable"] for p in data["parameters"]], ["order_no", "kind", "qty"])  # the file input is left out
        self.assertEqual((data["app_mode"], data["parameters_error"]), ("advanced-chat", None))
        with self.session_factory() as db:
            schema = db.scalar(select(CapabilityEntry).where(CapabilityEntry.source_id == UUID(data["id"]))).input_schema
        self.assertEqual(schema["required"], ["query", "order_no"])
        self.assertEqual(schema["properties"]["kind"]["enum"], ["退货", "换货"])
        self.assertEqual((schema["properties"]["qty"]["type"], schema["properties"]["order_no"]["maxLength"]), ("number", 32))
        self.call(data["id"], {"query": "查", "order_no": "A1", "qty": 2, "stray": "x"})
        self.assertEqual(self.server.bodies[-1]["inputs"], {"order_no": "A1", "qty": 2})  # the application's own variables only

    def test_syncing_again_updates_the_registrys_schema_and_a_changed_target_drops_the_old_variables(self) -> None:
        data = self.dify("chatflow", "Changes")
        self.assertEqual(data["parameters"], [])
        self.server.form = FORM
        self.assertEqual(len(self.client.post(f"/api/v1/flow-connections/{data['id']}/sync", headers=self.admin).json()["parameters"]), 3)
        with self.session_factory() as db:
            schema = db.scalar(select(CapabilityEntry).where(CapabilityEntry.source_id == UUID(data["id"]))).input_schema
        self.assertIn("order_no", schema["properties"])  # the entry follows the application, not only the name
        self.server.fail = 503  # Dify is down while the connection is pointed at another application
        with mock.patch.object(svc.time, "sleep", lambda _s: None):
            moved = self.update(data["id"], **{**self.dify_body("chatflow", "Changes"), "app_id": "another-app", "row_version": self.conn(data["id"])["row_version"]})
        self.assertEqual(moved.status_code, 200)
        self.assertEqual((moved.json()["parameters"], moved.json()["app_mode"]), ([], None))  # the old application's variables are gone
        self.assertTrue(moved.json()["parameters_error"])

    def test_a_variable_named_query_cannot_shadow_the_question(self) -> None:
        self.server.form = [{"text-input": {"label": "q", "variable": "query", "required": True}}, {"text-input": {"label": "n", "variable": "name", "required": True}}]
        data = self.dify("chatflow", "Shadow")
        with self.session_factory() as db:
            schema = db.scalar(select(CapabilityEntry).where(CapabilityEntry.source_id == UUID(data["id"]))).input_schema
        self.assertEqual((schema["required"], schema["properties"]["query"]["type"], list(schema["properties"])), (["query", "name"], "string", ["query", "name"]))
        self.call(data["id"], {"query": "问", "name": "x"})
        self.assertEqual(self.server.bodies[-1]["inputs"], {"name": "x"})

    def test_an_input_mapping_overrides_the_read_variables(self) -> None:
        self.server.form = FORM
        data = self.dify("workflow", "Mapped", input_mapping={"order_no": "order"})
        self.call(data["id"], {"order": "B2", "qty": 1})
        self.assertEqual(self.server.bodies[-1]["inputs"], {"order_no": "B2"})

    def test_an_application_of_the_wrong_type_is_refused_with_the_reason(self) -> None:
        self.server.mode_forced = self.server.mode = "workflow"
        refused = self.connect(self.dify_body("chatflow", "Wrong"))
        self.assertEqual((refused.status_code, refused.json()["code"]), (422, "flow_connection.kind_mismatch"))
        self.assertIn("Workflow", refused.json()["detail"])
        self.server.mode = "completion"
        self.assertEqual(self.connect(self.dify_body("workflow", "Wrong 2")).status_code, 422)

    def test_a_failed_read_is_recorded_and_does_not_block_saving_and_sync_reads_again(self) -> None:
        self.server.fail = 401
        saved = self.connect(self.dify_body("chatflow", "Unreadable"))
        self.assertEqual(saved.status_code, 201)
        self.assertIsNone(saved.json()["parameters_synced_at"])
        self.assertIn("Dify", saved.json()["parameters_error"])
        self.server.fail = None
        self.server.form = FORM
        synced = self.client.post(f"/api/v1/flow-connections/{saved.json()['id']}/sync", headers=self.admin)
        self.assertEqual(synced.status_code, 200, synced.text)
        self.assertEqual((len(synced.json()["parameters"]), synced.json()["parameters_error"]), (3, None))
        self.assertIsNotNone(synced.json()["parameters_synced_at"])

    def test_reads_are_retried_on_a_server_error_but_not_on_a_bad_key(self) -> None:
        data = self.dify("chatflow", "Retry")
        calls = []
        original = self.server.__call__

        def flaky(request):
            if request.method == "GET" and request.url.path.endswith("/info"):
                calls.append(1)
                if len(calls) < 3:
                    return httpx.Response(503, json={"message": "busy"})
            return original(request)

        svc.TRANSPORT = httpx.MockTransport(flaky)
        with mock.patch.object(svc.time, "sleep", lambda _s: None):
            self.assertEqual(self.client.post(f"/api/v1/flow-connections/{data['id']}/sync", headers=self.admin).json()["parameters_error"], None)
        self.assertEqual(len(calls), 3)
        self.server.key = "another"
        with mock.patch.object(svc.time, "sleep", lambda _s: None):
            failed = self.client.post(f"/api/v1/flow-connections/{data['id']}/sync", headers=self.admin).json()
        self.assertIn("拒绝", failed["parameters_error"])

    # -- the user ---------------------------------------------------------------------------------------

    def test_the_dify_user_is_a_stable_hash_without_the_end_user_id(self) -> None:
        data = self.dify()
        self.call(data["id"], {"query": "a"})
        self.call(data["id"], {"query": "b"})
        users = [b["user"] for b in self.server.bodies[-2:]]
        self.assertEqual(users[0], users[1])
        self.assertNotIn("user-1", users[0])
        self.assertEqual(users[0], svc.user_hash(UUID(self.ids["tenant"]), "user-1"))
        self.assertNotEqual(svc.user_hash(UUID(self.ids["tenant"]), "user-2"), users[0])
        self.assertNotEqual(svc.user_hash(uuid4(), "user-1"), users[0])

    # -- conversation binding and stickiness ----------------------------------------------------------

    def test_the_second_message_continues_the_same_dify_conversation(self) -> None:
        data = self.dify()
        self.call(data["id"], {"query": "第一句"})
        self.assertNotIn("conversation_id", self.server.bodies[-1])
        self.call(data["id"], {"query": "第二句"})
        self.assertEqual(self.server.bodies[-1]["conversation_id"], "c1")  # what Dify returned the first time
        with self.session_factory() as db:
            row = db.scalar(select(DifyConversation))
            self.assertEqual((row.dify_conversation_id, row.status, str(row.conversation_id)), ("c1", "active", str(self.CONVERSATION)))
            row.last_active_at = datetime.now(UTC) - timedelta(minutes=11)  # idle for longer than the 10 minutes
            db.commit()
        self.call(data["id"], {"query": "第三句"})
        self.assertNotIn("conversation_id", self.server.bodies[-1])  # a fresh Dify conversation

    def test_stickiness_ends_with_an_exit_word_idle_time_or_the_switch(self) -> None:
        data = self.dify("chatflow", "Sticky", exit_words=["退出", "取消"], idle_ttl_minutes=5)
        self.assertEqual((data["sticky"], data["idle_ttl_minutes"], data["exit_words"]), (True, 5, ["退出", "取消"]))
        self.call(data["id"], {"query": "开始"})
        tenant, conn_id, conversation = UUID(self.ids["tenant"]), UUID(data["id"]), self.CONVERSATION

        def waiting(text: str):
            with self.session_factory() as db:
                found = svc.sticky_connection(db, tenant, conn_id, conversation, text)
                db.commit()
                return found is not None

        self.assertTrue(waiting("我的订单号是 A1"))
        self.assertFalse(waiting(" 取消。 "))  # an exit word, punctuation and spaces aside
        self.assertFalse(waiting("继续"))  # and it closed the binding
        self.call(data["id"], {"query": "再开始"})
        self.assertTrue(waiting("继续"))
        with self.session_factory() as db:
            db.scalar(select(DifyConversation)).last_active_at = datetime.now(UTC) - timedelta(minutes=6)
            db.commit()
        self.assertFalse(waiting("继续"))  # idle for longer than 5 minutes
        self.call(data["id"], {"query": "又开始"})
        self.update(data["id"], **{**self.dify_body("chatflow", "Sticky"), "sticky": False, "row_version": self.conn(data["id"])["row_version"]})
        self.assertFalse(waiting("继续"))

    # -- streaming --------------------------------------------------------------------------------------

    def stream_with(self, body: bytes, status: int = 200):
        original = self.server.__call__
        self.stops: list[str] = []

        def handler(request):
            if request.method == "POST" and request.url.path.endswith("/stop"):
                self.stops.append(request.url.path)
                return httpx.Response(200, json={"result": "success"})
            if request.method == "POST" and json.loads(request.content).get("response_mode") == "streaming":
                return httpx.Response(status, content=body, headers={"content-type": "text/event-stream"})
            return original(request)

        svc.TRANSPORT = httpx.MockTransport(handler)

    def test_a_streamed_chatflow_answer_reaches_the_sink_token_by_token(self) -> None:
        data = self.dify("chatflow", "Streams", streaming=True)
        self.stream_with(sse(
            {"event": "workflow_started", "conversation_id": "d9", "task_id": "t1"}, {"event": "message", "answer": "你", "conversation_id": "d9", "task_id": "t1"},
            {"event": "message", "answer": "好", "conversation_id": "d9"}, {"event": "message_end", "conversation_id": "d9"},
        ))
        tokens: list[str] = []
        name = svc.tool_name(UUID(data["id"]))
        result = self.call(data["id"], {"query": "hi"}, self.ctx(name, stream=tokens.append))
        self.assertEqual((result.status, result.content, tokens), ("ok", "你好", ["你", "好"]))
        with self.session_factory() as db:
            self.assertEqual(db.scalar(select(DifyConversation)).dify_conversation_id, "d9")

    def test_a_cut_off_stream_is_an_error_even_after_some_text_and_crlf_and_a_missing_last_blank_line_are_read(self) -> None:
        data = self.dify("chatflow", "Cut", streaming=True)
        self.stream_with(sse({"event": "message", "answer": "半句", "task_id": "t"}))
        self.assertEqual(self.call(data["id"], {"query": "x"}).code, "tool.stream_interrupted")
        crlf = sse({"event": "message", "answer": "完", "task_id": "t"}, {"event": "message_end"}).replace(b"\n", b"\r\n")
        self.stream_with(crlf)
        self.assertEqual(self.call(data["id"], {"query": "x"}).content, "完")
        self.stream_with(sse({"event": "message", "answer": "尾"}, {"event": "message_end"})[:-2])  # the last frame has no blank line after it
        self.assertEqual(self.call(data["id"], {"query": "x"}).content, "尾")
        self.stream_with(sse({"event": "error", "status": "internal", "message": "boom"}))
        self.assertEqual(self.call(data["id"], {"query": "x"}).code, "tool.upstream_error")  # a non-numeric status is still a classified error

    def test_a_streamed_workflow_returns_only_the_final_output(self) -> None:
        data = self.dify("workflow", "Streams too", streaming=True, output_mapping={"field": "text"})
        self.stream_with(sse(
            {"event": "workflow_started", "task_id": "t2"}, {"event": "text_chunk", "data": {"text": "中间"}},
            {"event": "workflow_finished", "data": {"status": "succeeded", "outputs": {"text": "最终", "x": 1}}},
        ))
        tokens: list[str] = []
        name = svc.tool_name(UUID(data["id"]))
        result = self.call(data["id"], {"inputs": {}}, self.ctx(name, stream=tokens.append))
        self.assertEqual((result.content, tokens), ("最终", []))

    def test_a_stream_error_event_a_cut_stream_and_a_failed_workflow_are_errors(self) -> None:
        data = self.dify("chatflow", "Broken", streaming=True)
        self.stream_with(sse({"event": "message", "answer": "半", "task_id": "t"}, {"event": "error", "status": 429, "message": "slow down"}))
        self.assertEqual(self.call(data["id"], {"query": "x"}).code, "tool.rate_limited")
        self.stream_with(sse({"event": "workflow_started", "task_id": "t"}))
        self.assertEqual(self.call(data["id"], {"query": "x"}).code, "tool.stream_interrupted")
        flow = self.dify("workflow", "Fails", streaming=True)
        self.stream_with(sse({"event": "workflow_finished", "data": {"status": "failed", "error": "node failed"}}))
        self.assertEqual(self.call(flow["id"], {"inputs": {}}).code, "tool.workflow_failed")

    def test_cancelling_a_run_stops_the_dify_run(self) -> None:
        data = self.dify("chatflow", "Cancel me", streaming=True)
        self.stream_with(sse({"event": "message", "answer": "a", "task_id": "task-9"}, {"event": "message", "answer": "b", "task_id": "task-9"}, {"event": "message_end"}))
        seen = []

        def check_stop():
            seen.append(1)
            if len(seen) > 1:
                raise RuntimeError("cancelled")

        name = svc.tool_name(UUID(data["id"]))
        with self.session_factory() as db, self.assertRaises(RuntimeError), mock.patch.object(svc, "_conversation_of", lambda *_: None):
            conn = db.get(FlowConnection, UUID(data["id"]))
            ctx = self.ctx(name, check_stop=check_stop)
            svc.call_dify(conn, ctx, {"query": "x"})
        self.assertEqual(self.stops, ["/v1/chat-messages/task-9/stop"])


# The connection tests are inherited only for their helpers; they run in test_flow_connections.
for _name in [n for n in dir(base.FlowConnectionTests) if n.startswith("test_")]:
    setattr(DifyAdapterTests, _name, None)

if __name__ == "__main__":
    unittest.main()
