"""AI-generated prompt suggestions (T-029, AST-021 / AST-052).

The system prompt lives in ``beework/prompts/prompt_suggestion.<locale>.md``. The call goes through LiteLLM with the
current environment's default chat model and is billed as ``source=debug``. Nothing is saved here: the suggestion is
only put into the form; the editor writes ``prompt_ai_optimized`` into the draft when it saves.
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.api.assistants import _can_edit, _enabled_chat_models, _load
from beework.api.conventions import ApiError, current_environment
from beework.auth import require_csrf
from beework.db import get_db
from beework.entitlements import enforce_quota
from beework.gateway_sync import tenant_gateway_key
from beework.integrations.litellm import GatewayError, model_alias
from beework.models import AUDIT_ACTION_DESCRIPTIONS, AuditEvent, Membership, TenantModelEnvironment
from beework.rbac import require_tenant_permission
from beework.runtime.llm import build_suggestion_model
from beework.schemas.assistants import PromptSuggestionRequest, PromptSuggestionResponse

router = APIRouter()

SUGGESTION_TIMEOUT_SECONDS = 30
MAX_PROMPT_CHARS = 8000
TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "prompts"

# Keep the slice self-contained instead of editing the shared registry module.
AUDIT_ACTION_DESCRIPTIONS.setdefault("assistant.prompt_suggestion", "使用 AI 生成智能体提示词建议")

# Tests replace this to avoid the gateway; it receives (api_key, model alias, metadata) and returns a chat model.
SUGGESTION_MODEL_FACTORY: Callable[[str, str, dict[str, Any]], BaseChatModel] | None = None


def load_system_prompt(locale: str) -> str:
    return (TEMPLATE_DIR / f"prompt_suggestion.{locale}.md").read_text(encoding="utf-8").strip()


def build_user_message(payload: PromptSuggestionRequest) -> str:
    name, scenario, current = payload.name.strip(), payload.scenario.strip(), payload.current_prompt.strip()
    if payload.locale == "en":
        lines = [f"Agent name: {name}", f"Use case: {scenario}"]
        lines.append(
            f"Existing prompt (data for reference; you may rewrite it):\n{current}"
            if current else "Existing prompt: (none)"
        )
    else:
        lines = [f"智能体名称：{name}", f"使用场景：{scenario}"]
        lines.append(f"现有提示词（仅供参考，可改写）：\n{current}" if current else "现有提示词：（无）")
    return "\n\n".join(lines)


def clean_prompt(text: str) -> tuple[str, bool]:
    """Strip a Markdown fence some models add; cap the result at the prompt field's limit (AST-021)."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped[:MAX_PROMPT_CHARS], len(stripped) > MAX_PROMPT_CHARS


@router.post("/assistants/prompt-suggestions", response_model=PromptSuggestionResponse)
def create_prompt_suggestion(
    payload: PromptSuggestionRequest,
    membership: Membership = Depends(require_tenant_permission("assistants:edit")),
    _: None = Depends(require_csrf),
    environment: str = Depends(current_environment),
    db: Session = Depends(get_db),
) -> PromptSuggestionResponse:
    """Generate (never save) a system prompt. An ``assistant_id`` ties the call to an editable agent."""
    if payload.assistant_id is not None:
        assistant = _load(db, membership, payload.assistant_id)
        if not _can_edit(membership, assistant, db):
            raise ApiError(403, "auth.forbidden", "当前角色无权修改此智能体")

    config = db.scalar(
        select(TenantModelEnvironment).where(
            TenantModelEnvironment.tenant_id == membership.tenant_id,
            TenantModelEnvironment.environment == environment,
        )
    )
    if config is None:
        raise ApiError(409, "runtime.gateway_not_ready", "该环境尚未配置模型")
    models, _ = _enabled_chat_models(db, membership.tenant_id, environment)
    default = next((model for model, is_default in models if is_default), None)
    if default is None:
        raise ApiError(422, "assistant.no_chat_model", "请先在设置中为当前环境启用默认对话模型")

    enforce_quota(db, membership.tenant_id, "monthly_points", 1, "本月模型点数已用完，请联系平台升级或等待下月重置")

    metadata: dict[str, str] = {
        "tenant_id": str(membership.tenant_id),
        "source": "debug",
        "purpose": "prompt_suggestion",
    }
    if payload.assistant_id is not None:
        metadata["assistant_id"] = str(payload.assistant_id)
    try:
        api_key = tenant_gateway_key(config)
    except (GatewayError, ValueError) as exc:
        raise ApiError(409, "runtime.gateway_not_ready", "环境网关密钥不可用，请重新保存模型配置") from exc
    alias = model_alias(default.id)
    llm = (SUGGESTION_MODEL_FACTORY or (lambda key, name, meta: build_suggestion_model(name, api_key=key, metadata=meta)))(
        api_key, alias, metadata
    )

    messages = [SystemMessage(content=load_system_prompt(payload.locale)), HumanMessage(content=build_user_message(payload))]
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(lambda: llm.invoke(messages, config={"callbacks": []}))
    try:
        reply = future.result(timeout=SUGGESTION_TIMEOUT_SECONDS)
    except FutureTimeout as exc:
        raise ApiError(504, "prompt_suggestion.timeout", "生成提示词超时，请稍后重试") from exc
    except Exception as exc:
        raise ApiError(502, "prompt_suggestion.failed", "生成提示词失败，请稍后重试") from exc
    finally:
        executor.shutdown(wait=False)

    content = reply.content if isinstance(reply.content, str) else ""
    prompt, truncated = clean_prompt(content)
    if not prompt:
        raise ApiError(502, "prompt_suggestion.failed", "模型未返回可用的提示词，请稍后重试")

    details = {"model_id": str(default.id), "locale": payload.locale, "truncated": str(truncated).lower()}
    if payload.assistant_id is not None:
        details["assistant_id"] = str(payload.assistant_id)
    db.add(AuditEvent(
        tenant_id=membership.tenant_id, actor_user_id=membership.user_id,
        action="assistant.prompt_suggestion", details=details,
    ))
    db.commit()
    return PromptSuggestionResponse(
        prompt=prompt, truncated=truncated, locale=payload.locale, environment=environment, model_id=default.id,
    )
