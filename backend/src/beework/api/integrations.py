from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException

from beework.auth import get_current_user
from beework.integrations.langfuse import check_langfuse, langfuse_configured
from beework.integrations.litellm import LiteLLMClient, gateway_configured
from beework.integrations.ragflow import RAGFlowClient, ragflow_configured
from beework.models import User
from beework.schemas import IntegrationStatus

router = APIRouter()


def _litellm_health() -> bool:
    with LiteLLMClient() as client:
        return client.health()


def _ragflow_health() -> bool:
    with RAGFlowClient() as client:
        return client.health()


INTEGRATIONS: list[tuple[str, str, str, Callable[[], bool], Callable[[], bool]]] = [
    ("litellm", "LiteLLM", "AI 网关：模型路由、租户密钥、限流与用量", gateway_configured, _litellm_health),
    ("ragflow", "RAGFlow", "知识引擎：文档解析、切片、向量化与检索", ragflow_configured, _ragflow_health),
    ("langfuse", "Langfuse", "调用追踪：模型与智能体运行轨迹", langfuse_configured, check_langfuse),
]


@router.get("/platform/integrations", response_model=list[IntegrationStatus])
def list_integrations(user: User = Depends(get_current_user)) -> list[IntegrationStatus]:
    if not user.is_platform_admin:
        raise HTTPException(status_code=403, detail="仅平台管理员可查看集成状态")
    statuses: list[IntegrationStatus] = []
    for key, name, purpose, configured, health in INTEGRATIONS:
        if not configured():
            statuses.append(
                IntegrationStatus(
                    key=key, name=name, purpose=purpose, configured=False, reachable=None,
                    detail="未配置连接地址或密钥",
                )
            )
            continue
        try:
            health()
        except Exception as exc:  # noqa: BLE001 - surface any engine failure as status
            statuses.append(
                IntegrationStatus(
                    key=key, name=name, purpose=purpose, configured=True, reachable=False,
                    detail=str(exc)[:300],
                )
            )
        else:
            statuses.append(
                IntegrationStatus(key=key, name=name, purpose=purpose, configured=True, reachable=True)
            )
    return statuses
