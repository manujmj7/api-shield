"""redis_store.py - Redis data store abstraction and atomic rate-limiting primitives.

Why this module exists:
This is the ONLY module in the system allowed to touch Redis. Isolating Redis logic
here decouples business rules from database queries, enables mock injection for unit tests,
and guarantees atomic operations via Lua scripts so counters and TTLs never desynchronize.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable, Tuple, TypeVar

import redis

from errors import StoreUnavailableError

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def _handle_redis_errors(func: F) -> F:
    """Decorator to catch redis.RedisError and re-raise as StoreUnavailableError.

    Why:
    Higher-level controllers should not depend directly on low-level redis-py exceptions.
    Wrapping client errors in StoreUnavailableError allows clean fail-open/closed policies.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except redis.RedisError as exc:
            logger.error("Redis operation failed in %s: %s", func.__name__, exc)
            raise StoreUnavailableError(f"Redis store unavailable: {exc}") from exc

    return wrapper  # type: ignore[return-value]


# Lua script for atomic Fixed Window hit
# Fixes the SRS race where a crash between INCR and EXPIRE could leave an immortal key
FIXED_WINDOW_LUA = """
local key = KEYS[1]
local window = tonumber(ARGV[1])

local count = redis.call('INCR', key)
if count == 1 then
    redis.call('EXPIRE', key, window)
end

local ttl = redis.call('TTL', key)
if ttl < 0 then
    redis.call('EXPIRE', key, window)
    ttl = window
end

return {count, ttl}
"""

# Lua script for atomic Sliding Window hit
# Prunes expired entries, counts remaining, inserts only if below limit, sets TTL,
# and returns the oldest entry's timestamp for accurate Retry-After computation.
SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

local clear_before = now - window
redis.call('ZREMRANGEBYSCORE', key, '-inf', clear_before)

local current_count = redis.call('ZCARD', key)
local allowed = 0
if current_count < limit then
    redis.call('ZADD', key, now, member)
    current_count = current_count + 1
    allowed = 1
end

redis.call('EXPIRE', key, math.ceil(window + 1))

local oldest_ts = now
local oldest_arr = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
if oldest_arr and #oldest_arr >= 2 then
    oldest_ts = tonumber(oldest_arr[2])
end

return {allowed, current_count, tostring(oldest_ts)}
"""


class RedisStore:
    """Encapsulates all Redis interactions with atomic script guarantees."""

    def __init__(self, client: redis.Redis) -> None:
        """Initialize RedisStore with an injected redis-py client.

        Why:
        Dependency injection allows injecting fakeredis in tests or production redis
        without changing application code.
        """
        self.client = client
        self._fixed_window_script = self.client.register_script(FIXED_WINDOW_LUA)
        self._sliding_window_script = self.client.register_script(SLIDING_WINDOW_LUA)

    @_handle_redis_errors
    def ping(self) -> bool:
        """Check if Redis server is reachable."""
        return bool(self.client.ping())

    @_handle_redis_errors
    def get_ttl(self, key: str) -> int:
        """Get the time-to-live of a given key in seconds."""
        ttl = self.client.ttl(key)
        return int(ttl) if ttl is not None else -1

    @_handle_redis_errors
    def is_blocked(self, client_id: str) -> bool:
        """Check if client has an active temporary block key (block:<client>)."""
        key = f"block:{client_id}"
        return bool(self.client.exists(key))

    @_handle_redis_errors
    def get_block_ttl(self, client_id: str) -> int:
        """Get remaining seconds on client block, or 0 if not blocked."""
        key = f"block:{client_id}"
        ttl = self.client.ttl(key)
        return max(0, int(ttl)) if ttl and ttl > 0 else 0

    @_handle_redis_errors
    def block(self, client_id: str, seconds: int) -> None:
        """Set a temporary block flag with expiry for a client (block:<client>).

        Why:
        Storing the block key directly with an expiry ensures automatic unblocking
        by Redis without requiring a background cleanup worker.
        """
        key = f"block:{client_id}"
        self.client.set(key, "1", ex=max(1, int(seconds)))

    @_handle_redis_errors
    def unblock(self, client_id: str) -> bool:
        """Remove temporary block key for a client."""
        key = f"block:{client_id}"
        return bool(self.client.delete(key))

    @_handle_redis_errors
    def incr_violation(self, client_id: str, ttl: int) -> int:
        """Increment client violation counter and maintain retention TTL (violation:<client>).

        Why:
        Violations accumulate across multiple rate limit hits. Applying a retention TTL
        (e.g. 24h) resets historical bad behavior for reformed clients.
        """
        key = f"violation:{client_id}"
        count = int(self.client.incr(key))
        if count == 1:
            self.client.expire(key, max(1, int(ttl)))
        else:
            # Refresh or guarantee expiry exists
            if self.client.ttl(key) < 0:
                self.client.expire(key, max(1, int(ttl)))
        return count

    @_handle_redis_errors
    def get_violations(self, client_id: str) -> int:
        """Read the current violation counter for a client."""
        key = f"violation:{client_id}"
        val = self.client.get(key)
        if val is None:
            return 0
        try:
            return int(val)
        except (ValueError, TypeError):
            return 0

    @_handle_redis_errors
    def fixed_window_hit(self, key: str, window: int) -> Tuple[int, int]:
        """Atomically record a request in a fixed window and retrieve current count and TTL.

        Args:
            key: Full Redis key formatted as 'rate:<client>:<endpoint>:<window_number>'
            window: Window duration in seconds.

        Returns:
            Tuple of (current_count, remaining_ttl)
        """
        res = self._fixed_window_script(keys=[key], args=[window])
        return int(res[0]), int(res[1])

    @_handle_redis_errors
    def sliding_window_hit(
        self,
        key: str,
        now: float,
        window: int,
        limit: int,
        member: str,
    ) -> Tuple[bool, int, float]:
        """Atomically evaluate a sliding window request using a Redis Sorted Set.

        Args:
            key: Full Redis key formatted as 'slide:<client>:<endpoint>'
            now: Current epoch timestamp in seconds.
            window: Rolling window duration in seconds.
            limit: Maximum allowed requests within the window.
            member: Unique request identifier string (e.g. 'timestamp:nonce').

        Returns:
            Tuple of (allowed: bool, count: int, oldest_timestamp: float)
        """
        res = self._sliding_window_script(keys=[key], args=[now, window, limit, member])
        allowed = bool(int(res[0]))
        count = int(res[1])
        oldest_ts = float(res[2])
        return allowed, count, oldest_ts
