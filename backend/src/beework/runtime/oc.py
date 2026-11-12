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


class AssistantTerm(_Section):
    """An assistant-level term: its own (``from_id`` empty), an override of an inherited term, or a hidden marker for one."""

    id: str = Field(min_length=1, max_length=64)
    from_id: str | None = Field(default=None, max_length=64)  # the inherited term this overrides or hides
    hidden: bool = False
    term: str = Field(max_length=400)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    definition: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def check(self) -> "AssistantTerm":
        self.term = self.term.strip()
        if not self.term or len(self.term) > 200:
            raise ValueError("标准术语不能为空且不超过 200 个字符")
        aliases = list(dict.fromkeys(a.strip() for a in self.aliases if a and a.strip() and a.strip().lower() != self.term.lower()))
        if len(aliases) > 20 or any(len(a) > 100 for a in aliases):
            raise ValueError("别名最多 20 个，每个不超过 100 个字符")
        self.aliases = aliases
        self.definition = self.definition.strip()
        if len(self.definition) > 1000:
            raise ValueError("定义不能超过 1000 个字符")
        return self


class GlossaryConfig(_Section):
    use_tenant: bool = True  # “继承租户术语表” (OC-032): off starts from nothing but the assistant's own terms
    terms: list[AssistantTerm] = Field(default_factory=list, max_length=500)


Language = Literal["English", "中文", "日本語"]


class CoreferenceStep(_Section):
    enabled: bool = True
    rounds: int = Field(default=3, ge=1, le=10)  # 参考上下文轮数


class DecomposeStep(_Section):
    enabled: bool = False
    max_subquestions: int = Field(default=3, ge=2, le=6)


class TranslateStep(_Section):
    enabled: bool = False
    language: Language = "English"


class CustomPrompt(_Section):
    enabled: bool = False
    text: str = Field(default="", max_length=2000)


class RewriteConfig(_Section):
    """Question rewriting (OC-040–048). Off by default here: a rewrite is an extra model call on every message, so it
    is switched on per assistant once a rewrite model is chosen (the PRD default is on)."""

    enabled: bool = False
    model_id: str | None = Field(default=None, max_length=64)  # an enabled chat model of the tenant; empty = the assistant's own
    only_low_confidence: bool = True  # decided by routing (T-058): rewrite only when the intent confidence is low
    confidence_threshold: float = Field(default=0.7, ge=0.3, le=0.95)
    coreference: CoreferenceStep = Field(default_factory=CoreferenceStep)
    normalize: bool = True  # 口语规范化
    decompose: DecomposeStep = Field(default_factory=DecomposeStep)
    translate: TranslateStep = Field(default_factory=TranslateStep)
    custom_prompt: CustomPrompt = Field(default_factory=CustomPrompt)


def keywords_of(pattern: str) -> list[str]:
    """The keywords of a rule: separated by Chinese or English commas (and, leniently, 、 and newlines), trimmed, lower-cased, deduplicated."""
    import re as _re

    seen: dict[str, None] = {}
    for part in _re.split(r"[,，、\n]", pattern):
        part = part.strip()
        if part:
            seen.setdefault(part.lower())
    return list(seen)


class IntentRule(_Section):
    """A priority rule of the L2 intent stage (T-161, OC-061): a keyword list or a regular expression and the one capability it sends
    the message to. The target is a registry entry (T-160) by id; ``target_name`` is only for display. ``created_at`` breaks ties
    between equal priorities."""

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(max_length=200)
    priority: int = Field(default=10, ge=0, le=100000)  # the smaller, the earlier
    match_type: Literal["keyword", "regex"] = "keyword"
    pattern: str = Field(default="", max_length=500)
    target_entry_id: str | None = Field(default=None, max_length=64)
    target_name: str = Field(default="", max_length=200)
    enabled: bool = True
    created_at: str = Field(default="", max_length=40)

    @model_validator(mode="after")
    def check(self) -> "IntentRule":
        import regex

        self.name = self.name.strip()
        if not self.name or len(self.name) > 80:
            raise ValueError("规则名称不能为空且不超过 80 个字符")
        self.pattern = self.pattern.strip()
        if not self.pattern:
            raise ValueError("请填写匹配条件")
        if self.match_type == "regex":
            try:
                regex.compile(self.pattern)
            except regex.error as exc:
                raise ValueError("正则表达式不合法") from exc
        elif not keywords_of(self.pattern):
            raise ValueError("请填写至少一个关键词")
        return self


class IntentCustomPrompt(_Section):
    enabled: bool = False
    text: str = Field(default="", max_length=1000)  # appended to the classifier's prompt


class IntentConfig(_Section):
    """L2 intent judgement (T-161, 演进 PRD 23.1): priority rules, then recall from the capability registry, then the classifier (JEV).
    A confident, single, directly callable candidate goes straight to the capability (path A); everything else goes to L3 (path B).

    Off by default, like the rewrite: it costs a model call per message, and an agent that already plans with tools (L3) behaves
    the same without it. The classifier model is a setting, not a constant: an ordinary chat model today, a dedicated
    classification model when one is available (the stage only depends on the ``IntentClassifier`` interface)."""

    enabled: bool = False
    model_id: str | None = Field(default=None, max_length=64)  # an enabled chat model of the tenant; empty = the assistant's own
    threshold: float = Field(default=0.7, ge=0.3, le=0.95)  # the top candidate must reach it
    margin: float = Field(default=0.15, ge=0.0, le=0.5)  # ...and lead the second one by this much
    recall_k: int = Field(default=8, ge=3, le=20)  # candidates handed to the classifier
    faq_direct: bool = True  # a confident FAQ hit of a mounted knowledge base is answered directly, without a model call
    custom_prompt: IntentCustomPrompt = Field(default_factory=IntentCustomPrompt)
    priority_rules_enabled: bool = False
    rules: list[IntentRule] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def check(self) -> "IntentConfig":
        self.threshold = round(self.threshold, 2)
        self.margin = round(self.margin, 2)
        ids = [rule.id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("规则编号重复")
        return self


class ComponentsConfig(_Section):
    enabled: bool = False


class OCConfig(_Section):
    oc_schema_version: Literal[1] = 1
    pii: PiiConfig = Field(default_factory=PiiConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    glossary: GlossaryConfig = Field(default_factory=GlossaryConfig)
    rewrite: RewriteConfig = Field(default_factory=RewriteConfig)
    intent: IntentConfig = Field(default_factory=IntentConfig)
    components: ComponentsConfig = Field(default_factory=ComponentsConfig)

    @model_validator(mode="before")
    @classmethod
    def drop_legacy_routing(cls, data: object) -> object:
        """The ``routing`` section of the first sketch (T-054) is gone — T-058 was replaced by the L2 ``intent`` section; older drafts, versions and
        runs that still carry the key stay loadable and simply lose it."""
        if isinstance(data, dict) and "routing" in data:
            data = {key: value for key, value in data.items() if key != "routing"}
        return data
