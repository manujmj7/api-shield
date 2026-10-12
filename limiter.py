"""limiter.py - Rate limiting core algorithms (Fixed Window & Sliding Window).

Why this module exists:
Separating rate limiting algorithms from HTTP delivery and persistence makes the core
logic completely testable in memory with fake clocks and fake Redis backends.
Supports both Fixed Window (simple, low overhead) and Sliding Window (smooth rolling window).
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from errors import PolicyValidationError
from policy import RateLimitRule
from redis_store import RedisStore


@dataclass(frozen=True)
class RateLimitResult:
    """Immutable evaluation result of a rate limit check.

    Why:
    Frozen dataclass ensures results cannot be modified downstream.
    Carries all data needed to construct RFC-compliant HTTP headers
    (X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset, Retry-After).
    """

    allowed: bool
    limit: int
    remaining: int
    retry_after: int
    reset_at: int


class RateLimiter:
    """Evaluates incoming requests against rate limiting policies."""

    def __init__(
        self,
        store: RedisStore,
        time_func: Callable[[], float] = time.time,
    ) -> None:
        """Initialize RateLimiter with injected RedisStore and injectable clock.

        Why:
        Injecting time_func allows deterministic unit testing across window boundaries
        and rollover scenarios without calling time.sleep().
        """
        self.store = store
        self.time_func = time_func

    def fixed_window_check(
        self,
        key_suffix: str,
        limit: int,
        window: int,
    ) -> RateLimitResult:
        """Evaluate request using Fixed Window counter.

        Fixed Window mechanics:
        - window_number = int(now // window)
        - Redis key = rate:<client>:<endpoint>:<window_number>
        - Window end = (window_number + 1) * window
        - retry_after = ceil(window_end - now) when rejected (min 1)
        - remaining = max(0, limit - count)
        """
        now = self.time_func()
        window_number = int(now // window)
        window_end = (window_number + 1) * window
        redis_key = f"rate:{key_suffix}:{window_number}"

        count, _ = self.store.fixed_window_hit(redis_key, window)
        allowed = count <= limit
        remaining = max(0, limit - count)
        retry_after = max(1, math.ceil(window_end - now)) if not allowed else 0
        reset_at = int(window_end)

        return RateLimitResult(
            allowed=allowed,
            limit=limit,
            remaining=remaining,
            retry_after=retry_after,
            reset_at=reset_at,
        )

    def fixed_window(self, key: str, limit: int, window: int) -> bool:
        """Simplified boolean Fixed Window evaluation.

        Why:
        Preserves backward compatibility with the SRS example specification:
        `limiter.fixed_window("client1:/api/products", 5, 60) is True`
        """
        return self.fixed_window_check(key, limit, window).allowed

    def sliding_window_check(
        self,
        key_suffix: str,
        limit: int,
        window: int,
    ) -> RateLimitResult:
        """Evaluate request using Sliding Window (Redis Sorted Set).

        Sliding Window mechanics:
        - Redis key = slide:<client>:<endpoint>
        - Unique member = f"{now}:{uuid.uuid4().hex[:8]}" (prevents same-instant collisions)
        - Old entries before (now - window) are pruned atomically.
        - If count < limit: add member, request allowed.
        - If count >= limit: request rejected and NOT added to sorted set.
        - retry_after = ceil(oldest_ts + window - now) (min 1).
        """
        now = self.time_func()
        member = f"{now}:{uuid.uuid4().hex[:8]}"
        redis_key = f"slide:{key_suffix}"

        allowed, count, oldest_ts = self.store.sliding_window_hit(
            redis_key, now, window, limit, member
        )

        remaining = max(0, limit - count)
        retry_after = max(1, math.ceil(oldest_ts + window - now)) if not allowed else 0
        reset_at = (
            int(math.ceil(now + retry_after)) if not allowed else int(math.ceil(now + window))
        )

        return RateLimitResult(
            allowed=allowed,
            limit=limit,
            remaining=remaining,
            retry_after=retry_after,
            reset_at=reset_at,
        )

    def sliding_window(self, key: str, limit: int, window: int) -> bool:
        """Simplified boolean Sliding Window evaluation.

        Why:
        Preserves simple boolean interface for basic tests and checks.
        """
        return self.sliding_window_check(key, limit, window).allowed

    def is_allowed(self, client_id: str, policy: RateLimitRule) -> RateLimitResult:
        """Dispatch rate limit check based on configured policy algorithm.

        Why:
        Acts as the primary facade for route handlers, resolving endpoint keys
        and delegating to the appropriate algorithm implementation.
        """
        suffix = policy.redis_key_suffix(client_id)
        if policy.algorithm == "fixed_window":
            return self.fixed_window_check(suffix, policy.limit, policy.window)
        elif policy.algorithm == "sliding_window":
            return self.sliding_window_check(suffix, policy.limit, policy.window)
        else:
            raise PolicyValidationError(
                f"Unsupported rate limiting algorithm: {policy.algorithm!r}"
            )
