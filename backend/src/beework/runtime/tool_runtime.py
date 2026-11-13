"""What a run offers the model as tools, and how a tool call is carried out (T-061, 演进 PRD 6.4–6.6, 7.6, 15.3).

The tools are the ones the agent mounted *and ticked*: for every MCP / Skill mount of the DSL, whatever the tenant's
current rows still expose, narrowed to the mount's ticked tools. A call goes through the controlled executor (T-071), which
re-checks that the tool exists, is available and is in the allowed set, validates its arguments, scrubs credentials and
passes the result through the pipeline's output guard before the model sees it. Nothing is executed here directly.
"""

import json
import re
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.runtime.dsl import AssistantDSLv1
from beework.tools.executor import (
    ToolConfirmationRequired,
    ToolContext,
    ToolResult,
    ToolSpec,
    invoke_tool,
)

ALIAS_MAX = 64  # OpenAI function names: [a-zA-Z0-9_-], at most 64 characters
DISCLOSURE_LIMIT = 30  # more offered capabilities than this: progressive disclosure (T-162)
INITIAL_DISCLOSED = 5  # how many of L2's candidates are offered up front then
FIND_CAPABILITY = "find_capability"
FIND_RESULTS = 8


@dataclass(frozen=True)
class OfferedTool:
    alias: str  # what the model calls
    spec: ToolSpec
    description: str
    entry_id: str | None = None  # the capability registry entry the tool comes from (T-162)


def _alias(index: int, spec: ToolSpec) -> str:
    tool = spec.name.split(":", 2)[2] if spec.source in ("mcp", "api") and spec.name.count(":") >= 2 else spec.source
    return f"{spec.source}_{index}_{re.sub(r'[^a-zA-Z0-9_-]', '_', tool)}"[:ALIAS_MAX]


