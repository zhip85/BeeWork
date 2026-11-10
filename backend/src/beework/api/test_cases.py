from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework.api.assistants import _actor_name, _audit, _load, _load_for_write
from beework.api.conventions import ApiError, check_row_version
from beework.api.runs import _admit
from beework.auth import require_csrf
from beework.db import get_db
from beework.entitlements import enforce_quota
from beework.models import Membership, utc_now
from beework.models.assistants import Assistant, AssistantVersion
from beework.models.testcases import AssistantTestCase, AssistantTestRun
from beework.rbac import require_tenant_permission
from beework.runtime.errors import RuntimeFailure
from beework.schemas.test_cases import (
    StartTestRunRequest,
    StartTestRunResponse,
    TestCaseListResponse,
    TestCaseResponse,
    TestCaseWrite,
    TestRunResponse,
)
from beework.test_cases import (
    MAX_CASES_PER_ASSISTANT,
    create_batch,
    expire_stale_batches,
    resolve_target,
)

router = APIRouter()

ACTIVE = ("queued", "running")


def _case_response(case: AssistantTestCase) -> TestCaseResponse:
    return TestCaseResponse.model_validate(case, from_attributes=True)


def _run_response(batch: AssistantTestRun) -> TestRunResponse:
    return TestRunResponse.model_validate(batch, from_attributes=True)


def _case_of(db: Session, assistant: Assistant, case_id: UUID, *, lock: bool = False) -> AssistantTestCase:
    statement = select(AssistantTestCase).where(
        AssistantTestCase.id == case_id,
        AssistantTestCase.assistant_id == assistant.id,
        AssistantTestCase.tenant_id == assistant.tenant_id,
    )
    case = db.scalar(statement.with_for_update() if lock else statement)
    if case is None:
        raise ApiError(404, "not_found", "未找到指定测试用例")
    return case


def _count(db: Session, assistant: Assistant) -> int:
    return db.scalar(
        select(func.count()).select_from(AssistantTestCase).where(
            AssistantTestCase.assistant_id == assistant.id, AssistantTestCase.tenant_id == assistant.tenant_id
        )
    ) or 0


def _active_run(db: Session, assistant: Assistant) -> AssistantTestRun | None:
    return db.scalar(
        select(AssistantTestRun)
        .where(
            AssistantTestRun.assistant_id == assistant.id,
            AssistantTestRun.tenant_id == assistant.tenant_id,
            AssistantTestRun.status.in_(ACTIVE),
            AssistantTestRun.approval_id.is_(None),  # the pre-publish gate runs on its own
        )
        .order_by(AssistantTestRun.created_at.desc())
        .limit(1)
    )


def _audit_case(db: Session, membership: Membership, action: str, assistant: Assistant, case: AssistantTestCase) -> None:
    _audit(db, membership, action, assistant, test_case_id=str(case.id), case_name=case.name)


@router.get("/assistants/{assistant_id}/test-cases", response_model=TestCaseListResponse)
def list_test_cases(
    assistant_id: UUID,
    status_filter: str | None = Query(default=None, alias="status", pattern="^(passed|failed|not_run)$"),
    type_filter: str | None = Query(default=None, alias="type", pattern="^(regression|safety|knowledge)$"),
    q: str | None = Query(default=None, max_length=100),
    membership: Membership = Depends(require_tenant_permission("assistants:view")),
    db: Session = Depends(get_db),
) -> TestCaseListResponse:
    assistant = _load(db, membership, assistant_id)
    expire_stale_batches(db, assistant)
    db.commit()
    statement = select(AssistantTestCase).where(
        AssistantTestCase.assistant_id == assistant.id, AssistantTestCase.tenant_id == assistant.tenant_id
    )
    everything = list(db.scalars(statement.order_by(AssistantTestCase.created_at, AssistantTestCase.id)))
    items = [
        case for case in everything
        if (not status_filter or case.status == status_filter)
        and (not type_filter or case.type == type_filter)
        and (not q or q.strip().lower() in f"{case.name}\n{case.input}".lower())
    ]
    active = _active_run(db, assistant)
    return TestCaseListResponse(
        items=[_case_response(case) for case in items],
        total=len(everything),
        passed=sum(case.status == "passed" for case in everything),
        failed=sum(case.status == "failed" for case in everything),
        not_run=sum(case.status == "not_run" for case in everything),
        active_run=_run_response(active) if active else None,
        limit=MAX_CASES_PER_ASSISTANT,
    )


