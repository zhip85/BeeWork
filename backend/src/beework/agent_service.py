"""Flow studio domain rules (T-060): config parsing, presets, configuration checks, references, cycle detection and
versioned publishing of Chatflows / Workflows.

“智能体” is the ``assistants`` table (T-067); the ``agents`` table holds flows only.
"""

import re
from collections.abc import Callable
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework.models import utc_now
from beework.models.agents import Agent, FlowVersion
from beework.models.assistants import Assistant
from beework.schemas.agents import (
    ChatSettings,
    CheckIssue,
    FlowConfigV1,
    FlowNode,
    WorkflowTriggers,
)

FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
MAX_FIELDS = 40
REFERENCE_NODE_TYPES = ("agent", "copilot", "connector")


class ConfigCorrupted(Exception):
    """The stored configuration cannot be parsed; the original data must stay untouched (AGT-007, GLB-084)."""


def parse_config(raw: dict | None) -> FlowConfigV1:
    try:
        return FlowConfigV1.model_validate(raw or {})
    except ValidationError as exc:
        raise ConfigCorrupted(str(exc)) from exc


def preset_config(kind: str) -> dict:
    """Initial draft config (3.3.2). Pre-set flow nodes may be deleted by the user."""
    if kind == "chatflow":
        nodes = [
            FlowNode(id="n1", type="classify", name="意图识别", instruction="将用户消息分类为：产品咨询 / 售后服务 / 其他", order=1),
            FlowNode(id="n2", type="agent", name="答复智能体", order=2),
            FlowNode(id="n3", type="note", name="注释", instruction="信息不足时追问用户（interrupt）", order=3),
        ]
        return FlowConfigV1(nodes=nodes, chat_settings=ChatSettings()).model_dump(mode="json")
    nodes = [
        FlowNode(id="n1", type="extract", name="提取订单信息", instruction="从输入中提取 order_id、金额与原因", order=1),
        FlowNode(id="n2", type="agent", name="审核智能体", order=2),
        FlowNode(id="n3", type="review", name="人工复核", instruction="金额大于 5,000 时需人工审批", order=3),
    ]
    return FlowConfigV1(nodes=nodes, triggers=WorkflowTriggers()).model_dump(mode="json")


# ---------------------------------------------------------------------------
# References.
# ---------------------------------------------------------------------------


def flow_refs(config: FlowConfigV1) -> list[UUID]:
    """Other flows this flow's nodes call (cycle detection only follows these)."""
    return [
        node.target_id
        for node in config.nodes
        if node.type in ("agent", "copilot") and node.target_kind == "flow" and node.target_id
    ]


def raw_flow_refs(raw: dict | None) -> list[UUID]:
    if not raw:
        return []
    try:
        return flow_refs(parse_config(raw))
    except ConfigCorrupted:
        return []


def _live_flows(db: Session, tenant_id: UUID, ids: list[UUID]) -> dict[UUID, Agent]:
    if not ids:
        return {}
    return {
        flow.id: flow
        for flow in db.scalars(
            select(Agent).where(Agent.tenant_id == tenant_id, Agent.id.in_(ids), Agent.deleted_at.is_(None))
        )
    }


def _published_assistants(db: Session, tenant_id: UUID, ids: list[UUID]) -> set[UUID]:
    if not ids:
        return set()
    return set(
        db.scalars(
            select(Assistant.id).where(
                Assistant.tenant_id == tenant_id,
                Assistant.id.in_(ids),
                Assistant.deleted_at.is_(None),
                Assistant.status == "published",
            )
        )
    )


# Other modules register who points at a flow (flow connections T-075, portal apps T-140): fn(db, flow) -> [(kind, id, name)].
ReferenceProvider = Callable[[Session, Agent], list[tuple[str, UUID, str]]]
REFERENCE_PROVIDERS: list[ReferenceProvider] = []


def find_references(db: Session, flow: Agent) -> list[tuple[str, UUID, str]]:
    """Other flows (draft or published config) and registered providers that point at ``flow``."""
    found: list[tuple[str, UUID, str]] = []
    for other in db.scalars(
        select(Agent).where(Agent.tenant_id == flow.tenant_id, Agent.deleted_at.is_(None), Agent.id != flow.id)
    ):
        refs = raw_flow_refs(other.draft_config) + raw_flow_refs(other.published_config)
        if flow.id in refs:
            found.append((other.kind, other.id, other.name))
    for provider in REFERENCE_PROVIDERS:
        found.extend(provider(db, flow))
    return found


