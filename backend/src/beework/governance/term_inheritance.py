"""Inheritance for glossary terms (PRD 5.3, OC-030–036, SET-060–063). A pure module: no database.

Industry glossaries (a tenant picks several, in an order) → tenant → assistant. Same rules as for PII rules — an edit on
a lower layer creates an override whose ``from_id`` names the inherited term — plus *hiding*: deleting an inherited term
records a hidden marker on the layer, and “restore all” removes the markers. A standard term that appears in two
industry glossaries is inherited once: the glossary that comes first in the tenant's selection wins.
"""

from dataclasses import dataclass, field, replace
from typing import Literal

TermLabel = Literal["industry_inherited", "tenant", "tenant_modified", "assistant", "assistant_modified"]


@dataclass(frozen=True)
class Term:
    id: str
    layer: Literal["industry", "tenant", "assistant"]
    term: str
    aliases: tuple[str, ...] = ()
    definition: str = ""
    from_id: str | None = None  # the inherited term this row overrides or hides
    hidden: bool = False  # a marker: the layer does not want the inherited term
    source: str = ""  # industry layer: the glossary it comes from

    @property
    def key(self) -> str:
        return self.term.strip().lower()


@dataclass(frozen=True)
class EffectiveTerm:
    id: str  # the inherited term's id, or the layer's own term id: what a caller edits / restores / deletes
    term: Term  # whose values apply
    label: TermLabel
    override_id: str | None = None  # the viewing layer's override row (restorable)
    layers: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TermView:
    terms: list[EffectiveTerm]
    hidden: list[Term]  # inherited terms the viewing layer deleted (for “本层已删除 N 条继承术语”)


def industry_terms(glossaries: list[list[Term]]) -> list[Term]:
    """Terms of the selected glossaries, in selection order, each standard term once (first glossary wins)."""
    seen: set[str] = set()
    out: list[Term] = []
    for glossary in glossaries:
        for term in glossary:
            if term.key in seen:
                continue
            seen.add(term.key)
            out.append(term)
    return out


def _layer(
    inherited: list[EffectiveTerm], rows: list[Term], own_label: TermLabel, modified_label: TermLabel, layer: str
) -> TermView:
    by_origin = {row.from_id: row for row in rows if row.from_id is not None}
    result: list[EffectiveTerm] = []
    hidden: list[Term] = []
    for item in inherited:
        row = by_origin.get(item.id)
        if row is None:
            result.append(item)
        elif row.hidden:
            hidden.append(item.term)
        else:
            result.append(replace(item, term=row, label=modified_label, override_id=row.id, layers=(*item.layers, layer)))
    for row in rows:
        if row.from_id is None:
            result.append(EffectiveTerm(id=row.id, term=row, label=own_label, layers=(layer,)))
    return TermView(result, hidden)


def tenant_terms(industry: list[Term], tenant: list[Term]) -> TermView:
    """The tenant's view: inherited industry terms with its overrides, hidden markers and own terms."""
    inherited = [EffectiveTerm(id=term.id, term=term, label="industry_inherited", layers=("industry",)) for term in industry]
    return _layer(inherited, tenant, "tenant", "tenant_modified", "tenant")


def assistant_terms(industry: list[Term], tenant: list[Term], assistant: list[Term], *, inherit_tenant: bool = True) -> TermView:
    """The assistant's view; without ``inherit_tenant`` the assistant starts from nothing (OC-032)."""
    base = tenant_terms(industry, tenant).terms if inherit_tenant else []
    return _layer(base, assistant, "assistant", "assistant_modified", "assistant")
