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
    # Knowledge bases (T-033); other kinds describe themselves in ``meta`` (T-066, T-072–T-075).
    kb_type: Literal["standard", "faq"] | None = None
    documents: int | None = None
    faqs: int | None = None
    meta: dict = {}


class TypeEntry(BaseModel):
    kind: str
    mountable: bool  # a resolver is registered: the type can be mounted today
    available: int  # how many resources of this type could be mounted now (capped at 100)


class TypesResponse(BaseModel):
    items: list[TypeEntry]


class DependencyEntry(BaseModel):
    kind: Literal["channel", "assistant", "flow", "portal_app"]
    id: UUID
    name: str
    status: str
    detail: str = ""


class DependenciesResponse(BaseModel):
    items: list[DependencyEntry]


class CandidatesResponse(BaseModel):
    items: list[CandidateEntry]
