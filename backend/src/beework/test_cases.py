"""Assistant test cases (T-027): batch execution through the normal runtime, judged by an evaluation model.

Each case is executed exactly like a console run (a ``test`` conversation and run, the whole OC pipeline, tokens
and trace included); the actual reply is then compared with the expected behaviour by a *judge*. The judge is
pluggable (``JudgeFactory``) so tests use a fake one; the default asks the tenant's default chat model through
LiteLLM. Status and result columns of a case are written only here.
"""

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.conversations import append_message, console_end_user, start_conversation
from beework.integrations.litellm import model_alias
from beework.models import User
from beework.models.approvals import ApprovalRequest
from beework.models.assistants import Assistant, AssistantVersion
from beework.models.runtime import Run
from beework.models.testcases import AssistantTestCase, AssistantTestRun
from beework.runtime.dsl import AssistantDSLv1, dsl_from_draft
from beework.runtime.errors import RuntimeFailure
from beework.runtime.executor import LLMFactory, execute_run, gateway_llm_for
from beework.worker.events import publish_run_event
from beework.worker.queue import enqueue, job_handler

logger = logging.getLogger(__name__)

MAX_CASES_PER_ASSISTANT = 100
MAX_STORED_OUTPUT = 20000
ENVIRONMENT = "production"
CASE_TYPES = ("regression", "safety", "knowledge")
GATE_KIND = "test_cases"  # marks the approval check entry this module owns


# ---------------------------------------------------------------------------
# Judge.
# ---------------------------------------------------------------------------


@dataclass
class Verdict:
    passed: bool
    reason: str


class Judge(Protocol):
    def __call__(self, *, input: str, expected: str, actual: str) -> Verdict: ...


JudgeFactory = Callable[[Session, UUID], Judge]

JUDGE_SYSTEM = (
    "你是严格的 AI 助理评测员。根据【用户输入】和【期望行为】，判断【助理的实际回复】是否符合期望行为。"
    "只输出一个 JSON 对象，不要输出其他内容：{\"pass\": true 或 false, \"reason\": \"一句话说明理由\"}。"
)


def parse_verdict(text: str) -> Verdict:
    """Read the judge's JSON answer; anything unreadable is a failed verdict that says so."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            value = data.get("pass")
            if isinstance(value, bool):
                return Verdict(value, str(data.get("reason") or "").strip()[:1000] or ("符合期望" if value else "不符合期望"))
        except (ValueError, AttributeError):
            pass
    return Verdict(False, "评测模型返回的结果无法解析，请重试")


def _content_text(content: Any) -> str:
    """Message content is a string, or for some providers a list of parts; keep only the text."""
    if isinstance(content, str):
        return content
    parts = []
    for part in content or []:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and part.get("type", "text") == "text":
            parts.append(str(part.get("text", "")))
    return "".join(parts)


def llm_judge(llm: BaseChatModel) -> Judge:
    def judge(*, input: str, expected: str, actual: str) -> Verdict:
        reply = llm.invoke(
            [
                SystemMessage(content=JUDGE_SYSTEM),
                HumanMessage(content=f"【用户输入】\n{input}\n\n【期望行为】\n{expected}\n\n【助理的实际回复】\n{actual}"),
            ]
        )
        return parse_verdict(_content_text(reply.content))

    return judge


def default_judge(db: Session, tenant_id: UUID) -> Judge:
    """Judge with the tenant's default chat model (through LiteLLM)."""
    from beework.api.assistants import _enabled_chat_models

    models, _ = _enabled_chat_models(db, tenant_id)
    if not models:
        raise RuntimeFailure("runtime.model_unavailable", "租户尚未启用对话模型，无法评测")
    chosen = next((model for model, is_default in models if is_default), models[0][0])
    dsl = AssistantDSLv1(prompt="judge", model_alias=model_alias(chosen.id))
    llm = gateway_llm_for(db, tenant_id, ENVIRONMENT, dsl, {"tenant_id": str(tenant_id), "source": "test_judge"})
    return llm_judge(llm)


