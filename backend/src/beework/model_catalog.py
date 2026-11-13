"""T-014: model catalog degradation rules, deletion guards and runtime model resolution.

The platform catalog is the source of truth. LiteLLM keeps a projection of each
deployment plus a router-level fallback (``POST /fallback``); at run time the
catalog decides which deployment an assistant actually calls, even when its
frozen snapshot still names a model that was disabled or deleted later.
"""

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.errors import ApiError
from beework.integrations.litellm import model_alias
from beework.models import PlatformModel, PlatformModelTenant, Tenant, TenantModelSelection

ALIAS_PREFIX = "bw-"


@dataclass(frozen=True)
class ModelState:
    """What the configuration UI has to say about an assistant's chosen model."""

    unavailable: bool = False
    fallback_name: str | None = None
    disabled_name: str | None = None
    fallback_id: UUID | None = None


@dataclass(frozen=True)
class RuntimeModel:
    alias: str
    fallbacks: list[str] = field(default_factory=list)
    switched_from: str | None = None
    effective_name: str | None = None


def model_id_from_alias(alias: str) -> UUID | None:
    if not alias.startswith(ALIAS_PREFIX):
        return None
    try:
        return UUID(alias[len(ALIAS_PREFIX) :])
    except ValueError:
        return None


def _chain(db: Session, start: PlatformModel) -> tuple[list[PlatformModel], bool]:
    """Follow ``fallback_model_id``. Cyclic legacy data is reported, not looped on.

    There is no depth cap: the ``seen`` set alone terminates the walk, so a long but legitimate
    chain is never mistaken for a cycle (review fix) and a real cycle cannot loop forever.
    """
    items: list[PlatformModel] = []
    seen: set[UUID] = set()
    current: PlatformModel | None = start
    while current is not None:
        if current.id in seen:
            return items, True
        seen.add(current.id)
        items.append(current)
        current = db.get(PlatformModel, current.fallback_model_id) if current.fallback_model_id else None
    return items, False


def validate_fallback(
    db: Session,
    *,
    model_id: UUID | None,
    fallback_model_id: UUID | None,
    model_type: str,
    region: str,
) -> PlatformModel | None:
    """PLT-090: a degradation target is another model of the same type and region, still serving, without cycles."""
    if fallback_model_id is None:
        return None
    if fallback_model_id == model_id:
        raise ApiError(422, "model.fallback_self", "模型不能降级到自身")
    fallback = db.get(PlatformModel, fallback_model_id, with_for_update=True)  # serialises with a concurrent delete of the target
    if fallback is None:
        raise ApiError(422, "model.fallback_missing", "降级模型不存在")
    if fallback.model_type != model_type or fallback.region != region:
        raise ApiError(422, "model.fallback_region_type", "降级模型必须与当前模型类型和区域一致")
    if fallback.status == "disabled":
        raise ApiError(422, "model.fallback_disabled", "降级模型已停用，请选择其他模型")
    chain, cyclic = _chain(db, fallback)
    if model_id is not None and any(item.id == model_id for item in chain):
        raise ApiError(422, "model.fallback_cycle", "降级模型不能形成循环引用")
    if cyclic:
        raise ApiError(409, "model.fallback_cycle_existing", "现有模型降级关系已存在循环")
    return fallback


def gateway_fallback_alias(db: Session, model: PlatformModel) -> str | None:
    """The alias LiteLLM should fall back to (first serving model in the degradation chain)."""
    chain, _ = _chain(db, model)
    for item in chain[1:]:
        if item.status != "disabled":
            return model_alias(item.id)
    return None


def model_state(db: Session, model_id: UUID | str | None) -> ModelState:
    """A disabled or deleted model, and the name of the model that will serve in its place."""
    try:
        parsed = UUID(str(model_id)) if model_id else None
    except ValueError:
        return ModelState(unavailable=True)
    model = db.get(PlatformModel, parsed) if parsed else None
    if model is None:
        return ModelState(unavailable=True)
    if model.status != "disabled":
        return ModelState()
    chain, _ = _chain(db, model)
    serving = next((item for item in chain[1:] if item.status != "disabled"), None)
    return ModelState(
        unavailable=True,
        fallback_name=serving.display_name if serving else None,
        disabled_name=model.display_name,
        fallback_id=serving.id if serving else None,
    )


def model_states(db: Session, model_ids) -> dict[UUID, ModelState]:
    """Batch version of ``model_state``: one query for the catalog, chains walked in memory.

    The model-options endpoint lists every enabled model of an environment; per-option chain
    walks would be N+1 queries (review fix). The catalog is a small platform table, so loading
    it once is cheaper than one round trip per option.
    """
    wanted: list[UUID] = []
    for raw in model_ids:
        try:
            wanted.append(UUID(str(raw)))
        except ValueError:
            continue
    by_id = {model.id: model for model in db.scalars(select(PlatformModel))}
    result: dict[UUID, ModelState] = {}
    for model_id in wanted:
        model = by_id.get(model_id)
        if model is None:
            result[model_id] = ModelState(unavailable=True)
            continue
        if model.status != "disabled":
            result[model_id] = ModelState()
            continue
        successor = _serving_successor(by_id, model)
        result[model_id] = ModelState(
            unavailable=True,
            fallback_name=successor.display_name if successor else None,
            disabled_name=model.display_name,
            fallback_id=successor.id if successor else None,
        )
    return result


