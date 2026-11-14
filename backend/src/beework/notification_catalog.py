"""The 12 events a notification rule can listen to, their texts in Chinese and English, and how bus events map onto them (T-088, SET-101, PRD 5.8).

A message states what happened and where to look — ids and counts, never the text of a conversation, a rule's pattern or a secret.
"""

from typing import Any

RULE_EVENTS = (
    "approval.requested", "approval.result", "guardrail.triggered", "handoff.requested", "handoff.timeout", "budget.threshold",
    "quota.threshold", "evaluation.completed", "channel.unhealthy", "mcp.unhealthy", "key.expiring", "security.event",
)
CHANNELS = ("email", "slack", "sms", "webhook")
FREQUENCIES = ("realtime", "hourly", "daily")

LABELS = {
    "approval.requested": ("发起审批", "Approval requested"), "approval.result": ("审批结果", "Approval result"),
    "guardrail.triggered": ("护栏触发", "Guardrail triggered"), "handoff.requested": ("转人工请求", "Handoff requested"),
    "handoff.timeout": ("转人工超时", "Handoff timed out"), "budget.threshold": ("预算阈值", "Budget threshold"),
    "quota.threshold": ("配额阈值", "Quota threshold"), "evaluation.completed": ("评测完成", "Evaluation completed"),
    "channel.unhealthy": ("渠道异常", "Channel unhealthy"), "mcp.unhealthy": ("MCP 连接异常", "MCP connection unhealthy"),
    "key.expiring": ("密钥即将过期", "Key expiring soon"), "security.event": ("安全事件", "Security event"),
}

APPROVAL_RESULTS = {"approved", "rejected", "withdrawn"}
APPROVAL_STATES = {"submitted": ("已提交", "submitted"), "level_approved": ("上一级已通过，待你处理", "approved at the previous level"),
                   "approved": ("已通过", "approved"), "rejected": ("已驳回", "rejected"), "withdrawn": ("已撤回", "withdrawn")}
SECURITY_KINDS = {"login_locked": ("有账号因登录失败次数过多被锁定", "An account was locked after too many failed logins")}


def rule_events_of(event_type: str, payload: dict[str, Any]) -> list[str]:
    """Which notification events a bus event is. One bus event can be at most one of them."""
    if event_type == "approval.updated":
        state = payload.get("state")
        return ["approval.result"] if state in APPROVAL_RESULTS else ["approval.requested"]
    if event_type == "usage.threshold":
        return ["budget.threshold"]
    return [event_type] if event_type in RULE_EVENTS else []


def interested_event_types(rule_event: str) -> set[str]:
    """The bus event types that can produce a rule event (used to decide cheaply whether anyone is listening)."""
    return {"approval.requested": {"approval.updated"}, "approval.result": {"approval.updated"}, "budget.threshold": {"usage.threshold"}}.get(rule_event, {rule_event})


def render(rule_event: str, payload: dict[str, Any], locale: str = "zh-CN") -> tuple[str, str, str]:
    """``(title, body, link)`` of one event."""
    zh = not locale.startswith("en")
    pick = lambda pair: pair[0] if zh else pair[1]  # noqa: E731
    label = pick(LABELS[rule_event])
    link = {"approval.requested": "/approvals", "approval.result": "/approvals", "budget.threshold": "/settings/usage", "quota.threshold": "/settings/usage",
            "channel.unhealthy": "/channels", "mcp.unhealthy": "/capabilities", "key.expiring": "/settings/api", "handoff.requested": "/handoff",
            "handoff.timeout": "/handoff", "evaluation.completed": "/evaluations", "guardrail.triggered": "/settings/rules", "security.event": "/audit"}.get(rule_event, "/")
    if rule_event in ("approval.requested", "approval.result"):
        state = pick(APPROVAL_STATES.get(str(payload.get("state")), ("有变化", "changed")))
        body = f"审批单{state}。" if zh else f"An approval request was {state}."
    elif rule_event == "budget.threshold":
        body = f"本月点数用量已达预算的 {payload.get('threshold')}%（{payload.get('used')} / {payload.get('budget')} 点）。" if zh else f"Points used this month reached {payload.get('threshold')}% of the budget ({payload.get('used')} / {payload.get('budget')})."
    elif rule_event == "quota.threshold":
        body = f"配额“{payload.get('quota')}”已用 {payload.get('percent')}%。" if zh else f"Quota “{payload.get('quota')}” is {payload.get('percent')}% used."
    elif rule_event == "channel.unhealthy":
        body = f"渠道“{payload.get('name')}”连续健康检查失败，已标为异常。" if zh else f"Channel “{payload.get('name')}” failed its health checks and is marked abnormal."
    elif rule_event == "mcp.unhealthy":
        body = f"MCP 连接“{payload.get('name')}”连续检查失败。" if zh else f"MCP connection “{payload.get('name')}” keeps failing its health checks."
    elif rule_event == "key.expiring":
        body = f"API 密钥“{payload.get('name')}”将在 {payload.get('days')} 天内过期。" if zh else f"API key “{payload.get('name')}” expires in {payload.get('days')} days."
    elif rule_event == "security.event":
        body = pick(SECURITY_KINDS.get(str(payload.get("kind")), ("发生了安全事件", "A security event happened")))
    elif rule_event == "guardrail.triggered":
        body = f"护栏规则触发（动作：{payload.get('action')}）。" if zh else f"A guardrail rule was triggered (action: {payload.get('action')})."
    elif rule_event in ("handoff.requested", "handoff.timeout"):
        body = "有新的转人工请求。" if rule_event == "handoff.requested" and zh else "A new handoff request is waiting." if rule_event == "handoff.requested" else ("转人工请求等待超时。" if zh else "A handoff request has waited too long.")
    else:  # evaluation.completed
        body = "一次评测已完成。" if zh else "An evaluation has finished."
    return f"BeeWork · {label}", body, link