# ---------------------------------------------------------------------------
# Targets.
# ---------------------------------------------------------------------------


def resolve_target(db: Session, assistant: Assistant, target: dict[str, Any]) -> tuple[AssistantDSLv1, UUID | None, str]:
    """The DSL a batch executes, the version it is bound to and a short label."""
    kind = target.get("kind")
    if kind == "version":
        version = db.scalar(
            select(AssistantVersion).where(
                AssistantVersion.id == UUID(target["version_id"]),
                AssistantVersion.assistant_id == assistant.id,
                AssistantVersion.tenant_id == assistant.tenant_id,
            )
        )
        if version is None:
            raise RuntimeFailure("not_found", "未找到指定版本")
        return AssistantDSLv1.model_validate(version.snapshot["dsl"]), version.id, f"v{version.version}"
    if kind == "snapshot":
        approval = db.get(ApprovalRequest, UUID(target["approval_id"]))
        if approval is None or approval.tenant_id != assistant.tenant_id:
            raise RuntimeFailure("not_found", "未找到审批单")
        return AssistantDSLv1.model_validate(approval.payload["snapshot"]["dsl"]), None, "待发布版本"
    return dsl_from_draft(assistant, db), None, "draft"


# ---------------------------------------------------------------------------
# Batches.
# ---------------------------------------------------------------------------


STALE_AFTER = timedelta(hours=2)


def expire_stale_batches(db: Session, assistant: Assistant) -> None:
    """A batch whose worker is long gone must not block the assistant or leave cases looking “running” forever."""
    cutoff = datetime.now(UTC) - STALE_AFTER
    stale = db.scalars(
        select(AssistantTestRun).where(
            AssistantTestRun.assistant_id == assistant.id,
            AssistantTestRun.tenant_id == assistant.tenant_id,
            AssistantTestRun.status.in_(("queued", "running")),
            AssistantTestRun.created_at < cutoff,
        ).with_for_update()
    )
    for batch in stale:
        _release_cases(db, batch)
        batch.status = "failed"
        batch.error = "运行超时，已自动结束"
        batch.finished_at = datetime.now(UTC)
        _report_to_approval(db, batch)


def create_batch(
    db: Session,
    assistant: Assistant,
    cases: list[AssistantTestCase],
    *,
    target: dict[str, Any],
    target_label: str,
    user_id: UUID | None,
    approval_id: UUID | None = None,
) -> AssistantTestRun:
    batch = AssistantTestRun(
        tenant_id=assistant.tenant_id,
        assistant_id=assistant.id,
        target=target,
        target_label=target_label,
        case_ids=[str(case.id) for case in cases],
        total=len(cases),
        approval_id=approval_id,
        created_by=user_id,
    )
    db.add(batch)
    db.flush()
    if target.get("kind") != "snapshot":  # gate batches leave the visible case results alone
        for case in cases:
            case.run_state = "queued"
    enqueue(
        db, "testcases.run", {"batch_id": str(batch.id)}, tenant_id=assistant.tenant_id,
        idempotency_key=f"testcases.run:{batch.id}", max_attempts=1,
    )
    return batch


def start_gate(db: Session, assistant: Assistant, approval: ApprovalRequest) -> AssistantTestRun | None:
    """AST-044: run every test case against the snapshot awaiting approval and attach the outcome to the request."""
    cases = list(
        db.scalars(
            select(AssistantTestCase)
            .where(AssistantTestCase.assistant_id == assistant.id, AssistantTestCase.tenant_id == assistant.tenant_id)
            .order_by(AssistantTestCase.created_at, AssistantTestCase.id)
        )
    )
    if not cases:
        return None
    batch = create_batch(
        db, assistant, cases, target={"kind": "snapshot", "approval_id": str(approval.id)}, target_label="待发布版本",
        user_id=approval.requester_id, approval_id=approval.id,
    )
    _set_gate_check(approval, f"回归测试运行中（0/{len(cases)}）", "warn")
    return batch