def _serving_successor(by_id: dict[UUID, PlatformModel], model: PlatformModel) -> PlatformModel | None:
    """The first serving model after ``model`` in its degradation chain, from an in-memory map."""
    seen = {model.id}
    current = by_id.get(model.fallback_model_id) if model.fallback_model_id else None
    while current is not None and current.id not in seen:
        if current.status != "disabled":
            return current
        seen.add(current.id)
        current = by_id.get(current.fallback_model_id) if current.fallback_model_id else None
    return None


def resolve_runtime_model(db: Session, alias: str) -> RuntimeModel:
    """Resolve a frozen DSL alias against the *current* catalog (PLT-090 / PLT-092).

    A disabled model is replaced outright — its deployment no longer exists in the gateway, so a
    router fallback alone cannot save the request. A serving model keeps its alias and gets one
    request-level fallback, which also covers a gateway restart that lost the router setting.
    A deleted model has nothing to switch to (its chain is gone) and no fallback has happened,
    so nothing is recorded — the request fails at the gateway (review fix: no fake degradation).
    """
    model_id = model_id_from_alias(alias)
    if model_id is None:
        return RuntimeModel(alias=alias)
    model = db.get(PlatformModel, model_id)
    if model is None:
        return RuntimeModel(alias=alias)
    chain, _ = _chain(db, model)
    serving = [item for item in chain if item.status != "disabled"]
    if model.status != "disabled":
        next_alias = model_alias(serving[1].id) if len(serving) > 1 else None
        return RuntimeModel(alias=alias, fallbacks=[next_alias] if next_alias else [], effective_name=model.display_name)
    if not serving:
        return RuntimeModel(alias=alias)  # nothing to switch to: no degradation happened, so none is recorded
    primary = serving[0]
    next_alias = model_alias(serving[1].id) if len(serving) > 1 else None
    return RuntimeModel(
        alias=model_alias(primary.id),
        fallbacks=[next_alias] if next_alias else [],
        switched_from=model.display_name,
        effective_name=primary.display_name,
    )


def tenant_may_use(db: Session, tenant_id: UUID, model: PlatformModel) -> bool:
    """Whether the tenant's plan / allowlist entitles it to the model (the rule of the tenant model list). A degradation target
    outside it must not be added to the tenant's gateway scope."""
    tenant = db.get(Tenant, tenant_id)
    if tenant is None or model.region != tenant.region:
        return False
    if model.availability == "all_plans":
        return True
    if model.availability == "professional_plus":
        return tenant.plan in {"professional", "enterprise"}
    if model.availability == "enterprise":
        return tenant.plan == "enterprise"
    if model.availability == "allowlist":
        return db.scalar(
            select(PlatformModelTenant.model_id).where(PlatformModelTenant.model_id == model.id, PlatformModelTenant.tenant_id == tenant_id)
        ) is not None
    return False


def models_referencing_as_fallback(db: Session, model_id: UUID) -> list[PlatformModel]:
    """Catalog entries that name this model as their degradation target (direct referencers)."""
    return list(
        db.scalars(select(PlatformModel).where(PlatformModel.fallback_model_id == model_id))
    )


def models_whose_chain_includes(db: Session, model_id: UUID) -> list[PlatformModel]:
    """Every catalog entry whose degradation chain runs through this model, directly or via
    another hop: its gateway fallback projection and its tenants' team scopes depend on this
    model's state, so both must be re-synced when it changes (review fix 4)."""
    found: list[PlatformModel] = []
    seen: set[UUID] = {model_id}
    frontier: list[UUID] = [model_id]
    while frontier:
        rows = list(
            db.scalars(select(PlatformModel).where(PlatformModel.fallback_model_id.in_(frontier)))
        )
        frontier = []
        for row in rows:
            if row.id in seen:  # a legacy cycle must not loop here either
                continue
            seen.add(row.id)
            found.append(row)
            frontier.append(row.id)
    return found


def delete_blockers(db: Session, model_id: UUID) -> str | None:
    """PLT-093: an enabled model can only be disabled, never deleted."""
    if db.scalar(
        select(TenantModelSelection.id)
        .where(TenantModelSelection.model_id == model_id)
        .limit(1)
    ) is not None:
        return "有租户已启用该模型，只能停用，不能删除"
    if db.scalar(
        select(PlatformModel.id).where(PlatformModel.fallback_model_id == model_id).limit(1)
    ) is not None:
        return "该模型被其他模型设为降级模型，请先调整降级配置"
    return None
