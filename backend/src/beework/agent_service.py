"""Agent / Chatflow / Workflow domain rules (T-060): config parsing, presets, configuration checks, references,
cycle detection and the mount resolvers registered with ``capabilities``.
"""

import re
from collections.abc import Callable
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.capabilities import MOUNT_RESOLVERS, MountItem, MountResolver
from beework.knowledge_mounts import resolve_kb_mounts
from beework.models.agents import Agent
from beework.models.assistants import Assistant
from beework.schemas.agents import (
    AgentConfigV1,
    AgentResources,
    ChatSettings,
    CheckIssue,
    FlowConfigV1,
    FlowNode,
    TeamSettings,
    WorkflowTriggers,
)

FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
MAX_FIELDS = 40
REFERENCE_NODE_TYPES = ("agent", "copilot")


class ConfigCorrupted(Exception):
    """The stored configuration cannot be parsed; the original data must stay untouched (AGT-007, GLB-084)."""


def config_model(kind: str) -> type[BaseModel]:
    return AgentConfigV1 if kind == "agent" else FlowConfigV1


def parse_config(kind: str, raw: dict | None) -> AgentConfigV1 | FlowConfigV1:
    try:
        return config_model(kind).model_validate(raw or {})  # type: ignore[return-value]
    except ValidationError as exc:
        raise ConfigCorrupted(str(exc)) from exc


def preset_config(kind: str, is_team: bool, model_id: UUID | None) -> dict[str, Any]:
    """Initial draft config (3.3.2). Pre-set flow nodes may be deleted by the user."""
    if kind == "agent":
        config = AgentConfigV1(model_id=model_id)
        if is_team:
            config.team = TeamSettings()
        return config.model_dump(mode="json")
    if kind == "chatflow":
        nodes = [
            FlowNode(id="n1", type="classify", name="意图识别", instruction="将用户消息分类为：产品咨询 / 售后服务 / 其他", order=1),
            FlowNode(id="n2", type="agent", name="答复 Agent", order=2),
            FlowNode(id="n3", type="note", name="注释", instruction="信息不足时追问用户（interrupt）", order=3),
        ]
        return FlowConfigV1(nodes=nodes, chat_settings=ChatSettings()).model_dump(mode="json")
    nodes = [
        FlowNode(id="n1", type="extract", name="提取订单信息", instruction="从输入中提取 order_id、金额与原因", order=1),
        FlowNode(id="n2", type="agent", name="审核 Agent", order=2),
        FlowNode(id="n3", type="review", name="人工复核", instruction="金额大于 5,000 时需人工审批", order=3),
    ]
    return FlowConfigV1(nodes=nodes, triggers=WorkflowTriggers()).model_dump(mode="json")


# ---------------------------------------------------------------------------
# References.
# ---------------------------------------------------------------------------


def agent_refs(config: AgentConfigV1 | FlowConfigV1) -> list[UUID]:
    """Other agents / workflows this configuration connects to."""
    if isinstance(config, AgentConfigV1):
        return list(config.resources.agents)
    return [node.target_id for node in config.nodes if node.type in REFERENCE_NODE_TYPES and node.target_id]


def raw_agent_refs(kind: str, raw: dict | None) -> list[UUID]:
    if not raw:
        return []
    try:
        return agent_refs(parse_config(kind, raw))
    except ConfigCorrupted:
        return []


def _live_agents(db: Session, tenant_id: UUID, ids: list[UUID]) -> dict[UUID, Agent]:
    if not ids:
        return {}
    return {
        agent.id: agent
        for agent in db.scalars(
            select(Agent).where(Agent.tenant_id == tenant_id, Agent.id.in_(ids), Agent.deleted_at.is_(None))
        )
    }


def _resolver(kind: str, mount_kind: str) -> MountResolver:
    def resolve(db: Session, tenant_id: UUID, ids: list[UUID]) -> dict[UUID, MountItem]:
        """CAP-051: 可用 → available; 草稿 / 已停用 → disabled (the referencing object shows it red). A deleted
        agent, or one of another kind, is not returned and is therefore reported as deleted."""
        return {
            agent.id: MountItem(
                id=agent.id,
                kind=mount_kind,
                name=agent.name,
                state="available" if agent.status == "available" else "disabled",
                meta={"status": agent.status, "kind": agent.kind, "is_team": agent.is_team},
            )
            for agent in _live_agents(db, tenant_id, ids).values()
            if agent.kind == kind
        }

    return resolve


MOUNT_RESOLVERS["agent"] = _resolver("agent", "agent")
MOUNT_RESOLVERS["chatflow"] = _resolver("chatflow", "chatflow")
MOUNT_RESOLVERS["flow"] = _resolver("workflow", "flow")


def find_references(db: Session, agent: Agent) -> list[tuple[str, UUID, str]]:
    """Assistants (draft mounts) and other agents (draft or available config) that point at ``agent``."""
    target = str(agent.id)
    found: list[tuple[str, UUID, str]] = []
    mount_kind = {"agent": "agent", "chatflow": "chatflow", "workflow": "flow"}[agent.kind]
    for assistant in db.scalars(
        select(Assistant).where(Assistant.tenant_id == agent.tenant_id, Assistant.deleted_at.is_(None))
    ):
        mounted = ((assistant.draft_config or {}).get("mounts") or {}).get(mount_kind) or []
        if target in {str(value) for value in mounted}:
            found.append(("assistant", assistant.id, assistant.name))
    for other in db.scalars(
        select(Agent).where(Agent.tenant_id == agent.tenant_id, Agent.deleted_at.is_(None), Agent.id != agent.id)
    ):
        refs = raw_agent_refs(other.kind, other.draft_config) + raw_agent_refs(other.kind, other.available_config)
        if agent.id in refs:
            found.append((other.kind, other.id, other.name))
    return found