def _set_gate_check(approval: ApprovalRequest, label: str, level: str) -> None:
    others = [check for check in approval.checks if check.get("kind") != GATE_KIND]
    approval.checks = [*others, {"label": label, "level": level, "kind": GATE_KIND}]


def _report_to_approval(db: Session, batch: AssistantTestRun) -> None:
    if batch.approval_id is None:
        return
    approval = db.scalar(select(ApprovalRequest).where(ApprovalRequest.id == batch.approval_id).with_for_update())
    if approval is None:
        return
    if batch.status == "succeeded" and batch.failed == 0:
        _set_gate_check(approval, f"回归测试通过 {batch.passed}/{batch.total}", "ok")
    elif batch.status == "succeeded":
        _set_gate_check(approval, f"回归测试未通过 {batch.passed}/{batch.total}", "warn")
    else:
        _set_gate_check(approval, f"回归测试未能完成：{batch.error or '已取消'}", "warn")


def _start_case_run(db: Session, assistant: Assistant, batch: AssistantTestRun, case: AssistantTestCase, dsl: AssistantDSLv1, version_id: UUID | None, label: str) -> Run:
    user = db.get(User, batch.created_by) if batch.created_by else None
    if user is None:
        raise RuntimeFailure("runtime.tenant_unavailable", "发起人已不存在")
    conversation = start_conversation(
        db, assistant=assistant, end_user=console_end_user(db, batch.tenant_id, user), source="test",
        environment=ENVIRONMENT, created_by=batch.created_by,
    )
    conversation.assistant_version = label
    run = Run(
        tenant_id=batch.tenant_id, assistant_id=assistant.id, conversation_id=conversation.id, version_id=version_id,
        draft_row_version=None if version_id or batch.target.get("kind") == "snapshot" else assistant.row_version,
        environment=ENVIRONMENT, source="test", status="queued", config=dsl.model_dump(mode="json"),
        input={"message": case.input}, created_by=batch.created_by,
    )
    db.add(run)
    db.flush()
    append_message(db, conversation, "user", case.input, run_id=run.id)
    db.commit()
    publish_run_event(db, run.id, "queued", {"assistant_id": str(assistant.id), "test_case_id": str(case.id)}, tenant_id=batch.tenant_id)
    return run


def run_batch(
    db: Session,
    batch_id: UUID,
    *,
    llm_factory: LLMFactory | None = None,
    judge_factory: JudgeFactory | None = None,
) -> None:
    """Execute a batch to a terminal state. A batch reclaimed after a worker crash continues where it stopped."""
    batch = db.scalar(select(AssistantTestRun).where(AssistantTestRun.id == batch_id).with_for_update())
    if batch is None or batch.status not in {"queued", "running"}:
        return
    if batch.started_at is None:
        batch.started_at = datetime.now(UTC)
    batch.status = "running"
    db.commit()

    assistant = db.scalar(
        select(Assistant).where(Assistant.id == batch.assistant_id, Assistant.tenant_id == batch.tenant_id, Assistant.deleted_at.is_(None))
    )
    gate = batch.target.get("kind") == "snapshot"
    try:
        if assistant is None:
            raise RuntimeFailure("not_found", "助理已被删除")
        dsl, version_id, label = resolve_target(db, assistant, batch.target)
        judge = (judge_factory or default_judge)(db, batch.tenant_id)
    except Exception as exc:  # noqa: BLE001 - the batch records why nothing ran
        _fail_batch(db, batch_id, getattr(exc, "message", None) or str(exc))
        return

    try:
        _run_cases(db, batch, assistant, dsl, version_id, label, judge, llm_factory, gate)
    except Exception as exc:  # noqa: BLE001 - never leave the batch (and its cases) stuck as running
        db.rollback()  # the session may be in a failed state; only ids captured before are safe to use
        logger.exception("Test batch %s crashed", batch_id)
        _fail_batch(db, batch_id, f"运行异常：{getattr(exc, 'message', None) or exc}")


