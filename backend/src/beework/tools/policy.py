"""The execution policy of a tool call and the circuit breaker (T-165, 演进 PRD 23.2, NFR-009).

One place decides, for every source (MCP, API, Dify connections, Skill, sub-agents), how long a call may take, whether and how a
failed call is repeated, and when a tool that keeps failing is left alone for a while:

* **Policy** — the registry entry of the tool (T-160) over the platform default (``settings.tool_*``). A tenant-wide default has no
  storage yet; the resolution leaves the place for it.
* **Retry** — only calls that are safe to repeat: a read-only MCP / API tool on any transient failure; a write only when its source
  declares it idempotent *and* it carries the executor's idempotency key; a call whose effect is not known to be repeatable
  (Dify applications, Skill, sub-agents) only when the request provably never reached the other side. Nothing else is retried.
* **Circuit breaker** — consecutive transient failures of one tool open its circuit; calls are then refused at once
  (``tool.circuit_open``) until a single probe after the open period succeeds. The state lives in Valkey (shared by every instance)
  and falls back to the process when Valkey is down.
"""

import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

import redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework import capability_registry as registry
from beework.config import settings
from beework.models.capability_registry import CapabilityEntry

if TYPE_CHECKING:
    from beework.tools.executor import ToolSpec

logger = logging.getLogger(__name__)

CATEGORIES = ("network", "timeout", "5xx", "429")
MAX_ATTEMPTS = 5
MAX_BACKOFF_SECONDS = 30.0
UNDELIVERED = {"tool.unreachable", "tool.unresolvable"}  # the request never reached the other side
TRANSIENT_CODES = {"tool.timeout": "timeout", "tool.unreachable": "network", "tool.unresolvable": "network", "tool.rate_limited": "429"}


@dataclass(frozen=True)
class Policy:
    timeout_ms: int
    max_attempts: int
    backoff_ms: int
    retry_on: frozenset[str]
    source: str  # entry | platform
    entry_id: UUID | None = None


def entry_for_tool(db: Session, tenant_id: UUID, name: str) -> CapabilityEntry | None:
    """The registry entry behind an executor tool name (``mcp:{source}:{tool}``, ``connection:{id}`` …)."""
    kind, _, rest = name.partition(":")
    source_raw, _, identifier = rest.partition(":")
    try:
        source = UUID(source_raw)
    except ValueError:
        return None
    statement = select(CapabilityEntry).where(
        CapabilityEntry.tenant_id == tenant_id, CapabilityEntry.source_id == source, CapabilityEntry.removed_at.is_(None),
        CapabilityEntry.identifier == (identifier if kind in ("mcp", "api") else ""),
    )
    return db.scalar(statement.limit(1))


def _platform_retry_on() -> frozenset[str]:
    return frozenset(c.strip() for c in settings.tool_retry_on.split(",") if c.strip() in CATEGORIES)


def resolve(entry: CapabilityEntry | None) -> Policy:
    if entry is None:
        return Policy(settings.tool_timeout_ms, max(1, settings.tool_max_attempts), max(0, settings.tool_backoff_ms), _platform_retry_on(), "platform")
    raw = registry.resolve_policy(entry)
    own = bool(entry.timeout_ms or entry.retry)
    # An entry that sets its retry rules decides the categories itself — an empty list means "retry nothing", not "use the default".
    retry_on = frozenset(c for c in raw.get("retry_on") or [] if c in CATEGORIES) if entry.retry and "retry_on" in entry.retry else _platform_retry_on()
    return Policy(
        int(raw["timeout_ms"]), min(MAX_ATTEMPTS, max(1, int(raw["max_attempts"]))), max(0, int(raw["backoff_ms"])), retry_on,
        "entry" if own else "platform", entry.id,
    )


def category_of(exc: BaseException) -> str | None:
    """Which kind of transient failure an error is, or ``None`` when repeating cannot help (a bad key, bad arguments, an upstream 4xx)."""
    code = str(getattr(exc, "code", "") or "")
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        if status == 429:
            return "429"
        if status >= 500:
            return "5xx"
        return None
    return TRANSIENT_CODES.get(code)


