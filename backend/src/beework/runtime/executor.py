"""Execute a queued run: compile its frozen DSL, stream the graph, publish events, record the result."""

import logging
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import openai
from langchain_core.language_models import BaseChatModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

import beework.agent_teams  # noqa: F401  (registers the leaf-agent tool provider and the agent mount resolver)
import beework.capability_registry  # noqa: F401  (keeps the unified capability entries derived from their sources)
import beework.capability_service  # noqa: F401  (registers the MCP tool provider and the mcp mount resolver)
import beework.skill_service  # noqa: F401  (registers the skill load tool and the skill mount resolver)
from beework import model_catalog
from beework.compliance import get_policy
from beework.conversations import append_message, history_for_run
from beework.faqs import record_hits as record_faq_hits
from beework.gateway_sync import tenant_gateway_key
from beework.governance.glossary_runtime import dictionary as glossary_dictionary
from beework.governance.pii_engine import scan
from beework.governance.runtime_policy import build_policy
from beework.integrations.knowledge_engine import KnowledgeEngineError
from beework.integrations.litellm import GatewayError, model_alias
from beework.intent_service import (
    FAQ_MARGIN,
    build_candidates,
    candidate_of,
    config_from_dsl,
    faq_for_document,
    load_pending,
    load_scope,
)
from beework.knowledge_mounts import resolve_knowledge_bases
from beework.memory import load_memory_context
from beework.models import Tenant, TenantModelEnvironment
from beework.models.conversations import Conversation
from beework.models.jobs import RunEvent
from beework.models.runtime import Run
from beework.pii import make_redactor, redact_for_tenant
from beework.runtime import (
    glossary_stage as _glossary_stage,  # noqa: F401  (registers the glossary_rewrite stage)
)
from beework.runtime import (
    memory_stage as _memory_stage,  # noqa: F401  (registers the memory stage)
)
from beework.runtime import (
    pii_stage as _pii_stage,  # noqa: F401  (registers the pii_input / pii_output stages)
)
from beework.runtime.compiler import RuntimeDeps, StageClock, compile_assistant
from beework.runtime.dsl import AssistantDSLv1, KnowledgeBaseRef
from beework.runtime.errors import RunCancelled, RunTimedOut, RuntimeFailure
from beework.runtime.intent import (
    Candidate,
    ClassifyResult,
    LlmJsonClassifier,
    Slots,
    extract_arguments,
    trivial_arguments,
)
from beework.runtime.llm import build_chat_model, build_intent_model, build_rewrite_model
from beework.runtime.pii_stage import holds_output
from beework.runtime.retrieval import EngineRetriever, Retriever
from beework.runtime.rewrite import RewriteResult, active_steps, rewrite
from beework.runtime.route_stage import FaqAnswer, IntentRuntime
from beework.runtime.tool_runtime import ToolRuntime
from beework.runtime.tracing import TraceHandle, trace_run
from beework.tools.executor import ToolContext, invoke_tool
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
    return gateway_llm_for(db, run.tenant_id, run.environment, dsl, run_metadata(run))


