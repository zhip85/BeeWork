"""OC (orchestration control) configuration: one sub-object per OC stage, stored in ``draft_config.oc``.

Each stage owns its section; the stage tasks (T-055 PII, T-056 memory, T-057 glossary / rewrite,
T-058 routing, T-059 components) extend the sub-models. Defaults here make an assistant without any OC
configuration behave exactly as before, and unknown keys are rejected so typos never pass silently.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MemoryType = Literal["preference", "history", "fact"]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PiiConfig(_Section):
    enabled: bool = True


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
