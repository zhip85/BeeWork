from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

CaseType = Literal["regression", "safety", "knowledge"]


class TestCaseWrite(BaseModel):
    """Status and results are not writable: they only come from test runs (AST-071)."""

    __test__ = False  # not a pytest/unittest test class

    name: str = Field(min_length=1, max_length=100)
    input: str = Field(min_length=1, max_length=2000)
    expected: str = Field(min_length=1, max_length=1000)
    type: CaseType = "regression"
    row_version: int | None = None

    @field_validator("name", "input", "expected")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value


class TestCaseResponse(BaseModel):
    __test__ = False

    id: UUID
    name: str
    input: str
    expected: str
    type: str
    status: str
    run_state: str
    last_output: str | None
    last_reason: str | None
    last_target: str | None
    last_run_at: datetime | None
    row_version: int
    created_at: datetime
    updated_at: datetime


class TestRunItem(BaseModel):
    __test__ = False

    case_id: str
    name: str
    status: str
    reason: str
    output: str | None = None
    run_id: str | None = None


class TestRunResponse(BaseModel):
    __test__ = False

    id: UUID
    status: str
    target_label: str
    total: int
    completed: int
    passed: int
    failed: int
    results: list[TestRunItem]
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class TestCaseListResponse(BaseModel):
    __test__ = False

    items: list[TestCaseResponse]
    total: int
    passed: int
    failed: int
    not_run: int
    # The batch currently queued or running for this assistant, so progress survives a page reload.
    active_run: TestRunResponse | None
    limit: int


class StartTestRunRequest(BaseModel):
    # Omitted: every case of the assistant. Otherwise only these cases (the row "run" button sends one).
    case_ids: list[UUID] | None = Field(default=None, max_length=100)
    # Omitted: the editable draft. Otherwise a published version of the assistant.
    version_id: UUID | None = None


class StartTestRunResponse(BaseModel):
    run: TestRunResponse
