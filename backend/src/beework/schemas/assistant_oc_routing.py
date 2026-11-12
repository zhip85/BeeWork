from typing import Literal

from pydantic import BaseModel, Field

EntryKind = Literal["mcp_tool", "api_tool", "skill", "dify_workflow", "dify_chatflow", "agent", "kb"]


class IntentTargetEntry(BaseModel):
    """A registry entry in the agent's scope, with whether a priority rule may point at it (OC-061)."""

    id: str
    kind: EntryKind
    name: str
    description: str = ""
    status: Literal["available", "disabled", "invalid"]
    callable: bool  # directly callable: enabled for direct calls, available, not a write, not a knowledge base
    reason: str | None = None  # why not: disabled | not_direct | write | kb


class IntentTargetList(BaseModel):
    items: list[IntentTargetEntry]


class RuleTestRequest(BaseModel):
    """OC-061 “测试规则”: the rule as it is being edited (not saved) against a sample question."""

    question: str = Field(max_length=500)
    match_type: Literal["keyword", "regex"] = "keyword"
    pattern: str = Field(default="", max_length=500)


class RuleTestResponse(BaseModel):
    valid: bool
    error: str | None = None
    matched: bool = False
    keywords: list[str] = Field(default_factory=list)  # the tags shown under the keyword field


class IntentProblemEntry(BaseModel):
    code: str
    message: str
    rule_id: str | None = None


class IntentStatus(BaseModel):
    """What is wrong with the *saved draft* intent configuration now (the editor shows “目标失效 / 待重选” and counts them)."""

    problems: list[IntentProblemEntry]
    valid_rules: int
    invalid_rules: int
    in_scope: int  # how many capabilities the agent can see (available ones)


class IntentReferences(BaseModel):
    """AST-065: what in the draft still points at an entry of a source — asked before the source is unmounted."""

    rules: list[str]
    total: int
