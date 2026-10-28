"""Execute a queued run: compile its frozen DSL, stream the graph, publish events, record the result."""

import logging
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import openai
from langchain_core.language_models import BaseChatModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.gateway_sync import tenant_gateway_key
from beework.integrations.litellm import GatewayError
from beework.models import Tenant, TenantModelEnvironment
from beework.models.runtime import Run
from beework.runtime.compiler import RuntimeDeps, compile_assistant
from beework.runtime.dsl import AssistantDSLv1
from beework.runtime.errors import RunCancelled, RunTimedOut, RuntimeFailure
from beework.runtime.llm import build_chat_model
from beework.runtime.retrieval import Retriever, StubRetriever, tenant_dataset_ids
from beework.runtime.tracing import TraceHandle, trace_run
from beework.worker.events import publish_run_event

logger = logging.getLogger(__name__)

LLMFactory = Callable[[Run, AssistantDSLv1], BaseChatModel]
SERVING_TENANT_STATUSES = frozenset({"trial", "active", "grace"})
CANCEL_POLL_SECONDS = 0.5
TOKEN_FLUSH_SECONDS = 0.1
TOKEN_FLUSH_CHARS = 120


def run_metadata(run: Run) -> dict[str, str]:
    metadata = {
        "tenant_id": str(run.tenant_id),
        "assistant_id": str(run.assistant_id),
        "run_id": str(run.id),
        "source": run.source,
        "environment": run.environment,
        "version": str(run.version_id) if run.version_id else f"draft:{run.draft_row_version}",
    }
    if run.conversation_id:
        metadata["conversation_id"] = str(run.conversation_id)
    return metadata


def gateway_llm(db: Session, run: Run, dsl: AssistantDSLv1) -> BaseChatModel:
    environment = db.scalar(
        select(TenantModelEnvironment).where(
            TenantModelEnvironment.tenant_id == run.tenant_id,
            TenantModelEnvironment.environment == run.environment,
        )
    )
    if environment is None:
        raise RuntimeFailure("runtime.gateway_not_ready", "该环境尚未配置模型")
    try:
        api_key = tenant_gateway_key(environment)
    except GatewayError as exc:
        raise RuntimeFailure("runtime.gateway_not_ready", str(exc)) from exc
    except ValueError as exc:
        raise RuntimeFailure("runtime.gateway_not_ready", "环境网关密钥不可用，请重新保存模型配置") from exc
    return build_chat_model(dsl, api_key=api_key, metadata=run_metadata(run))


def classify_error(exc: Exception) -> RuntimeFailure:
    """Turn a provider/gateway error into a short Chinese message; never echo the raw error body."""
    if isinstance(exc, RuntimeFailure):
        return exc
    if isinstance(exc, openai.APITimeoutError):
        return RuntimeFailure("runtime.model_timeout", "模型响应超时")
    if isinstance(exc, openai.APIConnectionError):
        return RuntimeFailure("runtime.gateway_unreachable", "无法连接 AI 网关")
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code in {401, 403}:
            return RuntimeFailure("runtime.gateway_denied", "AI 网关拒绝了请求，请检查环境密钥与模型授权")
        if exc.status_code == 429:
            return RuntimeFailure("runtime.rate_limited", "模型请求过于频繁，请稍后重试")
        return RuntimeFailure("runtime.model_error", f"模型调用失败（{exc.status_code}）")
    logger.exception("Unexpected runtime error")
    return RuntimeFailure("runtime.internal", "运行时内部错误")


class _TokenBuffer:
    """Coalesce streamed tokens into fewer persisted events."""

    def __init__(self, db: Session, run: Run) -> None:
        self.db, self.run = db, run
        self.parts: list[str] = []
        self.size = 0
        self.last_flush = time.monotonic()

    def add(self, text: str) -> None:
        self.parts.append(text)
        self.size += len(text)
        if self.size >= TOKEN_FLUSH_CHARS or time.monotonic() - self.last_flush >= TOKEN_FLUSH_SECONDS:
            self.flush()

    def flush(self) -> None:
        if self.parts:
            publish_run_event(
                self.db, self.run.id, "token", {"text": "".join(self.parts)}, tenant_id=self.run.tenant_id
            )
            self.parts, self.size = [], 0
        self.last_flush = time.monotonic()


def _finish(
    db: Session,
    run: Run,
    status: str,
    event_type: str,
    data: dict[str, Any],
    *,
    started: float | None,
    error: RuntimeFailure | None = None,
    output: dict[str, Any] | None = None,
    trace_url: str | None = None,
) -> None:
    run.status = status
    run.finished_at = datetime.now(UTC)
    run.latency_ms = int((time.monotonic() - started) * 1000) if started is not None else None
    run.output = output
    run.trace_url = trace_url
    if error is not None:
        run.error_code, run.error = error.code, error.message[:500]
    db.commit()
    publish_run_event(db, run.id, event_type, data, tenant_id=run.tenant_id)


