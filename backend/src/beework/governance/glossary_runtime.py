"""The assistant's glossary at run time (T-057, OC-030–037).

The effective terms of the assistant (industry glossaries → tenant → assistant, ``term_inheritance``) form a dictionary.
Matching a query against it is deterministic — no model: every term whose standard name or alias appears in the query
adds its other names as synonyms for retrieval, and its definition goes into the prompt.
"""

import re
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.orm import Session

from beework.governance.tenant_glossary import _tenant_term, industry_layer, tenant_rows
from beework.governance.term_inheritance import Term, TermView, assistant_terms
from beework.runtime.oc import GlossaryConfig

MAX_SYNONYMS = 20
MAX_MATCHES = 10


@dataclass(frozen=True)
class DictEntry:
    term: str
    aliases: tuple[str, ...]
    definition: str

    @property
    def names(self) -> tuple[str, ...]:
        return (self.term, *self.aliases)


@dataclass
class Expansion:
    synonyms: list[str] = field(default_factory=list)  # names to add to the retrieval query
    matches: list[DictEntry] = field(default_factory=list)  # entries whose definition goes into the prompt


def assistant_layer(cfg: GlossaryConfig) -> list[Term]:
    return [
        Term(id=t.id, layer="assistant", term=t.term, aliases=tuple(t.aliases), definition=t.definition, from_id=t.from_id, hidden=t.hidden)
        for t in cfg.terms
    ]


def assistant_view(db: Session, tenant_id: UUID, cfg: GlossaryConfig) -> tuple[TermView, dict[str, int | list[str]]]:
    """The assistant's terms with every layer folded in, plus the chain numbers of the page header (OC-031)."""
    from beework.governance.tenant_glossary import summary

    industry = industry_layer(db, tenant_id)
    tenant = [_tenant_term(row) for row in tenant_rows(db, tenant_id)]
    view = assistant_terms(industry, tenant, assistant_layer(cfg), inherit_tenant=cfg.use_tenant)
    tenant_summary = summary(db, tenant_id)
    own = [t for t in cfg.terms if t.from_id is None]
    chain = {
        "industries": tenant_summary["industries"], "industry_terms": tenant_summary["industry_terms"],
        "tenant_total": tenant_summary["total"], "tenant_level": tenant_summary["tenant_level"],
        "assistant_inherited": sum(1 for item in view.terms if item.label not in ("assistant",)),
        "assistant_level": len([t for t in cfg.terms if not t.hidden]), "assistant_own": len(own), "hidden": len(view.hidden),
        "effective": len(view.terms),
    }
    return view, chain


def dictionary(db: Session, tenant_id: UUID, cfg: GlossaryConfig) -> list[DictEntry]:
    view, _ = assistant_view(db, tenant_id, cfg)
    return [DictEntry(item.term.term, tuple(item.term.aliases), item.term.definition) for item in view.terms]


def _occurs(name: str, text: str) -> bool:
    if not name:
        return False
    if re.fullmatch(r"[A-Za-z0-9 ._\-]+", name):  # a Latin term must not match inside a longer word
        return re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text, re.IGNORECASE) is not None
    return name.casefold() in text.casefold()


def expand(query: str, entries: list[DictEntry]) -> Expansion:
    """Deterministic alias expansion: the other names of every term the query mentions, and the terms themselves."""
    result = Expansion()
    for entry in entries:
        if not any(_occurs(name, query) for name in entry.names):
            continue
        if len(result.matches) < MAX_MATCHES:
            result.matches.append(entry)
        for name in entry.names:
            if len(result.synonyms) >= MAX_SYNONYMS:
                break
            if not _occurs(name, query) and name not in result.synonyms:
                result.synonyms.append(name)
    return result


def retrieval_query(query: str, synonyms: list[str]) -> str:
    return f"{query} {' '.join(synonyms)}" if synonyms else query


def prompt_section(matches: list[DictEntry]) -> str:
    """The prompt block that gives the model the definitions of the terms the question uses (OC-037)."""
    lines = []
    for entry in matches:
        extra = f"（别名：{'、'.join(entry.aliases)}）" if entry.aliases else ""
        lines.append(f"- {entry.term}{extra}：{entry.definition}" if entry.definition else f"- {entry.term}{extra}")
    return "# 术语\n" + "\n".join(lines) if lines else ""
