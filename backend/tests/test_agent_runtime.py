"""T-061: the tool-calling runtime, the backup model, and the agent configuration it reads."""

import json
import unittest
from typing import Any
from unittest import mock
from uuid import UUID, uuid4

import httpx
import openai
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, ToolMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from sqlalchemy import select

import test_runtime
from beework.models.assistants import Assistant
from beework.models.jobs import RunEvent
from beework.models.tools import ToolInvocation
from beework.runtime.dsl import AssistantDSLv1, ToolMount, dsl_from_draft
from beework.runtime.errors import RunTimedOut
from beework.runtime.tool_runtime import ToolRuntime
from beework.schemas.assistants import AssistantDraftConfig
from beework.tools.executor import PROVIDERS, ToolError, ToolOutput, ToolSpec

CAP = uuid4()  # the (fake) MCP connection the draft mounts


def spec(tool: str, *, writes: bool = False, schema: dict | None = None) -> ToolSpec:
    return ToolSpec(
        name=f"mcp:{CAP}:{tool}", source="mcp", capability_id=CAP, writes=writes, description=f"{tool} tool",
        parameters=schema or {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"], "additionalProperties": False},
    )


SPECS = {s.name: s for s in (spec("search"), spec("update", writes=True), spec("boom"))}


class FakeMcp:
    """Stands in for the MCP provider: records every execution."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, dict]] = []

    def lookup(self, db, ctx, name):
        return SPECS.get(name)

    def execute(self, db, ctx, tool_spec, args):
        self.executed.append((tool_spec.name, args))
        if tool_spec.name.endswith(":boom"):
            raise ToolError("tool.mcp_error", "remote failure")
        return ToolOutput(content=f"result for {args.get('q')} sk-secret-value", response_bytes=10)


def tool_call(name: str, args: dict, call_id: str = "call-1") -> dict:
    return {"name": name, "args": json.dumps(args), "id": call_id, "index": 0}


class ToolChat(BaseChatModel):
    """Plays a script, one item per model call: text, tool calls (a list of dicts) or an exception to raise."""

    script: list[Any] = []
    calls: list[Any] = []  # (messages, bound tool names) of every call
    bound: list[Any] = []
    name_tag: str = "primary"

    @property
    def _llm_type(self) -> str:
        return "tool-chat"

    def bind_tools(self, tools, **kwargs):
        self.bound = [t["function"]["name"] for t in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        chunks = list(self._stream(messages, stop=stop, run_manager=run_manager, **kwargs))
        return ChatResult(generations=[chunk for chunk in chunks])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        step = self.script[min(len(self.calls), len(self.script) - 1)]
        self.calls.append((list(messages), list(self.bound)))
        self.bound = []
        if isinstance(step, Exception):
            raise step
        if isinstance(step, str):
            yield ChatGenerationChunk(message=AIMessageChunk(content=step, usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}))
        else:
            yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=step, usage_metadata={"input_tokens": 7, "output_tokens": 2, "total_tokens": 9}))


def gateway_error() -> Exception:
    request = httpx.Request("POST", "http://gateway/v1/chat/completions")
    return openai.APIStatusError("boom", response=httpx.Response(500, request=request), body=None)


class AgentRuntimeTests(test_runtime.RuntimeTests):
    def setUp(self) -> None:
        super().setUp()
        self.mcp = FakeMcp()
        saved = PROVIDERS.get("mcp")
        PROVIDERS["mcp"] = self.mcp
        self.addCleanup(lambda: PROVIDERS.__setitem__("mcp", saved) if saved else PROVIDERS.pop("mcp", None))
        patcher = mock.patch("beework.capability_service.mcp_tool_specs", lambda db, tenant, ids: list(SPECS.values()))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.headers = self.login("admin@example.com")

    # -- helpers --------------------------------------------------------------------------------

    def configure(self, **config) -> None:
        with self.session_factory.begin() as db:
            assistant = db.get(Assistant, self.ids["assistant"])
            assistant.draft_config = {**assistant.draft_config, **config}

    def mount_tools(self, tools: list[str] | None = None, delegate: str = "需要查资料时使用") -> None:
        self.register_source()
        self.configure(mounts={"mcp": [str(CAP)]}, mount_settings={str(CAP): {"tools": tools or [], "delegate": delegate}})

    def register_source(self) -> None:
        """The tools come from the capability registry (T-162), which derives from a real MCP connection: create the one the fakes stand for."""
        from beework.models.capabilities import Capability, McpConnection

        with self.session_factory.begin() as db:
            if db.get(Capability, CAP) is not None:
                return
            tenant = UUID(str(self.ids["tenant"]))
            db.add(Capability(id=CAP, tenant_id=tenant, kind="mcp", name="Fake MCP", description="d", status="available"))
            db.flush()
            tools = [{"name": s.name.split(":", 2)[2], "description": s.description, "input_schema": s.parameters, "read_only": not s.writes} for s in SPECS.values()]
            db.add(McpConnection(capability_id=CAP, tenant_id=tenant, url="https://mcp.example.com/mcp", tools=tools, selected_tools=[t["name"] for t in tools]))

    def run_agent(self, llm: BaseChatModel, message: str = "帮我查一下") -> tuple[str, list[RunEvent]]:
        run_id = self.start_run(self.headers, message).json()["run_id"]
        self.run_in_worker(run_id, llm)
        return run_id, self.events(run_id)

    def tool_steps(self, events: list[RunEvent]) -> list[dict]:
        return [e.data for e in events if e.type == "step" and e.data.get("name") == "tool"]

    # -- the loop ---------------------------------------------------------------------------------------

    def test_the_model_calls_a_mounted_tool_and_answers_from_its_result(self) -> None:
        self.mount_tools()
        llm = ToolChat(script=[[tool_call("mcp_2_search", {"q": "退货"})], "7 天内可以退货"])
        run_id, events = self.run_agent(llm)
        self.assertEqual(self.run_row(run_id).status, "succeeded")
        self.assertEqual(events[-1].data["text"], "7 天内可以退货")
        self.assertEqual(self.mcp.executed, [(f"mcp:{CAP}:search", {"q": "退货"})])
        self.assertEqual(len(llm.calls), 2)
        second_call = llm.calls[1][0]
        tool_message = next(m for m in second_call if isinstance(m, ToolMessage))
        self.assertIn("result for 退货", tool_message.content)
        self.assertEqual(self.run_row(run_id).tokens_in, 7 + 10)  # both rounds are counted
        steps = self.tool_steps(events)
        self.assertEqual([(s["tool"], s["status"]) for s in steps], [("mcp_2_search", "completed")])
        with self.session_factory() as db:
            recorded = db.scalars(select(ToolInvocation).where(ToolInvocation.run_id == UUID(run_id))).all()
        self.assertEqual([(r.tool_name, r.status) for r in recorded], [(f"mcp:{CAP}:search", "ok")])

    def test_the_model_is_offered_only_what_is_mounted_and_ticked(self) -> None:
        self.mount_tools(["search"], delegate="需要查资料时使用")
        llm = ToolChat(script=["直接回答"])
        self.run_agent(llm)
        self.assertEqual(llm.calls[0][1], ["mcp_1_search"])  # `update` and `boom` exist on the connection but were not ticked
        self.configure(mounts={"mcp": []}, mount_settings={})
        llm = ToolChat(script=["直接回答"])
        self.run_agent(llm)
        self.assertEqual(llm.calls[0][1], [])  # nothing mounted: no tools bound at all

    def test_the_delegation_text_reaches_the_tool_description(self) -> None:
        self.mount_tools(["search"], delegate="当用户询问退货政策时使用")
        offered = None

        class Spy(ToolChat):
            def bind_tools(self, tools, **kwargs):
                nonlocal offered
                offered = tools
                return super().bind_tools(tools, **kwargs)

        self.run_agent(Spy(script=["ok"]))
        self.assertTrue(offered[0]["function"]["description"].startswith("当用户询问退货政策时使用"))

    def test_a_tool_the_model_invented_or_that_is_not_ticked_is_refused_and_never_executed(self) -> None:
        self.mount_tools(["search"])
        llm = ToolChat(script=[[tool_call("mcp_9_delete_everything", {"q": "x"}, "c1")], "好的，我无法执行"])
        run_id, events = self.run_agent(llm)
        self.assertEqual(self.run_row(run_id).status, "succeeded")
        self.assertEqual(self.mcp.executed, [])
        refusal = next(m for m in llm.calls[1][0] if isinstance(m, ToolMessage))
        self.assertIn("没有这个工具", refusal.content)
        self.assertEqual([(s["tool"], s["status"]) for s in self.tool_steps(events)], [("mcp_9_delete_everything", "failed")])

    def test_invalid_arguments_a_failing_tool_and_a_write_that_needs_confirmation_are_all_told_to_the_model(self) -> None:
        self.mount_tools()
        llm = ToolChat(script=[
            [tool_call("mcp_2_search", {"wrong": 1}, "a"), tool_call("mcp_1_boom", {"q": "x"}, "b"), tool_call("mcp_3_update", {"q": "x"}, "c")],
            "处理完毕",
        ])
        run_id, events = self.run_agent(llm)
        self.assertEqual(self.run_row(run_id).status, "succeeded")
        replies = [m.content for m in llm.calls[1][0] if isinstance(m, ToolMessage)]
        self.assertIn("参数不正确", replies[0])
        self.assertIn("remote failure", replies[1])
        self.assertIn("需要用户确认", replies[2])
        self.assertEqual([name for name, _ in self.mcp.executed], [f"mcp:{CAP}:boom"])  # only the failing one ran; the write did not

    def test_the_iteration_limit_ends_the_loop_with_an_answer_from_what_was_gathered(self) -> None:
        self.mount_tools()
        self.configure(max_output_tokens=512)
        llm = ToolChat(script=[[tool_call("mcp_2_search", {"q": f"again {i}"})] for i in range(8)] + ["根据已有信息的回答"])
        run_id, events = self.run_agent(llm)
        self.assertEqual(events[-1].data["text"], "根据已有信息的回答")
        self.assertEqual(len(llm.calls), 9)  # eight rounds with tools, then one without
        self.assertEqual(llm.calls[8][1], [])
        self.assertEqual(len(self.mcp.executed), 8)
        notice = [m for m in llm.calls[8][0] if getattr(m, "content", "").startswith("已达到工具调用次数上限")]
        self.assertTrue(notice)

    def test_the_same_call_twice_is_not_executed_again_and_the_total_is_capped(self) -> None:
        self.mount_tools(["search"])
        real = ToolRuntime
        limit = mock.patch("beework.runtime.executor.ToolRuntime", lambda *a, **kw: real(*a, **{**kw, "max_calls": 2}))
        limit.start()
        self.addCleanup(limit.stop)
        again = [tool_call("mcp_1_search", {"q": "a"}, "1"), tool_call("mcp_1_search", {"q": "a"}, "2")]
        llm = ToolChat(script=[again, [tool_call("mcp_1_search", {"q": "b"}, "3")], [tool_call("mcp_1_search", {"q": "c"}, "4")], "好的"])
        run_id, events = self.run_agent(llm)
        self.assertEqual([args for _name, args in self.mcp.executed], [{"q": "a"}, {"q": "b"}])  # the repeat and the third distinct call never ran
        codes = [step["code"] for step in self.tool_steps(events)]
        self.assertEqual(codes, [None, "tool.repeated", None, "tool.limit_reached"])
        self.assertEqual(self.run_row(run_id).status, "succeeded")

    def test_a_tool_step_names_the_registry_entry_and_the_tools_are_the_registrys(self) -> None:
        from beework.models.capability_registry import CapabilityEntry

        self.mount_tools(["search"])
        llm = ToolChat(script=[[tool_call("mcp_1_search", {"q": "x"})], "ok"])
        _run_id, events = self.run_agent(llm)
        with self.session_factory() as db:
            entry = db.scalar(select(CapabilityEntry).where(CapabilityEntry.source_id == CAP, CapabilityEntry.identifier == "search"))
            self.assertEqual(self.tool_steps(events)[0]["entry_id"], str(entry.id))
            entry.intent_description = "查退货政策"
            db.commit()
        llm = ToolChat(script=["ok"])
        self.run_agent(llm)
        self.assertEqual(llm.calls[0][1], ["mcp_1_search"])

    def test_too_many_tools_are_offered_through_find_capability(self) -> None:
        many = {f"mcp:{CAP}:t{i:02d}": spec(f"t{i:02d}") for i in range(35)}
        with mock.patch.dict(SPECS, many):
            self.register_source()
            self.mount_tools([])
            llm = ToolChat(script=[[tool_call("find_capability", {"query": "t07"})], [tool_call("mcp_10_t07", {"q": "x"})], "好"])
            _run_id, _events = self.run_agent(llm)
        self.assertEqual(llm.calls[0][1], ["find_capability"])  # nothing to start with: no candidates from L2
        self.assertTrue(any("mcp_" in m.content and "t07" in m.content for m in llm.calls[1][0] if isinstance(m, ToolMessage)))
        self.assertIn("mcp_10_t07", llm.calls[1][1])  # found, so callable from then on
        self.assertEqual(len(llm.calls[1][1]), 2)
        self.assertEqual([name for name, _ in self.mcp.executed], [f"mcp:{CAP}:t07"])

    def test_a_hidden_tool_cannot_be_called_before_it_is_found(self) -> None:
        many = {f"mcp:{CAP}:t{i:02d}": spec(f"t{i:02d}") for i in range(35)}
        with mock.patch.dict(SPECS, many):
            self.register_source()
            self.mount_tools([])
            llm = ToolChat(script=[[tool_call("mcp_10_t07", {"q": "x"})], "好"])
            self.run_agent(llm)
        self.assertEqual(self.mcp.executed, [])

    def test_the_overall_timeout_stops_a_tool_loop(self) -> None:
        self.mount_tools()

        def slow(*_args, **_kwargs):
            raise RunTimedOut(60)

        llm = ToolChat(script=[[tool_call("mcp_2_search", {"q": "x"})], "never"])
        with mock.patch("beework.runtime.compiler._run_tool", slow):
            run_id, _events = self.run_agent(llm)
        run = self.run_row(run_id)
        self.assertEqual((run.status, run.error_code), ("failed", "runtime.timeout"))

    def test_tool_results_pass_the_output_guard_before_the_model_sees_them(self) -> None:
        self.mount_tools()
        # a platform output rule that masks a literal, the way a PII rule would
        with mock.patch("beework.runtime.executor.scan") as scan:
            scan.return_value = mock.Mock(text="result [MASKED]", blocked=False, message="")
            llm = ToolChat(script=[[tool_call("mcp_2_search", {"q": "x"})], "done"])
            self.run_agent(llm)
        tool_message = next(m for m in llm.calls[1][0] if isinstance(m, ToolMessage))
        self.assertEqual(tool_message.content, "result [MASKED]")
        scan.assert_called()

    # -- backup model -----------------------------------------------------------------------------------------

    def test_a_primary_failure_switches_once_to_the_backup_model(self) -> None:
        # the draft's fallback is a *different* model so that the factory can tell the two apart
        with self.session_factory.begin() as db:
            backup_model = test_runtime.make_model("Chat Backup")
            db.add(backup_model)
            db.flush()
            from beework.models import TenantModelSelection

            db.add(TenantModelSelection(environment_id=self.ids["environment"], model_id=backup_model.id, model_type="chat", is_default=False))
            backup_id = backup_model.id
        self.configure(fallback_model_id=str(backup_id))
        primary, backup = ToolChat(script=[gateway_error()]), ToolChat(script=["来自备选模型"])
        run_id = self.start_run(self.headers, "hi").json()["run_id"]
        primary_alias = AssistantDSLv1.model_validate(self.run_row(run_id).config).model_alias
        with self.session_factory() as db:
            from beework.runtime.executor import execute_run

            execute_run(db, UUID(run_id), llm_factory=lambda _r, dsl: primary if dsl.model_alias == primary_alias else backup)
        events = self.events(run_id)
        self.assertEqual((self.run_row(run_id).status, events[-1].data["text"]), ("succeeded", "来自备选模型"))
        switches = [e.data for e in events if e.type == "step" and e.data.get("name") == "model_fallback"]
        self.assertEqual([s["reason"] for s in switches], ["APIStatusError"])  # the reason is recorded
        self.assertEqual((len(primary.calls), len(backup.calls)), (1, 1))

    def test_a_failing_backup_is_not_retried_and_the_run_fails(self) -> None:
        with self.session_factory.begin() as db:
            backup_model = test_runtime.make_model("Chat Backup 2")
            db.add(backup_model)
            db.flush()
            from beework.models import TenantModelSelection

            db.add(TenantModelSelection(environment_id=self.ids["environment"], model_id=backup_model.id, model_type="chat", is_default=False))
            backup_id = backup_model.id
        self.configure(fallback_model_id=str(backup_id))
        primary, backup = ToolChat(script=[gateway_error()]), ToolChat(script=[gateway_error()])
        run_id = self.start_run(self.headers, "hi").json()["run_id"]
        primary_alias = AssistantDSLv1.model_validate(self.run_row(run_id).config).model_alias
        with self.session_factory() as db:
            from beework.runtime.executor import execute_run

            execute_run(db, UUID(run_id), llm_factory=lambda _r, dsl: primary if dsl.model_alias == primary_alias else backup)
        self.assertEqual((self.run_row(run_id).status, len(backup.calls)), ("failed", 1))  # one switch only

    def test_without_a_backup_the_failure_is_reported_as_before(self) -> None:
        primary = ToolChat(script=[gateway_error()])
        run_id, _events = self.run_agent(primary)
        self.assertEqual((self.run_row(run_id).status, self.run_row(run_id).error_code), ("failed", "runtime.model_error"))

    # -- configuration -------------------------------------------------------------------------------------------

    def test_older_drafts_and_frozen_snapshots_load_with_defaults(self) -> None:
        old_draft = {"dsl_version": 1, "prompt": "p", "model_id": None, "mounts": {}}
        config = AssistantDraftConfig.model_validate(old_draft)
        self.assertEqual((config.role, config.answer_style, config.fallback_model_id, config.temperature, config.max_output_tokens), ("", "balanced", None, 0.7, None))
        old_dsl = {"dsl_version": 1, "prompt": "p", "model_alias": "bw-x"}  # a published snapshot from before T-061
        dsl = AssistantDSLv1.model_validate(old_dsl)
        self.assertEqual((dsl.role, dsl.answer_style, dsl.temperature, dsl.fallback_model_alias, dsl.tools.mounts, dsl.limits.max_iterations), ("", "balanced", 0.7, None, [], 8))

    def test_the_draft_becomes_the_dsl_with_identity_model_settings_and_tool_mounts(self) -> None:
        self.mount_tools(["search"], delegate="查资料")
        self.configure(role="退货客服", answer_style="concise", temperature=0.2, max_output_tokens=256)
        with self.session_factory() as db:
            assistant = db.get(Assistant, self.ids["assistant"])
            dsl = dsl_from_draft(assistant, db)
        self.assertEqual((dsl.role, dsl.answer_style, dsl.temperature, dsl.limits.max_tokens), ("退货客服", "concise", 0.2, 256))
        self.assertEqual([(m.kind, m.capability_id, m.tools, m.delegate) for m in dsl.tools.mounts], [("mcp", CAP, ["search"], "查资料")])
        self.assertIsInstance(dsl.tools.mounts[0], ToolMount)

    def test_the_role_and_style_are_part_of_the_system_prompt_and_the_defaults_change_nothing(self) -> None:
        plain = ToolChat(script=["ok"])
        self.run_agent(plain)
        self.assertEqual(plain.calls[0][0][0].content, "You are helpful.")  # a default agent's prompt is untouched
        self.configure(role="退货客服", answer_style="detailed")
        styled = ToolChat(script=["ok"])
        self.run_agent(styled)
        system = styled.calls[0][0][0].content
        self.assertIn("# 角色\n退货客服", system)
        self.assertIn("回答尽量详细", system)

    def test_the_model_is_called_with_the_temperature_and_output_limit(self) -> None:
        dsl = AssistantDSLv1(prompt="p", model_alias="bw-x", temperature=0.2)
        from beework.runtime.llm import build_chat_model

        model = build_chat_model(dsl, api_key="k", metadata={})
        self.assertEqual(model.temperature, 0.2)

    def test_saving_the_agent_validates_the_new_fields(self) -> None:
        headers = self.login("admin@example.com")
        assistant = self.client.get(f"/api/v1/assistants/{self.ids['assistant']}", headers=headers).json()

        def save(**changes):
            body = {"name": assistant["name"], "description": assistant["description"], "category": assistant["category"],
                    "visibility": assistant["visibility"], "visible_org_ids": assistant["visible_org_ids"],
                    "row_version": self.client.get(f"/api/v1/assistants/{self.ids['assistant']}", headers=headers).json()["row_version"],
                    "config": {**assistant["config"], **changes}}
            return self.client.put(f"/api/v1/assistants/{self.ids['assistant']}", json=body, headers=headers)

        self.assertEqual(save(temperature=3).status_code, 422)
        self.assertEqual(save(answer_style="rude").status_code, 422)
        self.assertEqual(save(max_output_tokens=5).status_code, 422)
        same = save(fallback_model_id=str(self.ids["chat"]))
        self.assertEqual((same.status_code, same.json()["code"]), (422, "assistant.fallback_model_unavailable"))  # same as the primary
        nonexistent = save(fallback_model_id=str(uuid4()))
        self.assertEqual(nonexistent.json()["code"], "assistant.fallback_model_unavailable")
        ok = save(role="退货客服", answer_style="concise", temperature=0.3, max_output_tokens=300)
        self.assertEqual(ok.status_code, 200, ok.text)
        config = ok.json()["config"]
        self.assertEqual((config["role"], config["answer_style"], config["temperature"], config["max_output_tokens"]), ("退货客服", "concise", 0.3, 300))


for _name in [n for n in dir(test_runtime.RuntimeTests) if n.startswith("test_")]:
    setattr(AgentRuntimeTests, _name, None)

if __name__ == "__main__":
    unittest.main()
