"""The rules in force for one run (T-055): platform → tenant → assistant PII rules plus the security baseline.

Platform and tenant rules are read when the run starts, so an edit takes effect on the next message (PLT-133); the
assistant layer comes from the frozen version snapshot (``oc.pii``). Enforced rules ignore the assistant's PII
detection switch (PLT-141).
"""

from uuid import UUID

from sqlalchemy.orm import Session

from beework.governance import baseline_rules_for_tenant, list_platform_pii_rules
from beework.governance.inheritance import Rule, Switches, effective_rules
from beework.governance.pii_engine import PolicyRule
from beework.governance.tenant import inherits_platform, tenant_rows, to_rule
from beework.models import Tenant
from beework.runtime.oc import PiiConfig

LAYER_OF_LABEL = {
    "platform_inherited": "platform", "tenant": "tenant", "tenant_modified": "tenant",
    "assistant": "assistant", "assistant_modified": "assistant",
}


def assistant_layer(pii: PiiConfig) -> list[Rule]:
    return [
        Rule(
            id=row.id, layer="assistant", name=row.name, match_type=row.match_type, pattern=row.pattern, stage=row.stage,
            action=row.action, block_message=row.block_message, enabled=row.enabled, from_id=row.from_id,
        )
        for row in pii.rules
    ]


def build_policy(db: Session, tenant_id: UUID, pii: PiiConfig | None = None) -> list[PolicyRule]:
    """Every enabled rule that applies to the run: PII rules (three layers) and the baseline distributed to the tenant."""
    pii = pii or PiiConfig()
    platform_rows = list_platform_pii_rules(db)
    platform = [to_rule(rule, "platform") for rule in platform_rows]
    tenant = [to_rule(row, "tenant") for row in tenant_rows(db, tenant_id)]
    switches = Switches(inherit_platform=inherits_platform(db, tenant_id), use_global=pii.use_global, pii_detection=pii.enabled)
    effective = effective_rules(platform, tenant, assistant_layer(pii), switches)
    rules: list[PolicyRule] = []
    present: set[str] = set()
    for item in effective:
        if not item.enabled:
            continue
        rule = item.rule
        present.add(item.key)
        rules.append(
            PolicyRule(
                id=item.key, name=rule.name, layer=LAYER_OF_LABEL[item.label], source="pii", category="pii", match_type=rule.match_type,
                pattern=rule.pattern, stage=rule.stage, action=rule.action, block_message=rule.block_message, enforced=item.enforced,
            )
        )
    tenant_row = db.get(Tenant, tenant_id)
    if tenant_row is None:
        return rules
    for base in baseline_rules_for_tenant(db, tenant_row):
        if base.category == "data_retention" or base.stage == "data":
            continue
        if base.category == "pii":
            if base.level != "enforced":
                continue  # recommended PII rows only point at rules that are already in the set above
            for platform_rule in base.pii_rules:  # an enforced baseline row switches its referenced rules on for good
                if str(platform_rule.id) in present:
                    continue
                present.add(str(platform_rule.id))
                rules.append(
                    PolicyRule(
                        id=str(platform_rule.id), name=platform_rule.name, layer="baseline", source="baseline", category="pii",
                        match_type=platform_rule.match_type, pattern=platform_rule.pattern, stage=platform_rule.stage,
                        action=platform_rule.action, block_message=platform_rule.block_message, enforced=True,
                    )
                )
            continue
        rules.append(
            PolicyRule(
                id=f"baseline:{base.id}", name=base.name, layer="baseline", source="baseline", category=base.category,
                match_type=base.match_type, pattern=base.pattern, stage=base.stage, action=base.action,
                block_message=base.block_message, enforced=base.level == "enforced",
            )
        )
    return rules


def assistant_view(db: Session, tenant_id: UUID, pii: PiiConfig):
    """The assistant's PII rules as the OC page shows them: every layer folded together (OC-012), plus the chain numbers."""
    platform_rows = list_platform_pii_rules(db)
    platform = [to_rule(rule, "platform") for rule in platform_rows]
    own_tenant_rows = tenant_rows(db, tenant_id)
    tenant = [to_rule(row, "tenant") for row in own_tenant_rows]
    switches = Switches(inherit_platform=inherits_platform(db, tenant_id), use_global=pii.use_global, pii_detection=pii.enabled)
    effective = effective_rules(platform, tenant, assistant_layer(pii), switches)
    chain = {
        "platform_total": len(platform_rows), "platform_enforced": sum(1 for r in platform_rows if r.enforced),
        "tenant_inherited": sum(1 for i in effective_rules(platform, tenant, switches=Switches(inherit_platform=switches.inherit_platform)) if i.label != "tenant"),
        "tenant_level": len(own_tenant_rows),
        "assistant_inherited": sum(1 for i in effective if i.label in ("platform_inherited", "tenant", "tenant_modified")),
        "assistant_level": len(pii.rules), "effective": sum(1 for i in effective if i.enabled),
    }
    return effective, chain


def overview_count(db: Session, tenant_id: UUID, pii: PiiConfig) -> int:
    """3.6.2: applicable baseline rules + the tenant's enabled rules + the assistant's enabled input rules."""
    tenant = db.get(Tenant, tenant_id)
    baseline = len(baseline_rules_for_tenant(db, tenant)) if tenant is not None else 0
    tenant_level = sum(1 for row in tenant_rows(db, tenant_id) if row.enabled)
    assistant = sum(1 for row in pii.rules if row.enabled and row.stage in ("both", "input"))
    return baseline + tenant_level + assistant