def execute_run(
    db: Session,
    run_id: UUID,
    *,
    llm_factory: LLMFactory | None = None,
    retriever: Retriever | None = None,
) -> None:
    """Run one queued run to a terminal state. Idempotent: anything not ``queued`` is left alone."""
    traces: list[TraceHandle] = []
    try:
        _execute_run(db, run_id, llm_factory, retriever, traces)
    finally:
        for trace in traces:
            trace.flush()  # after the terminal event, so tracing never delays the user


def _execute_run(
    db: Session,
    run_id: UUID,
    llm_factory: LLMFactory | None,
    retriever: Retriever | None,
    traces: list[TraceHandle],
) -> None:
    run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
    if run is None or run.status != "queued":
        return
    if run.cancel_requested:
        _finish(db, run, "cancelled", "cancelled", {"reason": "user"}, started=None)
        return
    tenant = db.get(Tenant, run.tenant_id)
    if tenant is None or tenant.status not in SERVING_TENANT_STATUSES:
        error = RuntimeFailure("runtime.tenant_unavailable", "企业工作区当前不可用")
        _finish(db, run, "failed", "error", {"code": error.code, "message": error.message}, started=None, error=error)
        return

    started = time.monotonic()
    run.status = "running"
    run.started_at = datetime.now(UTC)
    db.commit()
    publish_run_event(db, run.id, "running", {}, tenant_id=run.tenant_id)

    buffer = _TokenBuffer(db, run)
    last_poll = [0.0]
    trace_url: str | None = None
    try:
        dsl = AssistantDSLv1.model_validate(run.config)
        llm = llm_factory(run, dsl) if llm_factory else gateway_llm(db, run, dsl)

        def check_stop() -> None:
            now = time.monotonic()
            if now - started > dsl.limits.timeout_seconds:
                raise RunTimedOut(dsl.limits.timeout_seconds)
            if now - last_poll[0] >= CANCEL_POLL_SECONDS:
                last_poll[0] = now
                db.refresh(run)
                if run.cancel_requested:
                    raise RunCancelled

        def allowed_datasets(requested: Sequence[str]) -> list[str]:
            return tenant_dataset_ids(run.tenant_id, requested)

        final: dict[str, Any] = {}
        with trace_run(run.id, metadata=run_metadata(run)) as trace:
            trace_url = trace.trace_url
            traces.append(trace)
            graph = compile_assistant(
                dsl,
                RuntimeDeps(
                    llm=llm,
                    retriever=retriever or StubRetriever(),
                    tenant_id=run.tenant_id,
                    allowed_dataset_ids=allowed_datasets,
                    check_stop=check_stop,
                    callbacks=trace.callbacks,
                ),
            )
            for mode, payload in graph.stream(
                {"input": (run.input or {}).get("message", ""), "history": []},
                stream_mode=["custom", "values"],
            ):
                if mode == "custom":
                    if payload.get("type") == "token":
                        buffer.add(payload["text"])
                    else:
                        buffer.flush()
                        publish_run_event(db, run.id, "step", payload, tenant_id=run.tenant_id)
                    check_stop()
                else:
                    final = payload
        buffer.flush()
    except RunCancelled:
        buffer.flush()
        _finish(db, run, "cancelled", "cancelled", {"reason": "user"}, started=started, trace_url=trace_url)
        return
    except Exception as exc:
        db.rollback()
        run = db.get(Run, run_id)
        failure = classify_error(exc)
        _finish(
            db, run, "failed", "error",
            {"code": failure.code, "message": failure.message}, started=started, error=failure, trace_url=trace_url,
        )
        return

    usage = final.get("usage") or {"input": None, "output": None}
    citations = [
        {"document_name": c["document_name"], "chunk_id": c["chunk_id"], "snippet": c["text"][:200]}
        for c in final.get("context", [])
    ]
    retrieval = {"mode": final.get("retrieval_mode", "stub"), "citations": citations}
    run.tokens_in, run.tokens_out = usage.get("input"), usage.get("output")
    run.retrieval_mode = retrieval["mode"]
    text = final.get("text", "")
    latency_ms = int((time.monotonic() - started) * 1000)
    _finish(
        db, run, "succeeded", "done",
        {"text": text, "usage": usage, "retrieval": retrieval, "latency_ms": latency_ms, "trace_url": trace_url},
        started=started, output={"text": text, "usage": usage, "retrieval": retrieval}, trace_url=trace_url,
    )
