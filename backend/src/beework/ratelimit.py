"""Token-bucket rate limiting for the open API and the website widget (T-028, RUL-021, PLT-084).

The bucket lives in Valkey (one Lua script, server clock) so every API instance shares it. When Valkey is not
reachable, a per-process bucket takes over: limits stay roughly enforced instead of switching off.
"""

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import redis

from beework.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after: int = 0  # whole seconds until a token is available


class BucketStore(Protocol):
    def take(self, key: str, capacity: float, rate: float) -> Decision: ...


class MemoryStore:
    """Per-process bucket; also the fallback while Valkey is down. ``clock`` is injectable for tests."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._state: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def take(self, key: str, capacity: float, rate: float) -> Decision:
        now = self._clock()
        with self._lock:
            tokens, stamp = self._state.get(key, (capacity, now))
            tokens = min(capacity, tokens + (now - stamp) * rate)
            if tokens >= 1:
                self._state[key] = (tokens - 1, now)
                return Decision(True)
            self._state[key] = (tokens, now)
            return Decision(False, max(1, math.ceil((1 - tokens) / rate)))


# KEYS[1] = bucket, ARGV = capacity, rate per second. Uses the server clock so instances never disagree.
_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = capacity; ts = now end
tokens = math.min(capacity, tokens + (now - ts) * rate)
local allowed = 0
local wait = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  wait = math.ceil((1 - tokens) / rate)
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], math.ceil(capacity / rate) + 60)
return {allowed, wait}
"""


class ValkeyStore:
    def __init__(self, client: redis.Redis | None = None) -> None:
        self._client = client or redis.Redis.from_url(settings.valkey_url, socket_timeout=1, socket_connect_timeout=1)
        self._script = self._client.register_script(_LUA)

    def take(self, key: str, capacity: float, rate: float) -> Decision:
        allowed, wait = self._script(keys=[f"bw:ratelimit:{key}"], args=[capacity, rate])
        return Decision(bool(int(allowed)), int(wait))


class Limiter:
    def __init__(
        self,
        primary: BucketStore | None = None,
        fallback: BucketStore | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        retry_primary_after: float = 10.0,
    ) -> None:
        self._primary = primary
        self._fallback = fallback or MemoryStore()
        self._clock = clock
        self._retry_after = retry_primary_after
        self._primary_down_until = 0.0

    def check(self, key: str, *, per_second: float, burst: float | None = None) -> Decision:
        """Take one token from ``key``'s bucket: ``per_second`` sustained, ``burst`` (default: one second's worth)."""
        capacity = max(1.0, burst if burst is not None else per_second)
        rate = max(per_second, 0.001)
        if self._clock() >= self._primary_down_until:
            try:
                return (self._primary or self._valkey()).take(key, capacity, rate)
            except (redis.RedisError, OSError):
                # Do not make every request wait for a connection timeout while Valkey is down: use the
                # per-process bucket and only try Valkey again after a short back-off.
                self._primary_down_until = self._clock() + self._retry_after
                logger.warning("Valkey unavailable for rate limiting; using the per-process bucket for %ss", self._retry_after)
        return self._fallback.take(key, capacity, rate)

    def _valkey(self) -> BucketStore:
        self._primary = ValkeyStore()
        return self._primary


limiter = Limiter()