class ToolRuntime:
    def __init__(
        self, bind: Any, dsl: AssistantDSLv1, *, tenant_id: UUID, run_id: UUID | None, assistant_id: UUID | None,
        end_user: str | None = None, guard_output: Callable[[str], str] = lambda text: text,
        span: Callable[[str], AbstractContextManager] = lambda name: nullcontext(),
        depth: int = 1, allow_agents: bool = True, environment: str = "production",
        check_stop: Callable[[], None] = lambda: None, clock: Any = None, max_calls: int = 20,
    ) -> None:
        self.bind, self.dsl, self.tenant_id = bind, dsl, tenant_id
        self._guard, self._span, self._allow_agents, self._clock = guard_output, span, allow_agents, clock
        self._config: dict[str, Any] = {}
        self.offered: list[OfferedTool] = self._resolve()
        self._by_alias = {tool.alias: tool for tool in self.offered}
        self._unlocked: set[str] = set()
        self._entry_names = self._names()
        self.max_calls, self._calls, self._seen_calls = max_calls, 0, set()
        self.ctx = ToolContext(
            tenant_id=tenant_id, allowed=frozenset(tool.spec.name for tool in self.offered), run_id=run_id,
            assistant_id=assistant_id, end_user=end_user, depth=depth, guard_output=guard_output, span=span,
            environment=environment, check_stop=check_stop,
        )

    # -- what is offered ------------------------------------------------------------------------------

    def _resolve(self) -> list[OfferedTool]:
        """The tools are the registry entries in the agent's scope: mounted and ticked, available, sub-agents only while collaboration
        is on. Knowledge bases are retrieved by the pipeline, not called as tools."""
        from beework import capability_registry as registry
        from beework.intent_service import config_from_dsl, tool_name_for

        offered: list[OfferedTool] = []
        if not self.dsl.tools.mounts:
            return offered
        delegates = {str(m.capability_id): m.delegate.strip() for m in self.dsl.tools.mounts}
        with Session(bind=self.bind) as side:
            self._config = config_from_dsl(self.dsl)
            seen = registry.visible_entries(side, self.tenant_id, self._config)
            for item in sorted(seen, key=lambda i: self._mount_order(i.entry)):
                entry = item.entry
                name = tool_name_for(entry)
                if not name or entry.kind == "kb" or (entry.kind == "agent" and not self._allow_agents):
                    continue
                source, _, _ = name.partition(":")
                spec = ToolSpec(name=name, source=source, capability_id=entry.source_id, parameters=entry.input_schema or {}, writes=entry.writes, description=entry.description)
                parts = [delegates.get(str(entry.source_id), ""), entry.intent_description.strip(), entry.description]
                description = "\n".join(part for part in parts if part).strip()
                offered.append(OfferedTool(alias=_alias(len(offered) + 1, spec), spec=spec, description=description[:1000], entry_id=str(entry.id)))
        return offered

    def _mount_order(self, entry: Any) -> tuple[int, str, str]:
        """The order of the agent's mounts, then the tool name: aliases stay stable between runs."""
        order = {str(m.capability_id): index for index, m in enumerate(self.dsl.tools.mounts)}
        return order.get(str(entry.source_id), 10**6), entry.name.lower(), str(entry.id)

    def entry_id(self, alias: str) -> str | None:
        tool = self._by_alias.get(alias)
        return tool.entry_id if tool else None

    # -- progressive disclosure ---------------------------------------------------------------------------

    @property
    def progressive(self) -> bool:
        return len(self.offered) > DISCLOSURE_LIMIT

    def disclose(self, names: list[str]) -> None:
        """Start a run with L2's candidates (by name) when there are too many tools to offer at once."""
        if not self.progressive:
            return
        wanted = {n.lower() for n in names}
        for tool in self.offered:
            if len(self._unlocked) >= INITIAL_DISCLOSED:
                break
            if self._entry_names.get(tool.alias, "").lower() in wanted:
                self._unlocked.add(tool.alias)

    def find(self, query: str) -> str:
        """The meta tool: recall inside the agent's scope and make what it finds callable from now on."""
        from beework import capability_registry as registry

        with Session(bind=self.bind) as side:
            found = registry.recall(side, self.tenant_id, self._config, query, k=FIND_RESULTS)
        by_entry = {tool.entry_id: tool for tool in self.offered}
        lines = []
        for item in found:
            tool = by_entry.get(str(item.entry.id))
            if tool is None:
                continue
            self._unlocked.add(tool.alias)
            lines.append(f"- {tool.alias}：{item.entry.name} — {(item.entry.intent_description or item.entry.description)[:120]}")
        return "已可调用：\n" + "\n".join(lines) if lines else "没有找到相关能力。"

    def _names(self) -> dict[str, str]:
        if not self.progressive:
            return {}
        from beework.models.capability_registry import CapabilityEntry

        with Session(bind=self.bind) as side:
            ids = [UUID(t.entry_id) for t in self.offered if t.entry_id]
            rows = {str(e.id): e.name for e in side.scalars(select(CapabilityEntry).where(CapabilityEntry.id.in_(ids)))}
        return {t.alias: rows.get(t.entry_id or "", "") for t in self.offered}

    def schemas(self) -> list[dict[str, Any]]:
        """OpenAI-style function definitions. With too many tools only the unlocked ones and ``find_capability`` are offered."""
        tools = [t for t in self.offered if t.alias in self._unlocked] if self.progressive else self.offered
        definitions = self._definitions(tools)
        if self.progressive:
            definitions.append({"type": "function", "function": {
                "name": FIND_CAPABILITY, "description": f"当前可用能力很多（{len(self.offered)} 个），只列出了一部分。用关键词查找需要的能力，找到后即可调用。",
                "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "要做的事，如“查订单”"}}, "required": ["query"]},
            }})
        return definitions

    def _definitions(self, tools: list[OfferedTool]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.alias, "description": tool.description,
                    "parameters": tool.spec.parameters if tool.spec.parameters.get("type") == "object" else {"type": "object", "properties": {}},
                },
            }
            for tool in tools
        ]

    # -- a call ------------------------------------------------------------------------------------------

    def call(self, alias: str, args: dict[str, Any]) -> tuple[ToolResult, int]:
        """(result, duration in ms). An unknown alias — the model invented a tool — is refused, never guessed at."""
        started = time.monotonic()
        if alias == FIND_CAPABILITY and self.progressive:
            return ToolResult(status="ok", content=self.find(str(args.get("query") or ""))), int((time.monotonic() - started) * 1000)
        tool = self._by_alias.get(alias)
        if tool is None or (self.progressive and alias not in self._unlocked):
            return ToolResult(status="error", content="没有这个工具", code="tool.not_found"), 0
        if self._calls >= self.max_calls:
            return ToolResult(status="error", content=f"本次运行的工具调用已达上限（{self.max_calls} 次），不能再调用工具，请直接回答。", code="tool.limit_reached"), 0
        key = (alias, json.dumps(args, sort_keys=True, ensure_ascii=False, default=str))
        if key in self._seen_calls:
            return ToolResult(status="error", content="这个工具已经用相同的参数调用过，结果在上文里，请直接使用。", code="tool.repeated"), 0
        self._seen_calls.add(key)
        self._calls += 1
        try:
            with Session(bind=self.bind) as side, (self._clock.external() if self._clock is not None else nullcontext()):
                result = invoke_tool(side, self.ctx, tool.spec.name, args)
        except ToolConfirmationRequired:
            result = ToolResult(
                status="error", code="tool.confirmation_unsupported",
                content="该工具会修改数据，需要用户确认；当前对话暂不支持确认，未执行。请改用只读方式或告知用户在其他渠道操作。",
            )
        return result, int((time.monotonic() - started) * 1000)
