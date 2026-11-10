"""The assistant glossary and question rewrite on the OC page (T-057, OC-030–051)."""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.api.assistants import MODEL_ENVIRONMENT, _load
from beework.api.conventions import ApiError
from beework.auth import require_csrf
from beework.db import get_db
from beework.entitlements import enforce_quota
from beework.gateway_sync import tenant_gateway_key
from beework.governance.glossary_runtime import assistant_view, dictionary, expand, retrieval_query
from beework.integrations.litellm import GatewayError, model_alias
from beework.models import Membership, TenantModelEnvironment
from beework.rbac import require_tenant_permission
from beework.runtime.llm import build_rewrite_model
from beework.runtime.oc import GlossaryConfig, OCConfig, RewriteConfig
from beework.runtime.rewrite import active_steps, rewrite

router = APIRouter()

# Tests replace this to avoid the gateway; it receives (api_key, model alias, metadata) and returns a chat model.
REWRITE_MODEL_FACTORY: Callable[[str, str, dict[str, Any]], BaseChatModel] | None = None


class GlossaryPreviewRequest(BaseModel):
    glossary: GlossaryConfig


class TermItem(BaseModel):
    key: str
    term: str
    aliases: list[str]
    definition: str
    label: str  # industry_inherited | tenant | tenant_modified | assistant | assistant_modified
    restorable: bool
    deletable: bool


class GlossaryChain(BaseModel):
    industries: list[str]
    industry_terms: int
    tenant_total: int
    tenant_level: int
    assistant_inherited: int
    assistant_level: int
    hidden: int
    effective: int


class GlossaryPreviewResponse(BaseModel):
    chain: GlossaryChain
    items: list[TermItem]
    hidden: list[TermItem]  # inherited terms this assistant deleted (for “全部恢复”)


@router.post("/assistants/{assistant_id}/oc/glossary/preview", response_model=GlossaryPreviewResponse)
def preview_glossary(
    assistant_id: UUID,
    payload: GlossaryPreviewRequest,
    membership: Membership = Depends(require_tenant_permission("orchestration:view")),
    db: Session = Depends(get_db),
) -> GlossaryPreviewResponse:
    """The effective terms the draft ``oc.glossary`` would give the assistant; nothing is saved."""
    assistant = _load(db, membership, assistant_id)
    view, chain = assistant_view(db, assistant.tenant_id, payload.glossary)
    local_override_keys = {t.from_id for t in payload.glossary.terms if t.from_id and not t.hidden}
    items = [
        TermItem(
            key=item.id, term=item.term.term, aliases=list(item.term.aliases), definition=item.term.definition, label=item.label,
            restorable=item.label == "assistant_modified" and item.id in local_override_keys, deletable=True,
        )
        for item in sorted(view.terms, key=lambda i: i.term.key)
    ]
    hidden_ids = {t.from_id for t in payload.glossary.terms if t.hidden and t.from_id}
    return GlossaryPreviewResponse(
        chain=GlossaryChain(**{k: chain[k] for k in GlossaryChain.model_fields}), items=items,
        hidden=[TermItem(key=h_id, term=term.term, aliases=list(term.aliases), definition=term.definition, label="industry_inherited", restorable=False, deletable=False)
                for h_id, term in ((t.id, t) for t in view.hidden) if h_id in hidden_ids],
    )


class RewriteTestRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    history: list[dict[str, str]] = Field(default_factory=list, max_length=20)
    rewrite: RewriteConfig | None = None  # the unsaved draft being tested; the saved draft when omitted
    glossary: GlossaryConfig | None = None


class RewriteStep(BaseModel):
    step: str  # coreference | normalize | alias_expansion | decompose | translate
    before: str
    after: str


class RewriteTestResponse(BaseModel):
    steps: list[RewriteStep]  # only the steps that changed something, in the order of OC-050
    query: str
    sub_questions: list[str]
    synonyms: list[str]
    failure: str | None  # timeout | invalid | error: the original question was used
    custom_prompt: bool


def _model(db: Session, tenant_id: UUID, assistant_id: UUID, config: RewriteConfig, assistant_model_id: Any) -> BaseChatModel:
    environment = db.scalar(
        select(TenantModelEnvironment).where(TenantModelEnvironment.tenant_id == tenant_id, TenantModelEnvironment.environment == MODEL_ENVIRONMENT)
    )
    if environment is None:
        raise ApiError(409, "runtime.gateway_not_ready", "该环境尚未配置模型")
    model_id = config.model_id or (str(assistant_model_id) if assistant_model_id else None)
    if not model_id:
        raise ApiError(409, "rewrite.model_missing", "请先选择改写模型，或为智能体选择模型")
    metadata = {"tenant_id": str(tenant_id), "assistant_id": str(assistant_id), "source": "debug", "purpose": "rewrite_test"}
    try:
        api_key = tenant_gateway_key(environment)
    except (GatewayError, ValueError) as exc:
        raise ApiError(409, "runtime.gateway_not_ready", "环境网关密钥不可用，请重新保存模型配置") from exc
    alias = model_alias(UUID(model_id))
    return (REWRITE_MODEL_FACTORY or (lambda key, name, meta: build_rewrite_model(name, api_key=key, metadata=meta)))(api_key, alias, metadata)


@router.post("/assistants/{assistant_id}/oc/rewrite-test", response_model=RewriteTestResponse)
def test_rewrite(
    assistant_id: UUID,
    payload: RewriteTestRequest,
    membership: Membership = Depends(require_tenant_permission("orchestration:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> RewriteTestResponse:
    """OC-050: run the question through the configured steps and show each one that changed it. The model call is a
    ``source=debug`` call and counts against the tenant's points."""
    assistant = _load(db, membership, assistant_id)
    saved = OCConfig.model_validate((assistant.draft_config or {}).get("oc") or {})
    config = payload.rewrite or saved.rewrite
    glossary = payload.glossary or saved.glossary
    question = payload.question.strip()
    steps: list[RewriteStep] = []
    current = question
    sub_questions: list[str] = []
    failure = None
    if config.enabled and active_steps(config):
        enforce_quota(db, membership.tenant_id, "monthly_points", 1, "本月模型点数已用完，请联系平台升级或等待下月重置")
        llm = _model(db, membership.tenant_id, assistant.id, config, (assistant.draft_config or {}).get("model_id"))
        result = rewrite(question, payload.history, config, llm)
        failure, sub_questions = result.failure, result.sub_questions
        by_step = {s["step"]: s for s in result.steps}
        for name in ("coreference", "normalize"):
            if name in by_step:
                steps.append(RewriteStep(**by_step[name]))
                current = by_step[name]["after"]
        entries = dictionary(db, assistant.tenant_id, glossary)
        expansion = expand(current, entries)
        if expansion.synonyms:
            steps.append(RewriteStep(step="alias_expansion", before=current, after=retrieval_query(current, expansion.synonyms)))
        for name in ("decompose", "translate"):
            if name in by_step:
                steps.append(RewriteStep(**by_step[name]))
        final = result.query
    else:
        entries = dictionary(db, assistant.tenant_id, glossary)
        expansion = expand(current, entries)
        if expansion.synonyms:
            steps.append(RewriteStep(step="alias_expansion", before=current, after=retrieval_query(current, expansion.synonyms)))
        final = current
    return RewriteTestResponse(
        steps=steps, query=final, sub_questions=sub_questions, synonyms=expansion.synonyms, failure=failure,
        custom_prompt=bool(config.enabled and config.custom_prompt.enabled and config.custom_prompt.text.strip()),
    )