def retry_allowed(spec: "ToolSpec", key: str | None, exc: BaseException) -> bool:
    """May this failure be answered by sending the call again? Reads: yes (MCP, API). A write: only an idempotent one with a key.
    Anything else with unknown effects: only when the request never arrived."""
    code = str(getattr(exc, "code", "") or "")
    if spec.writes:
        return bool(spec.idempotent and key)
    if spec.source in ("mcp", "api"):
        return True
    return code in UNDELIVERED


def delay_seconds(policy: Policy, attempt: int, exc: BaseException) -> float:
    """Exponential backoff with jitter, or what the upstream asked for with ``Retry-After`` (capped)."""
    asked = getattr(exc, "retry_after", None)
    if isinstance(asked, int | float) and asked > 0:
        return min(float(asked), MAX_BACKOFF_SECONDS)
    base = min(policy.backoff_ms / 1000 * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS)
    return base * (0.5 + random.random() * 0.5)  # noqa: S311 — jitter, not security


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BreakerConfig:
    failures: int
    window_seconds: int
    open_seconds: int


def breaker_config() -> BreakerConfig:
    return BreakerConfig(max(1, settings.tool_breaker_failures), max(1, settings.tool_breaker_window_seconds), max(1, settings.tool_breaker_open_seconds))


class BreakerStore(Protocol):
    def admit(self, key: str, cfg: BreakerConfig) -> tuple[bool, str, int]:
        """(allowed, state, seconds until a probe is possible). ``state``: closed | half_open (this call is the probe) | open."""

    def success(self, key: str) -> None: ...

    def release(self, key: str) -> None:
        """Give the probe slot back without judging the tool."""

    def failure(self, key: str, cfg: BreakerConfig) -> bool:
        """Count a transient failure; ``True`` when this one opened the circuit."""

    def is_open(self, key: str) -> bool: ...

    def are_open(self, keys: list[str]) -> list[bool]: ...


class MemoryBreaker:
    """Per process; also the fallback while Valkey is down. ``clock`` is injectable for tests."""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, tuple[int, float]] = {}  # count, last failure
        self._open_until: dict[str, float] = {}
        self._tripped: set[str] = set()
        self._probing: dict[str, float] = {}

    def admit(self, key: str, cfg: BreakerConfig) -> tuple[bool, str, int]:
        now = self._clock()
        with self._lock:
            until = self._open_until.get(key, 0.0)
            if until > now:
                return False, "open", max(1, int(until - now + 0.999))
            if key not in self._tripped:
                return True, "closed", 0
            probing = self._probing.get(key, 0.0)
            if probing > now:  # another call is the probe
                return False, "open", max(1, int(probing - now + 0.999))
            self._probing[key] = now + cfg.open_seconds
            return True, "half_open", 0

    def release(self, key: str) -> None:
        with self._lock:
            self._probing.pop(key, None)

    def success(self, key: str) -> None:
        with self._lock:
            for table in (self._failures, self._open_until, self._probing):
                table.pop(key, None)
            self._tripped.discard(key)

    def failure(self, key: str, cfg: BreakerConfig) -> bool:
        now = self._clock()
        with self._lock:
            count, last = self._failures.get(key, (0, now))
            count = 1 if now - last > cfg.window_seconds else count + 1
            self._failures[key] = (count, now)
            if key in self._tripped or count >= cfg.failures:  # a failed probe, or the threshold: (re)open
                newly = key not in self._tripped
                self._tripped.add(key)
                self._open_until[key] = now + cfg.open_seconds
                self._probing.pop(key, None)
                return newly
            return False

    def is_open(self, key: str) -> bool:
        with self._lock:
            return key in self._tripped

    def are_open(self, keys: list[str]) -> list[bool]:
        with self._lock:
            return [k in self._tripped for k in keys]