class ReferenceCycle(Exception):
    def __init__(self, self_reference: bool) -> None:
        self.self_reference = self_reference


def ensure_no_cycle(db: Session, flow: Agent, config: FlowConfigV1) -> None:
    """RUL-014: a flow cannot call itself, and calls between flows must not form a loop."""
    new_refs = flow_refs(config)
    if flow.id in new_refs:
        raise ReferenceCycle(True)
    graph: dict[UUID, set[UUID]] = {}
    for other in db.scalars(
        select(Agent).where(Agent.tenant_id == flow.tenant_id, Agent.deleted_at.is_(None), Agent.id != flow.id)
    ):
        graph[other.id] = set(raw_flow_refs(other.draft_config)) | set(raw_flow_refs(other.published_config))
    # The published config also counts for the flow being edited: callers keep executing it.
    graph[flow.id] = set(new_refs) | set(raw_flow_refs(flow.published_config))
    seen: set[UUID] = set()
    stack = list(new_refs)
    while stack:
        current = stack.pop()
        if current == flow.id:
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


def _node_target_ok(
    node: FlowNode, flows: dict[UUID, Agent], assistants: set[UUID]
) -> bool:
    """演进 PRD 6.5: a node whose target is gone, unpublished or disabled is flagged, never skipped silently."""
    if node.target_id is None:
        return True
    if node.target_kind == "assistant":
        return node.target_id in assistants
    if node.target_kind == "flow":
        target = flows.get(node.target_id)
        return target is not None and target.status == "published"
    return False  # connections (T-075) and unknown kinds cannot resolve yet


def invalid_node_ids(db: Session, flow: Agent, config: FlowConfigV1) -> list[str]:
    nodes = [n for n in config.nodes if n.type in REFERENCE_NODE_TYPES and n.target_id]
    flows = _live_flows(db, flow.tenant_id, [n.target_id for n in nodes if n.target_kind == "flow"])
    assistants = _published_assistants(db, flow.tenant_id, [n.target_id for n in nodes if n.target_kind == "assistant"])
    return [n.id for n in nodes if not _node_target_ok(n, flows, assistants)]


def check_flow(db: Session, flow: Agent, config: FlowConfigV1) -> list[CheckIssue]:
    issues: list[CheckIssue] = []

    def add(code: str, message: str) -> None:
        issues.append(CheckIssue(code=code, message=message))

    if not flow.name.strip():
        add("name_required", "请填写名称")
    executable = [node for node in config.nodes if node.type != "note"]
    if not executable:
        add("nodes_required", "请添加执行节点")
    for node in executable:
        for rule in NODE_RULES:
            message = rule(node)
            if message:
                add("node_incomplete", f"{node.name or node.type} · {message}")
    if flow.kind == "chatflow":
        settings = config.chat_settings or ChatSettings()
        if not settings.opening.strip():
            add("opening_required", "请填写开场白")
        if not settings.fallback.strip():
            add("fallback_required", "请填写兜底回复")
        if not 1 <= settings.history_turns <= 50:
            add("history_turns", "历史轮数需在 1–50 之间")
    else:
        _workflow_issues(config, add)
    if invalid_node_ids(db, flow, config):
        add("resource_invalid", "请处理失效的资源引用")
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


# ---------------------------------------------------------------------------
# Versioned publishing (演进 PRD 6.8).
# ---------------------------------------------------------------------------


def publish_flow(db: Session, flow: Agent, config: FlowConfigV1, note: str, actor: str) -> FlowVersion:
    """Freeze the draft as the next immutable version and make it the one callers execute."""
    last = db.scalar(select(func.max(FlowVersion.version_seq)).where(FlowVersion.flow_id == flow.id)) or 0
    snapshot = config.model_dump(mode="json")
    version = FlowVersion(
        tenant_id=flow.tenant_id,
        flow_id=flow.id,
        version_seq=last + 1,
        version=f"v{last + 1}",
        snapshot=snapshot,
        change_note=note.strip(),
        published_by=actor,
    )
    db.add(version)
    db.flush()
    flow.published_config = snapshot
    flow.live_version_id = version.id
    flow.status = "published"
    flow.published_at = utc_now()
    return version
