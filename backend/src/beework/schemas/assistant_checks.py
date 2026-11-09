from typing import Literal
from uuid import UUID

from pydantic import BaseModel

CheckSeverity = Literal["blocker", "high", "info"]


class CheckIssue(BaseModel):
    code: str
    severity: CheckSeverity
    message: str
    # Where the problem is fixed: "basic" (wizard), "capabilities" (tab), "environment" (settings).
    target: str


class CheckResponse(BaseModel):
    issues: list[CheckIssue]
    blockers: int
    high_impact: int
    info: int


class MountEntry(BaseModel):
    id: UUID
    kind: str
    name: str
    state: Literal["available", "disabled", "deleted"]
    meta: dict[str, object]


class MountGroup(BaseModel):
    kind: str
    items: list[MountEntry]


class MountsResponse(BaseModel):
    total: int
    groups: list[MountGroup]
    # Kinds that can be mounted today; empty until the owning tasks register a resolver.
    mountable_kinds: list[str]


class CandidateEntry(BaseModel):
    id: UUID
    kind: str
    name: str
    kb_type: Literal["standard", "faq"]
    documents: int
    faqs: int


class CandidatesResponse(BaseModel):
    items: list[CandidateEntry]