def gateway_llm_for(
    db: Session, tenant_id: UUID, environment_name: str, dsl: AssistantDSLv1, metadata: dict[str, str]
) -> BaseChatModel:
    environment = db.scalar(
        select(TenantModelEnvironment).where(
            TenantModelEnvironment.tenant_id == tenant_id,
            TenantModelEnvironment.environment == environment_name,
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
    effective = dsl
    # T-014: the frozen snapshot may name a model that was disabled since it was published. Resolve it
    # against the current catalog, and give the gateway one degradation target for a failed call.
    resolved = model_catalog.resolve_runtime_model(db, effective.model_alias)
    effective = effective.model_copy(update={"model_alias": resolved.alias})
    info = dict(metadata)
    if resolved.switched_from:
        info["model_fallback_from"] = resolved.switched_from
    return build_chat_model(effective, api_key=api_key, metadata=info, fallbacks=resolved.fallbacks)


def gateway_rewrite_llm(db: Session, run: Run, dsl: AssistantDSLv1) -> BaseChatModel:
    """The question-rewrite model (OC-041): the configured one, or the assistant's own when none is chosen.

    Both go through catalog resolution (T-014 review fix 5): a configured model that was disabled
    since the draft was saved is replaced by its degradation target, with one request-level fallback."""
    environment = db.scalar(
        select(TenantModelEnvironment).where(
            TenantModelEnvironment.tenant_id == run.tenant_id, TenantModelEnvironment.environment == run.environment
        )
    )
    if environment is None:
        raise RuntimeFailure("runtime.gateway_not_ready", "该环境尚未配置模型")
    resolved = model_catalog.resolve_runtime_model(
        db,
        model_alias(UUID(dsl.oc.rewrite.model_id)) if dsl.oc.rewrite.model_id else dsl.model_alias,
    )
    info = {**run_metadata(run), "purpose": "rewrite"}
    if resolved.switched_from:
        info["model_fallback_from"] = resolved.switched_from
    return build_rewrite_model(
        resolved.alias, api_key=tenant_gateway_key(environment), metadata=info, fallbacks=resolved.fallbacks
    )


def gateway_intent_llm(db: Session, run: Run, dsl: AssistantDSLv1) -> BaseChatModel:
    """The L2 classifier model (T-161): the configured one, or the assistant's own when none is chosen.

    Resolved through the catalog like the rewrite model (T-014 review fix 5)."""
    environment = db.scalar(
        select(TenantModelEnvironment).where(
            TenantModelEnvironment.tenant_id == run.tenant_id, TenantModelEnvironment.environment == run.environment
        )
    )
    if environment is None:
        raise RuntimeFailure("runtime.gateway_not_ready", "该环境尚未配置模型")
    resolved = model_catalog.resolve_runtime_model(
        db,
        model_alias(UUID(dsl.oc.intent.model_id)) if dsl.oc.intent.model_id else dsl.model_alias,
    )
    info = {**run_metadata(run), "purpose": "intent"}
    if resolved.switched_from:
        info["model_fallback_from"] = resolved.switched_from
    return build_intent_model(
        resolved.alias, api_key=tenant_gateway_key(environment), metadata=info, fallbacks=resolved.fallbacks
    )


def _intent_model(bind: Any, run: Run, dsl: AssistantDSLv1) -> BaseChatModel:
    """Build the classifier model from a session of its own (the graph thread must not use the run's session)."""
    with Session(bind=bind) as side:
        return gateway_intent_llm(side, run, dsl)


def _intent_runtime(
    bind: Any, run: Run, dsl: AssistantDSLv1, *, trace: Any, check_stop: Callable[[], None], guard: Callable[[str], str],
    end_user: str | None, llm_factory: LLMFactory | None, retriever: Retriever, deadline: float | None = None,
) -> IntentRuntime | None:
    """What the route stage works with (T-161): the agent's scope read from the capability registry, the classifier and the argument
    extractor, the calls of path A and FAQ answers. Every database access uses a session of its own: the stage runs in a graph worker
    thread. ``None`` when nothing is in scope — then the agent simply answers."""
    config = config_from_dsl(dsl)
    with Session(bind=bind) as side:
        scope = load_scope(side, run.tenant_id, config)
    if not scope.entries:
        return None
    model: list[BaseChatModel] = []

    def llm() -> BaseChatModel:
        if not model:  # built on first use, so a missing key only costs the judgement
            model.append(llm_factory(run, dsl) if llm_factory else _intent_model(bind, run, dsl))
        return model[0]

    def candidates(query: str) -> list[Candidate]:
        with Session(bind=bind) as side:
            return build_candidates(side, run.tenant_id, config, scope, query, dsl.oc.intent.recall_k)

    def classify(query: str, history: list[dict[str, str]], found: list[Candidate]) -> ClassifyResult:
        try:
            classifier = LlmJsonClassifier(llm(), dsl.oc.intent, callbacks=trace.callbacks)
        except Exception:
            logger.warning("Intent classifier model unavailable", exc_info=True)
            return ClassifyResult(failure="error")
        return classifier.classify(query, history, found)

    def extract(target: Candidate, query: str, history: list[dict[str, str]]) -> Slots:
        if trivial_arguments(target.kind, target.input_schema, query) is not None:
            return extract_arguments(None, target, query, history)  # type: ignore[arg-type]  # needs no model
        try:
            return extract_arguments(llm(), target, query, history, callbacks=trace.callbacks)
        except Exception:
            logger.warning("Argument extraction unavailable", exc_info=True)
            return Slots("failed", problems=["error"])

    def invoke(target: Candidate, args: dict[str, Any], stream: Callable[[str], None] | None = None):
        with Session(bind=bind) as side:
            ctx = ToolContext(
                tenant_id=run.tenant_id, allowed=frozenset({target.tool}), run_id=run.id, assistant_id=run.assistant_id, end_user=end_user,
                guard_output=guard, span=trace.span, environment=run.environment, check_stop=check_stop, stream=stream, deadline=deadline,
            )
            result = invoke_tool(side, ctx, target.tool, args)
            side.commit()  # a Chatflow call binds its Dify conversation
            return result

    def faq(target: Candidate, query: str) -> FaqAnswer | None:
        with Session(bind=bind) as side:
            refs = resolve_knowledge_bases(side, run.tenant_id, [UUID(target.source_id)])
        ref = refs[0] if refs else None
        if ref is None or not ref.faq_dataset_id:
            return None
        hits = [c for c in retriever.retrieve(tenant_id=run.tenant_id, query=query, bases=[ref], top_k=3).chunks if c.kind == "faq"]
        if not hits:
            return None
        best, second = hits[0], (hits[1] if len(hits) > 1 else None)
        if best.score < ref.threshold or (second is not None and best.score - second.score < FAQ_MARGIN):
            return None  # not a confident, clear FAQ match: the usual retrieval and the model answer instead
        with Session(bind=bind) as side:
            row = faq_for_document(side, run.tenant_id, best.document_id)
            answer = row.answer if row is not None else None
        if not answer:
            return None
        chunk = {
            "document_name": best.document_name, "chunk_id": best.chunk_id, "text": best.text, "score": best.score, "kind": "faq",
            "kb_name": best.kb_name, "document_id": best.document_id,
        }
        return FaqAnswer(answer, chunk)

    def pending() -> Candidate | None:
        with Session(bind=bind) as side:
            current = side.get(Run, run.id)
            entry_id = load_pending(side, current) if current is not None else None
        entry = scope.entries.get(entry_id or "")
        return candidate_of(entry) if entry is not None else None

    return IntentRuntime(callable_by_id=scope.callable_by_id(), candidates=candidates, classify=classify, extract=extract, invoke=invoke, faq=faq, pending=pending,
                         faq_bases=[candidate_of(e) for e in scope.entries.values() if e.kind == "kb" and e.direct_enabled])


def _rewrite_model(bind: Any, run: Run, dsl: AssistantDSLv1) -> BaseChatModel:
    """Build the rewrite model from a session of its own (the graph thread must not use the run's session)."""
    with Session(bind=bind) as side:
        return gateway_rewrite_llm(side, run, dsl)


def classify_error(exc: Exception) -> RuntimeFailure:
    """Turn a provider/gateway error into a short Chinese message; never echo the raw error body.

    The failing OC stage (set by the pipeline wrapper) is carried on the returned failure.
    """
    failure = _classify(exc)
    failure.stage = failure.stage or getattr(exc, "beework_stage", None)
    return failure


def _classify(exc: Exception) -> RuntimeFailure:
    if isinstance(exc, RuntimeFailure):
        return exc
    if isinstance(exc, KnowledgeEngineError):
        # The user sees a fixed message; operators get the engine's (already sanitized) reason in the Worker log.
        logger.warning("Knowledge retrieval failed: %s", exc)
        return RuntimeFailure("runtime.knowledge_error", "知识库检索失败，请检查知识引擎状态后重试")
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
    timings: StageClock | None = None,
) -> None:
    run.status = status
    run.finished_at = datetime.now(UTC)
    run.latency_ms = int((time.monotonic() - started) * 1000) if started is not None else None
    run.output = output
    run.trace_url = trace_url
    if error is not None:
        run.error_code, run.error, run.error_stage = error.code, error.message[:500], error.stage
    if timings is not None:
        run.stage_ms, run.pipeline_ms = dict(timings.stage_ms), timings.pipeline_ms
    db.commit()
    publish_run_event(db, run.id, event_type, data, tenant_id=run.tenant_id)
    scrub_run_content(db, run)


def scrub_run_content(db: Session, run: Run) -> None:
    """Under a redacting policy, do not keep raw conversation text in run rows and events (SET-070).

    Streaming clients already received the raw tokens; afterwards the per-token events are dropped and
    the ``done`` event keeps only the redacted answer, so a later replay shows the redacted text.
    """
    if not get_policy(db, run.tenant_id).redact_pii:
        return
    if run.input and isinstance(run.input.get("message"), str):
        run.input = {**run.input, "message": redact_for_tenant(db, run.tenant_id, run.input["message"])}
    if run.output and isinstance(run.output.get("text"), str):
        run.output = {**run.output, "text": redact_for_tenant(db, run.tenant_id, run.output["text"])}
    db.execute(delete(RunEvent).where(RunEvent.run_id == run.id, RunEvent.type == "token"))
    for event in db.scalars(select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.type == "done")):
        if isinstance(event.data.get("text"), str):
            event.data = {**event.data, "text": redact_for_tenant(db, run.tenant_id, event.data["text"])}
    db.commit()


