"""What a run offers the model as tools, and how a tool call is carried out (T-061, 演进 PRD 6.4–6.6, 7.6, 15.3).

The tools are the ones the agent mounted *and ticked*: for every MCP / Skill mount of the DSL, whatever the tenant's
current rows still expose, narrowed to the mount's ticked tools. A call goes through the controlled executor (T-071), which
re-checks that the tool exists, is available and is in the allowed set, validates its arguments, scrubs credentials and
passes the result through the pipeline's output guard before the model sees it. Nothing is executed here directly.
"""

import re
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from beework.runtime.dsl import AssistantDSLv1, ToolMount
from beework.tools.executor import (
    ToolConfirmationRequired,
    ToolContext,
    ToolResult,
    ToolSpec,
    invoke_tool,
)

ALIAS_MAX = 64  # OpenAI function names: [a-zA-Z0-9_-], at most 64 characters


@dataclass(frozen=True)
class OfferedTool:
    alias: str  # what the model calls
    spec: ToolSpec
    description: str


def _alias(index: int, spec: ToolSpec) -> str:
    tool = spec.name.split(":", 2)[2] if spec.source == "mcp" and spec.name.count(":") >= 2 else spec.source
    return f"{spec.source}_{index}_{re.sub(r'[^a-zA-Z0-9_-]', '_', tool)}"[:ALIAS_MAX]


class ToolRuntime:
    def __init__(
        self, bind: Any, dsl: AssistantDSLv1, *, tenant_id: UUID, run_id: UUID | None, assistant_id: UUID | None,
        end_user: str | None = None, guard_output: Callable[[str], str] = lambda text: text,
        span: Callable[[str], AbstractContextManager] = lambda name: nullcontext(),
        depth: int = 1, allow_agents: bool = True, environment: str = "production",
        check_stop: Callable[[], None] = lambda: None, clock: Any = None,
    ) -> None:
        self.bind, self.dsl, self.tenant_id = bind, dsl, tenant_id
        self._guard, self._span, self._allow_agents, self._clock = guard_output, span, allow_agents, clock
        self.offered: list[OfferedTool] = self._resolve()
        self._by_alias = {tool.alias: tool for tool in self.offered}
        self.ctx = ToolContext(
            tenant_id=tenant_id, allowed=frozenset(tool.spec.name for tool in self.offered), run_id=run_id,
            assistant_id=assistant_id, end_user=end_user, depth=depth, guard_output=guard_output, span=span,
            environment=environment, check_stop=check_stop,
        )

    # -- what is offered ------------------------------------------------------------------------------

    def _specs_of(self, side: Session, mount: ToolMount) -> list[ToolSpec]:
        from beework.capability_service import mcp_tool_specs
        from beework.skill_service import skill_tool_specs

        if mount.kind == "agent":
            from beework.agent_teams import agent_tool_specs

            return agent_tool_specs(side, self.tenant_id, [mount.capability_id]) if self._allow_agents else []
        if mount.kind == "mcp":
            specs = mcp_tool_specs(side, self.tenant_id, [mount.capability_id])
            return [s for s in specs if not mount.tools or s.name.split(":", 2)[2] in mount.tools]  # ticked for this agent
        return skill_tool_specs(side, self.tenant_id, [mount.capability_id])

    def _resolve(self) -> list[OfferedTool]:
        offered: list[OfferedTool] = []
        if not self.dsl.tools.mounts:
            return offered
        with Session(bind=self.bind) as side:
            for mount in self.dsl.tools.mounts:
                for spec in self._specs_of(side, mount):
                    description = f"{mount.delegate.strip()}\n{spec.description}".strip() if mount.delegate.strip() else spec.description
                    offered.append(OfferedTool(alias=_alias(len(offered) + 1, spec), spec=spec, description=description[:1000]))
        return offered

    def schemas(self) -> list[dict[str, Any]]:
        """OpenAI-style function definitions."""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.alias, "description": tool.description,
                    "parameters": tool.spec.parameters if tool.spec.parameters.get("type") == "object" else {"type": "object", "properties": {}},
                },
            }
            for tool in self.offered
        ]

    # -- a call ------------------------------------------------------------------------------------------

    def call(self, alias: str, args: dict[str, Any]) -> tuple[ToolResult, int]:
        """(result, duration in ms). An unknown alias — the model invented a tool — is refused, never guessed at."""
        started = time.monotonic()
        tool = self._by_alias.get(alias)
        if tool is None:
            return ToolResult(status="error", content="没有这个工具", code="tool.not_found"), 0
        try:
            with Session(bind=self.bind) as side, (self._clock.external() if self._clock is not None else nullcontext()):
                result = invoke_tool(side, self.ctx, tool.spec.name, args)
        except ToolConfirmationRequired:
            result = ToolResult(
                status="error", code="tool.confirmation_unsupported",
                content="该工具会修改数据，需要用户确认；当前对话暂不支持确认，未执行。请改用只读方式或告知用户在其他渠道操作。",
            )
        return result, int((time.monotonic() - started) * 1000)
