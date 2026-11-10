"""OC (orchestration control) configuration: one sub-object per OC stage, stored in ``draft_config.oc``.

Each stage owns its section; the stage tasks (T-055 PII, T-056 memory, T-057 glossary / rewrite,
T-058 routing, T-059 components) extend the sub-models. Defaults here make an assistant without any OC
configuration behave exactly as before, and unknown keys are rejected so typos never pass silently.
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MemoryType = Literal["preference", "history", "fact"]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssistantPiiRule(_Section):
    """An assistant-level PII rule: its own (``from_id`` empty) or an override of an inherited rule (``from_id`` = its key)."""

    id: str = Field(min_length=1, max_length=64)
    from_id: str | None = Field(default=None, max_length=64)
    name: str = Field(max_length=200)
    match_type: Literal["regex", "keyword", "classifier", "llm_judge"] = "regex"
    pattern: str = Field(default="", max_length=4000)
    stage: Literal["both", "input", "output"] = "both"
    action: Literal["mask", "block", "flag"] = "mask"
    block_message: str = Field(default="", max_length=600)
    enabled: bool = True

    @model_validator(mode="after")
    def check(self) -> "AssistantPiiRule":
        self.name = self.name.strip()
        if not self.name or len(self.name) > 80:
            raise ValueError("规则名称不能为空且不超过 80 个字符")
        self.pattern = self.pattern.strip()
        if self.match_type in ("regex", "keyword") and not self.pattern:
            raise ValueError("请填写匹配内容")
        if self.match_type == "regex":
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"正则表达式无效：{exc}") from exc
        if self.action != "block":
            self.block_message = ""
        return self


class PiiConfig(_Section):
    enabled: bool = True  # “PII 个人信息检测”: off disables every rule that is not enforced (OC-010)
    use_global: bool = True  # “引用全局 PII 规则”: off keeps only enforced rules plus the assistant's own
    rules: list[AssistantPiiRule] = Field(default_factory=list, max_length=100)


class ShortTermMemory(_Section):
    enabled: bool = True
    window_rounds: int = Field(default=3, ge=1, le=30)
    max_tokens: int = Field(default=5000, ge=1000, le=30000)
    type_whitelist: list[MemoryType] = Field(default_factory=lambda: ["preference", "history"])


class MemoryFile(_Section):
    enabled: bool = True
    top_k: int = Field(default=3, ge=1, le=10)
    retention_days: int = Field(default=180, ge=1, le=3650)


class UserProfileFile(MemoryFile):
    top_k: int = Field(default=2, ge=1, le=10)
    retention_days: int = Field(default=365, ge=1, le=3650)
    write_enabled: bool = True
    subscribe_enabled: bool = True


class ToolsFile(MemoryFile):
    top_k: int = Field(default=5, ge=1, le=10)
    retention_days: int = Field(default=90, ge=1, le=3650)


class SessionFile(MemoryFile):
    top_k: int = Field(default=5, ge=1, le=10)
    retention_days: int = Field(default=30, ge=1, le=3650)


class LongTermMemory(_Section):
    enabled: bool = True
    user_profile: UserProfileFile = Field(default_factory=UserProfileFile)
    user_memory: MemoryFile = Field(default_factory=MemoryFile)
    tools: ToolsFile = Field(default_factory=ToolsFile)
    session: SessionFile = Field(default_factory=SessionFile)


class MemoryConfig(_Section):
    short_term: ShortTermMemory = Field(default_factory=ShortTermMemory)
    long_term: LongTermMemory = Field(default_factory=LongTermMemory)


class GlossaryConfig(_Section):
    enabled: bool = False


class RewriteConfig(_Section):
    enabled: bool = False


class RoutingConfig(_Section):
    priority_rules_enabled: bool = False


class ComponentsConfig(_Section):
    enabled: bool = False


class OCConfig(_Section):
    oc_schema_version: Literal[1] = 1
    pii: PiiConfig = Field(default_factory=PiiConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    glossary: GlossaryConfig = Field(default_factory=GlossaryConfig)
    rewrite: RewriteConfig = Field(default_factory=RewriteConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    components: ComponentsConfig = Field(default_factory=ComponentsConfig)