def execute_run(
    db: Session,
    run_id: UUID,
    *,
    llm_factory: LLMFactory | None = None,
    retriever: Retriever | None = None,
    rewrite_llm_factory: LLMFactory | None = None,
    intent_llm_factory: LLMFactory | None = None,
) -> None:
    """Run one queued run to a terminal state. Idempotent: anything not ``queued`` is left alone."""
    traces: list[TraceHandle] = []
    try:
        _execute_run(db, run_id, llm_factory, retriever, traces, rewrite_llm_factory, intent_llm_factory)
    finally:
        for trace in traces:
            trace.flush()  # after the terminal event, so tracing never delays the user


def _execute_run(
    db: Session,
    run_id: UUID,
    llm_factory: LLMFactory | None,
    retriever: Retriever | None,
    traces: list[TraceHandle],
    rewrite_llm_factory: LLMFactory | None = None,
    intent_llm_factory: LLMFactory | None = None,
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
    clock = StageClock()
    bind = db.get_bind()
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
                # Graph nodes run in worker threads while this thread publishes events on ``db``;
                # a Session is not thread safe, so nodes only ever use their own short-lived sessions.
                with Session(bind=bind) as side:
                    if side.scalar(select(Run.cancel_requested).where(Run.id == run.id)):
                        raise RunCancelled

        def resolve_bases(requested: Sequence[UUID]) -> list[KnowledgeBaseRef]:
            # Called from a graph worker thread: own session. The tenant's current rows decide what is searched.
            with Session(bind=bind) as side:
                return resolve_knowledge_bases(side, run.tenant_id, list(requested))

        end_user_id = None
        if run.conversation_id is not None:
            end_user_id = db.scalar(select(Conversation.end_user_id).where(Conversation.id == run.conversation_id))

        def memory_context(query: str) -> list[dict[str, str]]:
            with Session(bind=bind) as side:  # called from a graph worker thread: own session
                from beework.portal_profile_service import memory_allowed_for_end_user

                if not memory_allowed_for_end_user(side, run.tenant_id, end_user_id):
                    return []  # personal memory switched off by the administrator or the employee (T-145)
                return load_memory_context(
                    side, tenant_id=run.tenant_id, end_user_id=end_user_id, assistant_id=run.assistant_id,
                    oc=dsl.oc, query=query,
                )

        final: dict[str, Any] = {}
        redact = get_policy(db, run.tenant_id).redact_pii
        entries = glossary_dictionary(db, run.tenant_id, dsl.oc.glossary)
        rewrite_fn = None
        if dsl.oc.rewrite.enabled and active_steps(dsl.oc.rewrite):
            rewrite_llm: list[BaseChatModel] = []

            def rewrite_fn(query: str, history: list[dict[str, str]]) -> RewriteResult:
                try:
                    if not rewrite_llm:  # built on first use, so a missing key only costs the rewrite
                        rewrite_llm.append((rewrite_llm_factory or gateway_rewrite_llm)(run, dsl) if rewrite_llm_factory else _rewrite_model(bind, run, dsl))
                    return rewrite(query, history, dsl.oc.rewrite, rewrite_llm[0])
                except Exception:
                    logger.warning("Question rewrite unavailable", exc_info=True)
                    return RewriteResult(query=query, failure="error")

        pii_rules = build_policy(db, run.tenant_id, dsl.oc.pii)  # platform and tenant rules are read now, the assistant's from the snapshot

        def guard_tool_output(text: str) -> str:
            """Tool results pass the same output rules as the reply before the model sees them (CAP-054)."""
            outcome = scan(text, pii_rules, "output")
            return outcome.message if outcome.blocked else outcome.text

        fallback_llm = None
        if dsl.fallback_model_alias:
            fallback_dsl = dsl.model_copy(update={"model_alias": dsl.fallback_model_alias})

            def fallback_llm() -> BaseChatModel:
                if llm_factory:
                    return llm_factory(run, fallback_dsl)
                with Session(bind=bind) as side:  # called from a graph worker thread: own session
                    return gateway_llm_for(side, run.tenant_id, run.environment, fallback_dsl, run_metadata(run))
        short_term = dsl.oc.memory.short_term  # OC-020: the window in rounds (a round is a question and its answer) and its token budget
        history = history_for_run(db, run, short_term.window_rounds * 2, max_tokens=short_term.max_tokens) if short_term.enabled and dsl.limits.history_messages > 0 else []  # history_messages=0 still switches history off
        with trace_run(run.id, metadata=run_metadata(run), redact=redact, redactor=make_redactor(pii_rules)) as trace:
            trace_url = trace.trace_url
            traces.append(trace)
            tool_runtime = None
            if dsl.tools.mounts:  # what the agent mounted and ticked, through the controlled executor; one span per call
                tool_runtime = ToolRuntime(
                    bind, dsl, tenant_id=run.tenant_id, run_id=run.id, assistant_id=run.assistant_id,
                    end_user=str(end_user_id) if end_user_id else None, guard_output=guard_tool_output, span=trace.span,
                    environment=run.environment, check_stop=check_stop, clock=clock, max_calls=dsl.limits.max_tool_calls,
                    deadline=started + dsl.limits.timeout_seconds,
                )
            intent_runtime = None
            if dsl.oc.intent.enabled:  # L2: the agent's scope from the capability registry, the classifier, path A (T-161)
                intent_runtime = _intent_runtime(
                    bind, run, dsl, trace=trace, check_stop=check_stop, guard=guard_tool_output,
                    end_user=str(end_user_id) if end_user_id else None, llm_factory=intent_llm_factory, retriever=retriever or EngineRetriever(),
                    deadline=started + dsl.limits.timeout_seconds,
                )
            graph = compile_assistant(
                dsl,
                RuntimeDeps(
                    llm=llm,
                    retriever=retriever or EngineRetriever(),
                    tenant_id=run.tenant_id,
                    resolve_bases=resolve_bases,
                    check_stop=check_stop,
                    callbacks=trace.callbacks,
                    span=trace.span,
                    clock=clock,
                    memory=memory_context,
                    pii_rules=pii_rules,
                    hold_output=holds_output(pii_rules),
                    dictionary=entries,
                    rewrite=rewrite_fn,
                    tools=tool_runtime,
                    fallback_llm=fallback_llm,
                    routing=intent_runtime,
                ),
            )
            for mode, payload in graph.stream(
                {"input": (run.input or {}).get("message", ""), "history": history},
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
        _finish(db, run, "cancelled", "cancelled", {"reason": "user"}, started=started, trace_url=trace_url, timings=clock)
        return
    except Exception as exc:
        db.rollback()
        run = db.get(Run, run_id)
        failure = classify_error(exc)
        _finish(
            db, run, "failed", "error",
            {"code": failure.code, "message": failure.message, "stage": failure.stage},
            started=started, error=failure, trace_url=trace_url, timings=clock,
        )
        return

    usage = final.get("usage") or {"input": None, "output": None}
    citations = [
        {
            "document_name": c["document_name"], "chunk_id": c["chunk_id"], "snippet": c["text"][:200],
            "kind": c.get("kind", "document"), "kb_name": c.get("kb_name", ""), "score": round(c.get("score", 0), 4),
        }
        for c in final.get("context", [])
    ]
    retrieval = {"mode": final.get("retrieval_mode", "none"), "citations": citations}
    pii = {"hits": final.get("pii_hits", []), "failures": final.get("pii_failures", []), "blocked": bool(final.get("blocked"))}
    glossary = final.get("stage_info") or {}
    route = final.get("route_info") or {}
    faq_documents = [c.get("document_id", "") for c in final.get("context", []) if c.get("kind") == "faq"]
    run.tokens_in, run.tokens_out = usage.get("input"), usage.get("output")
    run.retrieval_mode = retrieval["mode"]
    text = final.get("text", "")
    latency_ms = int((time.monotonic() - started) * 1000)
    if run.conversation_id is not None:
        conversation = db.scalar(
            select(Conversation).where(Conversation.id == run.conversation_id).with_for_update()
        )
        if conversation is not None:
            append_message(
                db, conversation, "assistant", text,
                run_id=run.id, trace_id=trace_url.rsplit("/", 1)[-1] if trace_url else None, citations=citations,
            )
            conversation.tokens += (usage.get("input") or 0) + (usage.get("output") or 0)
    if faq_documents:
        record_faq_hits(db, run.tenant_id, run.id, faq_documents)
    _finish(
        db, run, "succeeded", "done",
        {"text": text, "usage": usage, "retrieval": retrieval, "latency_ms": latency_ms, "trace_url": trace_url, "pii": pii, "query": glossary, "route": route},
        started=started, output={"text": text, "usage": usage, "retrieval": retrieval, "pii": pii, "query": glossary, "route": route}, trace_url=trace_url,
        timings=clock,
    )