@router.post("/assistants/{assistant_id}/test-cases", response_model=TestCaseResponse, status_code=status.HTTP_201_CREATED)
def create_test_case(
    assistant_id: UUID,
    payload: TestCaseWrite,
    membership: Membership = Depends(require_tenant_permission("assistants:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> TestCaseResponse:
    assistant = _load_for_write(db, membership, assistant_id)
    if _count(db, assistant) >= MAX_CASES_PER_ASSISTANT:
        raise ApiError(409, "test_case.limit", f"每个智能体最多 {MAX_CASES_PER_ASSISTANT} 个测试用例")
    case = AssistantTestCase(
        tenant_id=assistant.tenant_id, assistant_id=assistant.id, name=payload.name, input=payload.input,
        expected=payload.expected, type=payload.type, created_by=_actor_name(db, membership),
    )
    db.add(case)
    db.flush()
    _audit_case(db, membership, "assistant.test_case_created", assistant, case)
    db.commit()
    return _case_response(case)


@router.put("/assistants/{assistant_id}/test-cases/{case_id}", response_model=TestCaseResponse)
def update_test_case(
    assistant_id: UUID,
    case_id: UUID,
    payload: TestCaseWrite,
    membership: Membership = Depends(require_tenant_permission("assistants:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> TestCaseResponse:
    assistant = _load_for_write(db, membership, assistant_id)
    case = _case_of(db, assistant, case_id, lock=True)
    if payload.row_version is not None:
        check_row_version(case, payload.row_version, db)
    if case.run_state != "idle":
        raise ApiError(409, "test_case.running", "该用例正在运行，请等待结束后再修改")
    changed = (case.input, case.expected) != (payload.input, payload.expected)
    case.name, case.input, case.expected, case.type = payload.name, payload.input, payload.expected, payload.type
    if changed and case.status != "not_run":
        # The stored verdict was for the old wording, so it no longer says anything about this case.
        case.status, case.last_output, case.last_reason, case.last_run_id, case.last_run_at, case.last_target = (
            "not_run", None, None, None, None, None
        )
    case.row_version += 1
    case.updated_by = _actor_name(db, membership)
    case.updated_at = utc_now()
    _audit_case(db, membership, "assistant.test_case_updated", assistant, case)
    db.commit()
    return _case_response(case)


@router.post("/assistants/{assistant_id}/test-cases/{case_id}/copy", response_model=TestCaseResponse, status_code=status.HTTP_201_CREATED)
def copy_test_case(
    assistant_id: UUID,
    case_id: UUID,
    membership: Membership = Depends(require_tenant_permission("assistants:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> TestCaseResponse:
    assistant = _load_for_write(db, membership, assistant_id)
    source = _case_of(db, assistant, case_id)
    if _count(db, assistant) >= MAX_CASES_PER_ASSISTANT:
        raise ApiError(409, "test_case.limit", f"每个智能体最多 {MAX_CASES_PER_ASSISTANT} 个测试用例")
    copy = AssistantTestCase(
        tenant_id=assistant.tenant_id, assistant_id=assistant.id, name=f"{source.name}（副本）"[:100], input=source.input,
        expected=source.expected, type=source.type, created_by=_actor_name(db, membership),
    )
    db.add(copy)
    db.flush()
    _audit_case(db, membership, "assistant.test_case_copied", assistant, copy)
    db.commit()
    return _case_response(copy)


@router.delete("/assistants/{assistant_id}/test-cases/{case_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_test_case(
    assistant_id: UUID,
    case_id: UUID,
    membership: Membership = Depends(require_tenant_permission("assistants:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    assistant = _load_for_write(db, membership, assistant_id)
    case = _case_of(db, assistant, case_id, lock=True)
    _audit_case(db, membership, "assistant.test_case_deleted", assistant, case)
    db.delete(case)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/assistants/{assistant_id}/test-runs", response_model=StartTestRunResponse, status_code=status.HTTP_202_ACCEPTED)
def start_test_run(
    assistant_id: UUID,
    payload: StartTestRunRequest,
    membership: Membership = Depends(require_tenant_permission("assistants:test")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> StartTestRunResponse:
    """Queue a batch for the worker; poll ``GET /assistants/{id}/test-runs/{run_id}`` for progress."""
    assistant = _load(db, membership, assistant_id)
    _admit(db, membership, assistant)
    expire_stale_batches(db, assistant)
    statement = select(AssistantTestCase).where(
        AssistantTestCase.assistant_id == assistant.id, AssistantTestCase.tenant_id == assistant.tenant_id
    )
    if payload.case_ids is not None:
        statement = statement.where(AssistantTestCase.id.in_(payload.case_ids))
    cases = list(db.scalars(statement.order_by(AssistantTestCase.created_at, AssistantTestCase.id).with_for_update()))
    if payload.case_ids is not None and len(cases) != len(set(payload.case_ids)):
        raise ApiError(404, "not_found", "未找到指定测试用例")
    # The busy check comes after the case rows are locked: a concurrent start waits here, then sees the first batch.
    if _active_run(db, assistant) is not None:
        raise ApiError(409, "test_run.busy", "该智能体已有测试正在运行，请等待结束")
    if not cases:
        raise ApiError(422, "test_run.empty", "没有可运行的测试用例")
    if payload.version_id is not None:
        version = db.scalar(
            select(AssistantVersion).where(
                AssistantVersion.id == payload.version_id, AssistantVersion.assistant_id == assistant.id,
                AssistantVersion.tenant_id == assistant.tenant_id,
            )
        )
        if version is None:
            raise ApiError(404, "not_found", "未找到指定版本")
        target = {"kind": "version", "version_id": str(version.id)}
    else:
        target = {"kind": "draft"}
    try:
        _, _, label = resolve_target(db, assistant, target)
    except RuntimeFailure as exc:
        raise ApiError(409, exc.code, exc.message) from exc
    enforce_quota(db, membership.tenant_id, "monthly_points", 1, "本月模型点数已用完，请联系平台升级或等待下月重置")
    batch = create_batch(db, assistant, cases, target=target, target_label=label, user_id=membership.user_id)
    _audit(db, membership, "assistant.test_run_started", assistant, batch_id=str(batch.id), cases=str(len(cases)), target=label)
    db.commit()
    return StartTestRunResponse(run=_run_response(batch))


def _batch_of(db: Session, assistant: Assistant, run_id: UUID, *, lock: bool = False) -> AssistantTestRun:
    statement = select(AssistantTestRun).where(
        AssistantTestRun.id == run_id, AssistantTestRun.assistant_id == assistant.id,
        AssistantTestRun.tenant_id == assistant.tenant_id,
    )
    batch = db.scalar(statement.with_for_update() if lock else statement)
    if batch is None:
        raise ApiError(404, "not_found", "未找到指定测试运行")
    return batch


@router.get("/assistants/{assistant_id}/test-runs/{run_id}", response_model=TestRunResponse)
def get_test_run(
    assistant_id: UUID,
    run_id: UUID,
    membership: Membership = Depends(require_tenant_permission("assistants:view")),
    db: Session = Depends(get_db),
) -> TestRunResponse:
    return _run_response(_batch_of(db, _load(db, membership, assistant_id), run_id))


@router.post("/assistants/{assistant_id}/test-runs/{run_id}/cancel", response_model=TestRunResponse)
def cancel_test_run(
    assistant_id: UUID,
    run_id: UUID,
    membership: Membership = Depends(require_tenant_permission("assistants:test")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> TestRunResponse:
    batch = _batch_of(db, _load(db, membership, assistant_id), run_id, lock=True)
    if batch.status not in ACTIVE:
        raise ApiError(409, "test_run.not_cancellable", "测试运行已结束")
    batch.cancel_requested = True
    db.commit()
    return _run_response(batch)
