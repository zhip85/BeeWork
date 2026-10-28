"""Basic personal-information redaction for stored conversation data (SET-070).

Fixed regex rules for mainland-China mobile numbers, e-mail addresses, resident ID numbers and bank
card numbers. T-055 replaces this with configurable rule sets (and optional Presidio); keep the
``redact_text`` / ``redact_data`` interface stable.
"""

import re
from typing import Any

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


def redact_data(value: Any) -> Any:
    """Redact every string inside nested dicts / lists (used to mask trace payloads)."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_data(item) for item in value)
    return value