class ReferenceCycle(Exception):
    def __init__(self, self_reference: bool) -> None:
        self.self_reference = self_reference


def ensure_no_cycle(db: Session, agent: Agent, config: AgentConfigV1 | FlowConfigV1) -> None:
    """RUL-014: an agent cannot connect to itself, and connections between agents must not form a loop."""
    new_refs = agent_refs(config)
    if agent.id in new_refs:
        raise ReferenceCycle(True)
    graph: dict[UUID, set[UUID]] = {}
    for other in db.scalars(
        select(Agent).where(Agent.tenant_id == agent.tenant_id, Agent.deleted_at.is_(None), Agent.id != agent.id)
    ):
        graph[other.id] = set(raw_agent_refs(other.kind, other.draft_config)) | set(
            raw_agent_refs(other.kind, other.available_config)
        )
    # The saved available config also counts for the agent being edited: callers keep executing it.
    graph[agent.id] = set(new_refs) | set(raw_agent_refs(agent.kind, agent.available_config))
    seen: set[UUID] = set()
    stack = list(new_refs)
    while stack:
        current = stack.pop()
        if current == agent.id:
            raise ReferenceCycle(False)
        if current in seen:
            continue
        seen.add(current)
        stack.extend(graph.get(current, ()))


# ---------------------------------------------------------------------------
# Configuration check (AGT-028).
# ---------------------------------------------------------------------------

# T-062 registers node completeness rules (AGT-045): fn(node) -> message when the node still needs setting up.
NodeRule = Callable[[FlowNode], str | None]
NODE_RULES: list[NodeRule] = []


def _resource_issues(db: Session, agent: Agent, config: AgentConfigV1 | FlowConfigV1) -> list[CheckIssue]:
    states: list[str] = []
    if isinstance(config, AgentConfigV1):
        resources: AgentResources = config.resources
        # Channels, skills and tools get their catalogue in T-070/T-072/T-074; until then they cannot resolve.
        states += ["deleted" for _ in [*resources.channels, *resources.skills, *resources.tools]]
        found = resolve_kb_mounts(db, agent.tenant_id, list(resources.knowledge))
        states += [found[i].state if i in found else "deleted" for i in resources.knowledge]
        agents = _live_agents(db, agent.tenant_id, list(resources.agents))
        states += ["available" if i in agents and agents[i].status == "available" else "deleted" for i in resources.agents]
    else:
        ids = [n.target_id for n in config.nodes if n.type in REFERENCE_NODE_TYPES and n.target_id]
        agents = _live_agents(db, agent.tenant_id, ids)
        states += ["available" if i in agents and agents[i].status == "available" else "deleted" for i in ids]
    if any(state != "available" for state in states):
        return [CheckIssue(code="resource_invalid", message="请处理失效的资源引用")]
    return []


def check_agent(db: Session, agent: Agent, config: AgentConfigV1 | FlowConfigV1) -> list[CheckIssue]:
    issues: list[CheckIssue] = []

    def add(code: str, message: str) -> None:
        issues.append(CheckIssue(code=code, message=message))

    if not agent.name.strip():
        add("name_required", "请填写名称")
    if isinstance(config, AgentConfigV1):
        if not config.instructions.strip():
            add("instructions_required", "请填写指令")
        if agent.is_team:
            members = config.resources.agents
            if len(members) < 2:
                add("team_members", "协作组至少连接 2 个智能体")
            team = config.team
            if team is not None and team.lead_agent_id is not None and team.lead_agent_id not in members:
                add("team_lead", "主控智能体需在已连接的智能体中")
    else:
        executable = [node for node in config.nodes if node.type != "note"]
        if not executable:
            add("nodes_required", "请添加执行节点")
        for node in executable:
            for rule in NODE_RULES:
                message = rule(node)
                if message:
                    add("node_incomplete", f"{node.name or node.type} · {message}")
        if agent.kind == "chatflow":
            settings = config.chat_settings or ChatSettings()
            if not settings.opening.strip():
                add("opening_required", "请填写开场白")
            if not settings.fallback.strip():
                add("fallback_required", "请填写兜底回复")
            if not 1 <= settings.history_turns <= 50:
                add("history_turns", "历史轮数需在 1–50 之间")
        else:
            _workflow_issues(config, add)
    issues += _resource_issues(db, agent, config)
    return issues


def _workflow_issues(config: FlowConfigV1, add: Callable[[str, str], None]) -> None:
    names_in = [f.name for f in config.inputs]
    names_out = [f.name for f in config.outputs]
    valid = all(FIELD_NAME.match(n) for n in names_in + names_out)
    unique = len(set(names_in)) == len(names_in) and len(set(names_out)) == len(names_out)
    if not valid or not unique:
        add("field_names", "请修正输入输出字段名")
    if not config.outputs:
        add("outputs_required", "请至少定义一个输出字段")
    triggers = config.triggers or WorkflowTriggers()
    if not (triggers.api.enabled or triggers.schedule.enabled or triggers.tool.enabled):
        add("trigger_required", "请至少开启一种触发方式")
    if triggers.schedule.enabled and len(triggers.schedule.cron.split()) != 5:
        add("cron_invalid", "定时触发需填写 5 段 cron 表达式")
    if triggers.tool.enabled and not FIELD_NAME.match(triggers.tool.name):
        add("tool_name", "工具名需为小写下划线格式")
    if len(config.inputs) + len(config.outputs) > MAX_FIELDS:
        add("too_many_fields", "字段过多")
