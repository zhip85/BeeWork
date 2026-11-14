"""SMS delivery (T-088): the adapter interface and the "not configured" state.

No provider is chosen yet (the dev lead picks one when a provider account exists). Until ``register_provider`` is given one, the SMS channel
reports itself as not configured: a rule cannot be created with it and the page shows it as unavailable.
"""

from typing import Protocol


class SmsProvider(Protocol):
    name: str

    def send(self, phone: str, text: str) -> None:
        """Send one text message; raise on failure (the caller records a short reason)."""


_provider: SmsProvider | None = None


def register_provider(provider: SmsProvider | None) -> None:
    global _provider
    _provider = provider


def configured() -> bool:
    return _provider is not None


def send(phone: str, text: str) -> None:
    if _provider is None:
        raise RuntimeError("短信通道未配置")
    _provider.send(phone, text)