class ValkeyBreaker:
    def __init__(self, client: redis.Redis | None = None) -> None:
        self._client = client or redis.Redis.from_url(settings.valkey_url, socket_timeout=1, socket_connect_timeout=1)

    def admit(self, key: str, cfg: BreakerConfig) -> tuple[bool, str, int]:
        c = self._client
        ttl, tripped = c.pipeline(transaction=False).pttl(f"{key}:open").exists(f"{key}:tripped").execute()  # one round trip
        if ttl and ttl > 0:
            return False, "open", max(1, int(ttl / 1000 + 0.999))
        if not tripped:
            return True, "closed", 0
        if c.set(f"{key}:probe", "1", nx=True, ex=cfg.open_seconds):
            return True, "half_open", 0
        probe_ttl = c.pttl(f"{key}:probe")
        return False, "open", max(1, int((probe_ttl if probe_ttl and probe_ttl > 0 else cfg.open_seconds * 1000) / 1000 + 0.999))

    def release(self, key: str) -> None:
        self._client.delete(f"{key}:probe")

    def success(self, key: str) -> None:
        self._client.delete(f"{key}:count", f"{key}:open", f"{key}:tripped", f"{key}:probe")

    def failure(self, key: str, cfg: BreakerConfig) -> bool:
        c = self._client
        was_tripped, count, _ = c.pipeline(transaction=True).exists(f"{key}:tripped").incr(f"{key}:count").expire(f"{key}:count", cfg.window_seconds).execute()
        tripped = bool(was_tripped)
        if tripped or int(count) >= cfg.failures:
            c.set(f"{key}:tripped", "1", ex=86400)
            c.set(f"{key}:open", "1", ex=cfg.open_seconds)
            c.delete(f"{key}:probe")
            return not tripped
        return False

    def is_open(self, key: str) -> bool:
        return bool(self._client.exists(f"{key}:tripped"))

    def are_open(self, keys: list[str]) -> list[bool]:
        if not keys:
            return []
        pipe = self._client.pipeline(transaction=False)
        for key in keys:
            pipe.exists(f"{key}:tripped")
        return [bool(v) for v in pipe.execute()]


class Breaker:
    """Valkey first; the process when Valkey cannot be reached (the circuit then protects only this instance, which is better than not at all)."""

    RETRY_VALKEY_AFTER = 10.0  # seconds a failed Valkey is left alone: a list of 200 entries must not wait 200 times

    def __init__(self, valkey: BreakerStore | None = None, memory: BreakerStore | None = None) -> None:
        self._valkey, self._memory = valkey, memory or MemoryBreaker()
        self._down_until = 0.0

    def _store(self) -> BreakerStore:
        if time.monotonic() < self._down_until:
            return self._memory
        if self._valkey is None:
            try:
                self._valkey = ValkeyBreaker()
            except (redis.RedisError, OSError):
                return self._memory
        return self._valkey

    def _call(self, name: str, *args):
        try:
            return getattr(self._store(), name)(*args)
        except (redis.RedisError, OSError):
            logger.warning("Circuit breaker store unavailable; using the process", exc_info=False)
            self._down_until = time.monotonic() + self.RETRY_VALKEY_AFTER
            return getattr(self._memory, name)(*args)

    def admit(self, key: str, cfg: BreakerConfig) -> tuple[bool, str, int]:
        return self._call("admit", key, cfg)

    def release(self, key: str) -> None:
        self._call("release", key)

    def success(self, key: str) -> None:
        self._call("success", key)

    def failure(self, key: str, cfg: BreakerConfig) -> bool:
        return self._call("failure", key, cfg)

    def is_open(self, key: str) -> bool:
        return self._call("is_open", key)

    def are_open(self, keys: list[str]) -> list[bool]:
        return self._call("are_open", keys)


BREAKER = Breaker()


def breaker_key(tenant_id: UUID, name: str) -> str:
    return f"bw:cb:{tenant_id}:{name}"