def _run_cases(db, batch, assistant, dsl, version_id, label, judge, llm_factory, gate) -> None:
    done = {item["case_id"] for item in batch.results}
    for case_id in batch.case_ids:
        if case_id in done:
            continue
        db.refresh(batch)
        if batch.cancel_requested:
            _release_cases(db, batch)
            batch.status = "cancelled"
            batch.finished_at = datetime.now(UTC)
            db.commit()
            _report_to_approval(db, batch)
            db.commit()
            return
        case = db.get(AssistantTestCase, UUID(case_id))
        if case is None:  # deleted while queued
            batch.total -= 1
            db.commit()
            continue
        if not gate:
            case.run_state = "running"
            db.commit()
        name = case.name
        outcome = _run_case(db, assistant, batch, case, dsl, version_id, label, judge, llm_factory)
        batch.results = [*batch.results, {"case_id": case_id, "name": name, **outcome}]
        batch.completed += 1
        batch.passed += outcome["status"] == "passed"
        batch.failed += outcome["status"] == "failed"
        case = db.get(AssistantTestCase, UUID(case_id), populate_existing=True)  # may have been deleted while it ran
        if case is not None and not gate:
            case.status = outcome["status"]
            case.run_state = "idle"
            case.last_output = (outcome.get("output") or "")[:MAX_STORED_OUTPUT]
            case.last_reason = outcome["reason"][:1000]
            case.last_run_id = UUID(outcome["run_id"]) if outcome.get("run_id") else None
            case.last_target = label
            case.last_run_at = datetime.now(UTC)
        db.commit()

    batch.status = "succeeded"
    batch.finished_at = datetime.now(UTC)
    _report_to_approval(db, batch)
    db.commit()


def _run_case(db, assistant, batch, case, dsl, version_id, label, judge, llm_factory) -> dict[str, Any]:
    try:
        run = _start_case_run(db, assistant, batch, case, dsl, version_id, label)
        execute_run(db, run.id, llm_factory=llm_factory)
        db.refresh(run)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception("Test case %s could not be run", case.id)
        return {"status": "failed", "reason": f"运行出错：{getattr(exc, 'message', None) or exc}", "output": None, "run_id": None}
    if run.status != "succeeded":
        detail = run.error or run.error_code or run.status
        return {"status": "failed", "reason": f"运行失败：{detail}", "output": None, "run_id": str(run.id)}
    text = (run.output or {}).get("text", "")
    try:
        verdict = judge(input=case.input, expected=case.expected, actual=text)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Judging test case %s failed", case.id)
        return {"status": "failed", "reason": f"评测失败：{getattr(exc, 'message', None) or exc}", "output": text, "run_id": str(run.id)}
    return {"status": "passed" if verdict.passed else "failed", "reason": verdict.reason, "output": text, "run_id": str(run.id)}


def _release_cases(db: Session, batch: AssistantTestRun) -> None:
    if batch.target.get("kind") == "snapshot":
        return
    finished = {item["case_id"] for item in batch.results}
    for case_id in batch.case_ids:
        if case_id not in finished:
            case = db.get(AssistantTestCase, UUID(case_id))
            if case is not None and case.run_state != "idle":
                case.run_state = "idle"


def _fail_batch(db: Session, batch_id: UUID, message: str) -> None:
    db.rollback()
    batch = db.get(AssistantTestRun, batch_id)
    if batch is None:
        return
    _release_cases(db, batch)
    batch.status = "failed"
    batch.error = message[:500]
    batch.finished_at = datetime.now(UTC)
    _report_to_approval(db, batch)
    db.commit()


@job_handler("testcases.run")
def run_batch_job(db: Session, payload: dict[str, Any]) -> None:
    run_batch(db, UUID(payload["batch_id"]))
