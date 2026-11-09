"""Multi-layer inheritance with per-layer overrides for PII rules (PRD 5.3, OC-012). A pure module: no database.

Layers are platform → tenant → assistant. A lower layer never edits an upper rule; editing or switching an inherited
rule *creates an override* on the lower layer (``from_id`` points at the rule it overrides, ``None`` means a rule of
the layer's own). Enforced platform rules cannot be switched off below the platform, only their content can change.
The terms engine (T-053) and the assistant layer (T-055) reuse the same types.
"""

from dataclasses import dataclass, field, replace
from typing import Literal

Layer = Literal["platform", "tenant", "assistant"]
# How the viewing layer sees a rule (OC-010 / SET-052): inherited as-is, or a rule of / modified on a layer.
Label = Literal["platform_inherited", "tenant", "tenant_modified", "assistant", "assistant_modified"]


@dataclass(frozen=True)
class Rule:
    """One stored rule at one layer. ``id`` is the row's own id; ``from_id`` the overridden upper-layer rule's origin id."""

    id: str
    layer: Layer
    name: str
    match_type: str = "regex"
    pattern: str = ""
    stage: str = "both"
    action: str = "mask"
    block_message: str = ""
    enforced: bool = False
    enabled: bool = True
    from_id: str | None = None

    @property
    def origin_id(self) -> str:
        """The id every layer's view of this rule is keyed by: the platform / own rule an override points at."""
        return self.from_id or self.id


@dataclass(frozen=True)
class Switches:
    inherit_platform: bool = True  # tenant switch: inherit all platform rules (SET-051); off keeps only enforced ones
    use_global: bool = True  # assistant switch: reference the tenant's effective rules (OC-010); off keeps only enforced
    pii_detection: bool = True  # assistant switch: off disables everything that is not enforced


@dataclass(frozen=True)
class EffectiveRule:
    key: str  # the origin id: what a caller edits / restores
    rule: Rule  # the row whose values apply
    label: Label
    enabled: bool  # after enforcement and the PII detection switch
    enforced: bool
    override_id: str | None  # the viewing layer's override row, if any (restorable)
    switched_off: bool = False  # disabled only because PII detection is off
    layers: tuple[str, ...] = field(default_factory=tuple)  # the layers that contributed, bottom-up


def _apply_enforcement(rule: Rule, base: Rule | None) -> tuple[bool, bool]:
    """(enabled, enforced) of a rule: an enforced platform rule is always on and stays enforced, whatever an override says.

    The platform may switch its own enforced rule off for a while (PLT-131); that decision binds every layer below.
    """
    if base is not None and base.enforced:
        return base.enabled, True
    return rule.enabled, rule.enforced


def effective_rules(
    platform: list[Rule],
    tenant: list[Rule],
    assistant: list[Rule] | None = None,
    switches: Switches | None = None,
) -> list[EffectiveRule]:
    """The rules in force at the deepest layer given, following OC-012 step by step."""
    switches = switches or Switches()
    # 1. Platform rules: all of them when the tenant inherits, otherwise only the enforced ones.
    layer_one = [rule for rule in platform if switches.inherit_platform or rule.enforced]

    # 2. Tenant layer: an override replaces the platform rule, own rules are added.
    tenant_overrides = {rule.from_id: rule for rule in tenant if rule.from_id is not None}
    stage_two: list[EffectiveRule] = []
    for rule in layer_one:
        override = tenant_overrides.get(rule.id)
        enabled, enforced = _apply_enforcement(override or rule, rule)
        stage_two.append(
            EffectiveRule(
                key=rule.id, rule=override or rule, label="tenant_modified" if override else "platform_inherited",
                enabled=enabled, enforced=enforced, override_id=override.id if override else None,
                layers=("platform", "tenant") if override else ("platform",),
            )
        )
    for rule in tenant:
        if rule.from_id is None:
            stage_two.append(
                EffectiveRule(key=rule.id, rule=rule, label="tenant", enabled=rule.enabled, enforced=False, override_id=None, layers=("tenant",))
            )
    result = stage_two

    # 3. Assistant layer: reference the tenant's rules (or only the enforced ones), then apply the assistant's overrides.
    if assistant is not None:
        base = [item for item in stage_two if switches.use_global or item.enforced]
        overrides = {rule.from_id: rule for rule in assistant if rule.from_id is not None}
        result = []
        for item in base:
            override = overrides.get(item.key)
            if override is None:
                result.append(item)
                continue
            # An enforced rule stays on (or off, if the platform switched it off) whatever the assistant's override says.
            result.append(
                replace(
                    item, rule=override, label="assistant_modified", enabled=item.enabled if item.enforced else override.enabled,
                    override_id=override.id, layers=(*item.layers, "assistant"),
                )
            )
        for rule in assistant:
            if rule.from_id is None:
                result.append(
                    EffectiveRule(key=rule.id, rule=rule, label="assistant", enabled=rule.enabled, enforced=False, override_id=None, layers=("assistant",))
                )

    # 4. PII detection off: everything but the enforced rules is disabled.
    if not switches.pii_detection:
        result = [
            item if item.enforced else replace(item, enabled=False, switched_off=True)
            for item in result
        ]
    return result


def effective_count(rules: list[EffectiveRule]) -> int:
    """5. The number of rules in force."""
    return sum(1 for item in rules if item.enabled)
