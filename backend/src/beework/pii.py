"""Personal-information redaction for stored conversation data (SET-070).

A tenant's stored text is redacted with the rules in force for it (platform, tenant and baseline PII rules, T-055):
``redact_for_tenant``. The fixed patterns below — mainland-China mobile numbers, e-mail addresses, resident ID
numbers and bank card numbers — remain as the floor for tenants that have no masking rule at all, and for the
memory store, which always redacts.
"""

import re
from collections.abc import Callable
from typing import Any
from uuid import UUID

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
ID_CARD = re.compile(
    r"(?<![0-9A-Za-z])[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![0-9A-Za-z])"
)
BANK_CARD = re.compile(r"(?<![0-9])\d{13,19}(?![0-9])")
PHONE = re.compile(r"(?<![0-9])(?:\+?86[- ]?)?1[3-9]\d{9}(?![0-9])")


def _luhn_valid(number: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(number)):
        digit = int(char)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def redact_text(text: str) -> str:
    """Replace personal information in ``text`` with category placeholders."""
    text = EMAIL.sub("[邮箱]", text)
    text = ID_CARD.sub("[身份证号]", text)
    text = BANK_CARD.sub(lambda m: "[银行卡号]" if _luhn_valid(m.group(0)) else m.group(0), text)
    return PHONE.sub("[手机号]", text)


def redact_data(value: Any, redactor: Callable[[str], str] | None = None) -> Any:
    """Redact every string inside nested dicts / lists (used to mask trace payloads)."""
    redactor = redactor or redact_text
    if isinstance(value, str):
        return redactor(value)
    if isinstance(value, dict):
        return {key: redact_data(item, redactor) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_data(item, redactor) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_data(item, redactor) for item in value)
    return value


def make_redactor(rules: list) -> Callable[[str], str]:
    """A text redactor for the given policy rules; without any masking rule it falls back to the fixed patterns."""
    from beework.governance.pii_engine import redact_for_logs

    local = [rule for rule in rules if rule.match_type in ("regex", "keyword") and rule.action in ("mask", "block")]
    if not local:
        return redact_text
    return lambda text: redact_for_logs(text, local)


def redact_for_tenant(db: Any, tenant_id: UUID, text: str) -> str:
    """Redact stored text with the rules in force for the tenant (the assistant layer is not known here)."""
    from beework.governance.runtime_policy import build_policy

    return make_redactor(build_policy(db, tenant_id))(text)
