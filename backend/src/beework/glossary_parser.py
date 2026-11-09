"""Parser for glossary import text (PLT-124). A pure function: the tenant glossary import (T-053) reuses it.

One term per line: ``term, aliases, definition``. The first line is skipped when it starts with ``term`` / ``术语`` /
``name``; fields may be separated by an English comma, a Chinese comma or a Tab; aliases are separated by ``;``;
everything after the second separator belongs to the definition.
"""

import re
from dataclasses import dataclass, field

HEADER_WORDS = frozenset({"term", "术语", "name"})
FIELD_SPLIT = re.compile(r"[,，\t]")
ALIAS_SPLIT = re.compile(r"[;；]")
MAX_TERM = 200
MAX_ALIAS = 100
MAX_ALIASES = 20
MAX_DEFINITION = 1000
MAX_ROWS = 2000


@dataclass
class ParsedTerm:
    line: int
    term: str
    aliases: list[str]
    definition: str


@dataclass
class ParseResult:
    terms: list[ParsedTerm] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)  # {"line": int, "message": str}
    header_skipped: bool = False


def split_aliases(raw: str, term: str = "") -> list[str]:
    """Aliases of one field: ``;`` separated, trimmed, de-duplicated case-insensitively, never the term itself."""
    seen = {term.strip().lower()} if term else set()
    out: list[str] = []
    for alias in ALIAS_SPLIT.split(raw or ""):
        alias = alias.strip()
        if alias and alias.lower() not in seen:
            seen.add(alias.lower())
            out.append(alias)
    return out


def parse_terms(text: str, *, max_rows: int = MAX_ROWS) -> ParseResult:
    result = ParseResult()
    seen_terms: dict[str, int] = {}
    first = True
    for number, raw_line in enumerate((text or "").lstrip("﻿").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        parts = FIELD_SPLIT.split(line, maxsplit=2)
        if first:
            first = False
            if parts[0].strip().lower() in HEADER_WORDS:
                result.header_skipped = True
                continue
        term = parts[0].strip()
        aliases_raw = parts[1] if len(parts) > 1 else ""
        definition = parts[2].strip() if len(parts) > 2 else ""
        if not term:
            result.errors.append({"line": number, "message": "术语不能为空"})
            continue
        if len(term) > MAX_TERM:
            result.errors.append({"line": number, "message": f"术语不能超过 {MAX_TERM} 个字符"})
            continue
        aliases = split_aliases(aliases_raw, term)
        if len(aliases) > MAX_ALIASES:
            result.errors.append({"line": number, "message": f"别名最多 {MAX_ALIASES} 个"})
            continue
        if any(len(alias) > MAX_ALIAS for alias in aliases):
            result.errors.append({"line": number, "message": f"每个别名不能超过 {MAX_ALIAS} 个字符"})
            continue
        if len(definition) > MAX_DEFINITION:
            result.errors.append({"line": number, "message": f"定义不能超过 {MAX_DEFINITION} 个字符"})
            continue
        key = term.lower()
        if key in seen_terms:
            result.errors.append({"line": number, "message": f"术语与第 {seen_terms[key]} 行重复"})
            continue
        if len(result.terms) >= max_rows:
            result.errors.append({"line": number, "message": f"一次最多导入 {max_rows} 条，其余已忽略"})
            break
        seen_terms[key] = number
        result.terms.append(ParsedTerm(number, term, aliases, definition))
    return result
