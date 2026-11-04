"""Chat execution for external callers: the open API (T-028) and the website widget (T-040).

Both reach the same path: resolve what runs (RUL-003: production runs the published version, development and
staging the latest draft), admit the request (PRD 5.5 steps 1–2), record the end user, conversation and run, queue
the run for the Worker and either wait for it or relay its events.
"""

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.conversations import append_message, start_conversation
from beework.entitlements import check_quota, get_usage_counter, quota_limit
from beework.errors import ApiError
from beework.gateway_sync import environment_ready
from beework.models import Tenant, TenantModelEnvironment
from beework.models.assistants import Assistant
from beework.models.conversations import Conversation, EndUser
from beework.models.runtime import Run
from beework.ratelimit import limiter
from beework.runtime.dsl import AssistantDSLv1, dsl_from_draft
from beework.runtime.errors import RuntimeFailure
from beework.runtime.executor import SERVING_TENANT_STATUSES
from beework.tenant_access import ENVIRONMENTS, environments_for_plan
from beework.worker.events import publish_run_event, stream_run_events
from beework.worker.queue import enqueue

TERMINAL = frozenset({"done", "error", "cancelled"})
WAIT_MARGIN_SECONDS = 30


@dataclass
class ChatTarget:
    assistant: Assistant
    dsl: AssistantDSLv1
    version_id: UUID | None
    label: str
    environment: str


# ---------------------------------------------------------------------------
# Admission.
# ---------------------------------------------------------------------------


def enforce_qps(db: Session, tenant: Tenant) -> None:
    """RUL-021: every API request counts against the tenant's QPS quota; over it, 429 with ``Retry-After``."""
    qps = quota_limit(db, tenant.id, "api_qps")
    if not qps:
        return
    decision = limiter.check(f"tenant:{tenant.id}", per_second=qps)
    if not decision.allowed:
        raise ApiError(429, "rate_limited", "请求过于频繁，已超过套餐的 QPS 限额", {"Retry-After": str(decision.retry_after)})


def enforce_visitor_limit(ip: str | None, scope: str, *, per_second: float = 1.0, burst: float = 20) -> None:
    """Anonymous widget traffic is also limited per address, so one visitor cannot spend a tenant's whole quota."""
    decision = limiter.check(f"visitor:{scope}:{ip or 'unknown'}", per_second=per_second, burst=burst)
    if not decision.allowed:
        raise ApiError(429, "rate_limited", "请求过于频繁，请稍后再试", {"Retry-After": str(decision.retry_after)})


def resolve_target(db: Session, tenant: Tenant, assistant_id: UUID, environment: str, message: str) -> ChatTarget:
    if environment not in ENVIRONMENTS or environment not in environments_for_plan(tenant.plan):
        raise ApiError(403, "environment_unavailable", "当前套餐不提供该环境")
    if tenant.status not in SERVING_TENANT_STATUSES:
        raise ApiError(403, "tenant_unavailable", "企业工作区当前不可用")
    assistant = db.scalar(
        select(Assistant).where(
            Assistant.id == assistant_id, Assistant.tenant_id == tenant.id, Assistant.deleted_at.is_(None)
        )
    )
    if assistant is None:  # another tenant's assistant looks exactly like a missing one
        raise ApiError(404, "assistant_not_found", "未找到指定助理")
    if assistant.status == "disabled":
        raise ApiError(409, "assistant_disabled", "助理已停用")
    config = db.scalar(
        select(TenantModelEnvironment).where(
            TenantModelEnvironment.tenant_id == tenant.id, TenantModelEnvironment.environment == environment
        )
    )
    if not environment_ready(config):
        raise ApiError(503, "service_unavailable", "模型服务暂不可用，请稍后重试")
    from beework.assistant_versions import (
        live_version,  # lazy: the schemas package imports the API package
    )

    if environment == "production":
        version = live_version(db, assistant)
        if version is None:
            raise ApiError(409, "assistant_not_published", "助理还没有已发布的版本")
        dsl = AssistantDSLv1.model_validate(version.snapshot["dsl"])  # the frozen snapshot, never the draft
        model_id, version_id, label = version.snapshot.get("model_id"), version.id, f"v{version.version}"
    else:
        try:
            dsl = dsl_from_draft(assistant)
        except RuntimeFailure as exc:
            raise ApiError(409, "assistant_not_ready", exc.message) from exc
        model_id, version_id, label = assistant.draft_config.get("model_id"), None, f"draft:{assistant.draft_version}"
    from beework.api.assistants import _enabled_chat_models

    models, _ = _enabled_chat_models(db, tenant.id, environment)
    if str(model_id) not in {str(model.id) for model, _ in models}:
        raise ApiError(503, "service_unavailable", "助理所选模型当前不可用")
    if len(message.strip()) > dsl.limits.max_input_chars:
        raise ApiError(422, "invalid_request", f"message：长度不能超过 {dsl.limits.max_input_chars} 个字符")
    if get_usage_counter("monthly_points") is not None and not check_quota(db, tenant.id, "monthly_points", 0).allowed:
        raise ApiError(402, "quota_exceeded", "本月模型点数已用完")
    return ChatTarget(assistant, dsl, version_id, label, environment)


# ---------------------------------------------------------------------------
# Starting a run.
# ---------------------------------------------------------------------------


