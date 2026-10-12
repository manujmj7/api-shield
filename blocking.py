"""blocking.py - Violation tracking and temporary blocking mechanism (SRS Section 6).

Why this module exists:
Implements progressive rate limit penalties for repeat offenders.
Clients that persistently breach rate limits receive tiered temporary blocks:
- 1 to 3 violations: 5 minutes (300s)
- 4 to 6 violations: 15 minutes (900s)
- 7 or more violations: 30 minutes (1800s)

Crucial Anti-Self-Extending Rule:
Violations are recorded ONLY when a rate limiter check initially rejects a request.
Requests received while a client is already blocked do NOT record additional violations
or reset the block duration. This prevents an ongoing DDoS from infinitely trapping a client.
"""

from __future__ import annotations

import logging
from typing import Tuple

from redis_store import RedisStore

logger = logging.getLogger(__name__)


def get_block_duration(violations: int) -> int:
    """Calculate tiered block duration in seconds based on cumulative violation count.

    SRS 6.1 tier specification:
    - 1 to 3 violations: 300s (5 minutes)
    - 4 to 6 violations: 900s (15 minutes)
    - 7+ violations: 1800s (30 minutes)
    """
    if violations <= 3:
        return 300
    elif violations <= 6:
        return 900
    return 1800


class BlockManager:
    """Manages violation accumulation and temporary blocking in Redis."""

    def __init__(self, store: RedisStore, violation_ttl: int = 86400) -> None:
        """Initialize BlockManager with RedisStore and violation retention period.

        Why:
        Injecting violation_ttl (default 24h = 86400s) allows violations to expire
        over time, giving clients a clean slate after good behavior.
        """
        self.store = store
        self.violation_ttl = violation_ttl

    def is_blocked(self, client_id: str) -> bool:
        """Check if client currently has an active block in Redis."""
        return self.store.is_blocked(client_id)

    def get_block_ttl(self, client_id: str) -> int:
        """Get remaining TTL on active block in seconds (or 0 if unblocked)."""
        return self.store.get_block_ttl(client_id)

    def block_client(self, client_id: str, duration_seconds: int) -> None:
        """Place an explicit temporary block on a client in Redis."""
        self.store.block(client_id, duration_seconds)

    def get_violations(self, client_id: str) -> int:
        """Read the client's current violation count."""
        return self.store.get_violations(client_id)

    def record_violation(self, client_id: str) -> Tuple[int, int]:
        """Record a rate limit violation and trigger a temporary block.

        Returns:
            Tuple of (total_violations: int, block_duration_seconds: int)

        Important:
            If the client is ALREADY blocked, this method does NOT record a new
            violation or extend the block. This prevents blocks from self-extending.
        """
        if self.is_blocked(client_id):
            # Do not self-extend existing block
            current_violations = self.get_violations(client_id)
            current_ttl = self.get_block_ttl(client_id)
            logger.debug(
                "Client %s already blocked (ttl=%ds); skipping violation increment.",
                client_id,
                current_ttl,
            )
            return current_violations, current_ttl

        violations = self.store.incr_violation(client_id, self.violation_ttl)
        duration = get_block_duration(violations)
        self.block_client(client_id, duration)
        logger.info(
            "Client %s recorded violation #%d; temporarily blocked for %ds.",
            client_id,
            violations,
            duration,
        )
        return violations, duration