def _end_user(db: Session, tenant_id: UUID, channel_type: str, external_id: str, name: str | None) -> EndUser:
    statement = select(EndUser).where(
        EndUser.tenant_id == tenant_id, EndUser.channel_type == channel_type, EndUser.external_id == external_id
    )
    end_user = db.scalar(statement)
    if end_user is None:
        end_user = EndUser(tenant_id=tenant_id, channel_type=channel_type, external_id=external_id, name=name)
        try:
            with db.begin_nested():
                db.add(end_user)
                db.flush()
        except Exception:  # noqa: BLE001 - a concurrent request created the same identity first
            end_user = db.scalar(statement)
            if end_user is None:
                raise
    elif name and end_user.name != name:
        end_user.name = name
    return end_user


def start_chat(
    db: Session,
    target: ChatTarget,
    *,
    source: str,
    channel_type: str,
    channel_instance_id: UUID | None,
    external_id: str,
    end_user_name: str | None,
    message: str,
    conversation_id: UUID | None,
) -> tuple[Run, Conversation]:
    assistant = target.assistant
    end_user = _end_user(db, assistant.tenant_id, channel_type, external_id, end_user_name)
    if conversation_id is not None:
        conversation = db.scalar(
            select(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == assistant.tenant_id,
                Conversation.assistant_id == assistant.id,
                Conversation.end_user_id == end_user.id,
                Conversation.source == source,
                Conversation.environment == target.environment,  # a development key never touches production conversations
            )
            .with_for_update()
        )
        if conversation is None:
            raise ApiError(404, "conversation_not_found", "未找到指定会话")
        in_flight = db.scalar(
            select(Run.id).where(Run.conversation_id == conversation.id, Run.status.in_(("queued", "running"))).limit(1)
        )
        if in_flight is not None:
            raise ApiError(409, "conversation_busy", "上一条消息仍在处理中，请稍后再发送")
    else:
        conversation = start_conversation(
            db, assistant=assistant, end_user=end_user, source=source, environment=target.environment, created_by=None
        )
        conversation.assistant_version = target.label
        conversation.channel_instance_id = channel_instance_id
    run = Run(
        tenant_id=assistant.tenant_id,
        assistant_id=assistant.id,
        conversation_id=conversation.id,
        version_id=target.version_id,
        draft_row_version=None if target.version_id else assistant.row_version,
        environment=target.environment,
        source=source,
        status="queued",
        config=target.dsl.model_dump(mode="json"),
        input={"message": message},
        created_by=None,
    )
    db.add(run)
    db.flush()
    append_message(db, conversation, "user", message, run_id=run.id)
    enqueue(
        db, "runtime.execute", {"run_id": str(run.id)}, tenant_id=assistant.tenant_id,
        idempotency_key=f"runtime.execute:{run.id}",
    )
    # Commits the run, its job and the first ``queued`` event together.
    publish_run_event(db, run.id, "queued", {"assistant_id": str(assistant.id)}, tenant_id=assistant.tenant_id)
    return run, conversation


# ---------------------------------------------------------------------------
# Waiting for / relaying the result.
# ---------------------------------------------------------------------------


def _usage(data: dict) -> dict[str, int | None]:
    usage = data.get("usage") or {}
    return {"input_tokens": usage.get("input"), "output_tokens": usage.get("output")}


def wait_for_reply(bind, run_id: UUID, *, timeout_seconds: int) -> dict:
    """Block until the run ends and return the public reply; used by non-streaming requests."""
    deadline = time.monotonic() + timeout_seconds + WAIT_MARGIN_SECONDS
    text = ""
    with Session(bind=bind) as db:
        for event in stream_run_events(db, run_id, 0, heartbeat=True):
            if event is not None:
                if event["type"] == "token":
                    text += event["data"].get("text", "")
                elif event["type"] == "done":
                    return {"content": event["data"].get("text", text), "usage": _usage(event["data"])}
                elif event["type"] == "error":
                    raise ApiError(502, "run_failed", event["data"].get("message", "助理运行失败"))
                elif event["type"] == "cancelled":
                    raise ApiError(409, "run_cancelled", "运行已取消")
            if time.monotonic() > deadline:
                raise ApiError(504, "timeout", "助理响应超时")
    raise ApiError(504, "timeout", "助理响应超时")


def relay_events(bind, run_id: UUID, conversation_id: UUID, *, timeout_seconds: int) -> Iterator[str]:
    """Server-sent events for ``stream=true``: ``start``, ``delta``…, then ``done`` or ``error``.

    Internal pipeline ``step`` events are not exposed to external callers.
    """

    def frame(name: str, payload: dict) -> str:
        return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    yield frame("start", {"conversation_id": str(conversation_id), "run_id": str(run_id)})
    deadline = time.monotonic() + timeout_seconds + WAIT_MARGIN_SECONDS
    with Session(bind=bind) as db:
        for event in stream_run_events(db, run_id, 0, heartbeat=True):
            if event is None:
                yield ": keepalive\n\n"
            elif event["type"] == "token":
                yield frame("delta", {"text": event["data"].get("text", "")})
            elif event["type"] == "done":
                yield frame("done", {"message": {"role": "assistant", "content": event["data"].get("text", "")}, "usage": _usage(event["data"])})
                return
            elif event["type"] in ("error", "cancelled"):
                yield frame("error", {"code": event["data"].get("code", event["type"]), "message": event["data"].get("message", "助理运行失败")})
                return
            if time.monotonic() > deadline:
                yield frame("error", {"code": "timeout", "message": "助理响应超时"})
                return
