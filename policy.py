"""policy.py - Policy rules and thread-safe policy management for API Shield.

Why this module exists:
Decoupling rate limiting rules from route code allows dynamic reconfiguration of limits,
endpoints, and algorithms without restarting the application. A thread-safe,
JSON-persisted PolicyManager guarantees that rule modifications take effect immediately
for concurrent worker threads (BR-10).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from errors import DuplicatePolicyError, PolicyNotFoundError, PolicyValidationError

logger = logging.getLogger(__name__)

SUPPORTED_ALGORITHMS = {"fixed_window", "sliding_window"}
ALLOWED_RULE_FIELDS = {"name", "endpoint", "limit", "window", "algorithm", "active", "updated_at"}


@dataclass
class RateLimitRule:
    """Represents a single rate limiting policy rule.

    Why:
    Dataclasses enforce clean typing and attribute contracts. Encapsulating
    validation logic inside the rule guarantees that invalid configurations
    are rejected immediately before reaching the Redis storage engine.
    """

    name: str
    endpoint: str
    limit: int
    window: int
    algorithm: str
    active: bool = True
    updated_at: float = field(default_factory=time.time)

    def validate(self) -> None:
        """Validate all policy attributes against strict domain rules.

        Raises:
            PolicyValidationError: If any attribute violates the specification.

        Why:
            Python's loose typing can permit booleans as integers (since bool is a
            subclass of int) or negative limits. Explicit type and value checks
            ensure zero runtime ambiguity.
        """
        if not isinstance(self.name, str) or not self.name.strip():
            raise PolicyValidationError("Policy name must be a non-empty string.")

        if not isinstance(self.endpoint, str) or not self.endpoint.startswith("/"):
            raise PolicyValidationError(
                f"Policy endpoint must be a string starting with '/', got {self.endpoint!r}"
            )

        # In Python, isinstance(True, int) is True, so verify type is strictly int
        if type(self.limit) is not int or self.limit <= 0:
            raise PolicyValidationError(
                f"Policy limit must be a positive integer (> 0), got {self.limit!r}"
            )

        if type(self.window) is not int or self.window <= 0:
            raise PolicyValidationError(
                f"Policy window must be a positive integer in seconds (> 0), got {self.window!r}"
            )

        if not isinstance(self.algorithm, str) or self.algorithm not in SUPPORTED_ALGORITHMS:
            raise PolicyValidationError(
                f"Algorithm must be one of {sorted(SUPPORTED_ALGORITHMS)}, got {self.algorithm!r}"
            )

        if type(self.active) is not bool:
            raise PolicyValidationError(
                f"Policy active flag must be a boolean (True/False), got {self.active!r}"
            )

    def redis_key_suffix(self, client_id: str) -> str:
        """Generate the unified client and endpoint suffix for Redis keys (BR-08).

        Why:
            BR-08 requires Redis keys to combine client identity and endpoint path
            to prevent traffic counters across endpoints or clients from mixing.
        """
        return f"{client_id}:{self.endpoint}"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize policy rule to a JSON-compatible dictionary."""
        return {
            "name": self.name,
            "endpoint": self.endpoint,
            "limit": self.limit,
            "window": self.window,
            "algorithm": self.algorithm,
            "active": self.active,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RateLimitRule:
        """Create and validate a RateLimitRule from dictionary data.

        Raises:
            PolicyValidationError: If unknown fields exist or validation fails.
        """
        if not isinstance(data, dict):
            raise PolicyValidationError("Policy data must be a JSON object dictionary.")

        # Reject unknown fields to maintain data integrity
        unknown_fields = set(data.keys()) - ALLOWED_RULE_FIELDS
        if unknown_fields:
            raise PolicyValidationError(f"Unknown policy fields: {sorted(unknown_fields)}")

        required = {"name", "endpoint", "limit", "window", "algorithm"}
        missing = required - set(data.keys())
        if missing:
            raise PolicyValidationError(f"Missing required fields: {sorted(missing)}")

        rule = cls(
            name=data["name"],
            endpoint=data["endpoint"],
            limit=data["limit"],
            window=data["window"],
            algorithm=data["algorithm"],
            active=data.get("active", True),
            updated_at=data.get("updated_at", time.time()),
        )
        rule.validate()
        return rule


class PolicyManager:
    """Thread-safe, JSON-persisted repository of rate limit policies.

    Why:
    Flask may process requests across multiple threads concurrently. Using an
    RLock prevents race conditions during policy creation, updates, and lookups.
    Atomic file writes guarantee policies are never corrupted by mid-write crashes.
    """

    def __init__(self, storage_path: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self.storage_path = storage_path
        self._policies: Dict[str, RateLimitRule] = {}
        if self.storage_path:
            self._load()

    def _load(self) -> None:
        """Load policies from JSON file, tolerating missing, empty, or corrupt files."""
        if not self.storage_path:
            return

        path = Path(self.storage_path)
        if not path.exists():
            logger.info(
                "Policy file '%s' does not exist; starting with empty registry.", self.storage_path
            )
            return

        try:
            content = path.read_text(encoding="utf-8").strip()
            if not content:
                logger.warning(
                    "Policy file '%s' is empty; starting with empty registry.", self.storage_path
                )
                return

            raw_data = json.loads(content)
            if not isinstance(raw_data, dict):
                logger.warning("Policy file root must be an object; starting empty.")
                return

            loaded: Dict[str, RateLimitRule] = {}
            for name, rule_data in raw_data.items():
                try:
                    if isinstance(rule_data, dict):
                        rule = RateLimitRule.from_dict(rule_data)
                        loaded[name] = rule
                except Exception as exc:
                    logger.warning("Skipping invalid policy '%s' from file: %s", name, exc)

            with self._lock:
                self._policies = loaded
            logger.info("Loaded %d policies from %s", len(loaded), self.storage_path)
        except Exception as exc:
            logger.warning(
                "Failed to parse policy file '%s': %s. Starting with empty registry.",
                self.storage_path,
                exc,
            )

    def _save(self) -> None:
        """Persist current policies to storage_path atomically using a temporary file.

        Why:
        Atomic write (write to temp file then rename via os.replace) ensures that
        concurrent readers or unexpected power loss never see a half-written JSON file.
        """
        if not self.storage_path:
            return

        target = Path(self.storage_path)
        target.parent.mkdir(parents=True, exist_ok=True)

        data = {name: rule.to_dict() for name, rule in self._policies.items()}
        serialized = json.dumps(data, indent=2)

        temp_fd, temp_path = tempfile.mkstemp(
            dir=str(target.parent),
            prefix="policies_",
            suffix=".tmp",
            text=True,
        )
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as tmp_file:
                tmp_file.write(serialized)
            os.replace(temp_path, str(target))
        except Exception:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise

    def create(self, policy: RateLimitRule) -> RateLimitRule:
        """Register a new policy rule.

        Raises:
            DuplicatePolicyError: If a policy with the same name already exists.
            PolicyValidationError: If rule values fail validation.
        """
        policy.validate()
        with self._lock:
            if policy.name in self._policies:
                raise DuplicatePolicyError(f"Policy '{policy.name}' already exists.")
            policy.updated_at = time.time()
            self._policies[policy.name] = policy
            self._save()
            return policy

    def update(self, name: str, policy: RateLimitRule) -> RateLimitRule:
        """Update an existing policy rule.

        Raises:
            PolicyNotFoundError: If the policy does not exist.
            PolicyValidationError: If rule values fail validation.
        """
        policy.validate()
        with self._lock:
            if name not in self._policies:
                raise PolicyNotFoundError(f"Policy '{name}' not found.")
            # Ensure name consistency
            policy.name = name
            policy.updated_at = time.time()
            self._policies[name] = policy
            self._save()
            return policy

    def activate(self, name: str) -> RateLimitRule:
        """Activate a policy rule by name."""
        with self._lock:
            if name not in self._policies:
                raise PolicyNotFoundError(f"Policy '{name}' not found.")
            rule = self._policies[name]
            rule.active = True
            rule.updated_at = time.time()
            self._save()
            return rule

    def deactivate(self, name: str) -> RateLimitRule:
        """Deactivate a policy rule by name."""
        with self._lock:
            if name not in self._policies:
                raise PolicyNotFoundError(f"Policy '{name}' not found.")
            rule = self._policies[name]
            rule.active = False
            rule.updated_at = time.time()
            self._save()
            return rule

    def get(self, name: str) -> RateLimitRule:
        """Retrieve a policy by name."""
        with self._lock:
            if name not in self._policies:
                raise PolicyNotFoundError(f"Policy '{name}' not found.")
            return self._policies[name]

    def list(self) -> List[RateLimitRule]:
        """Return a list of all registered policies."""
        with self._lock:
            return list(self._policies.values())

    def get_policy_for_endpoint(self, path: str) -> Optional[RateLimitRule]:
        """Find the active policy matching an exact endpoint path.

        Deterministic tie-breaking:
        If multiple active policies match the exact same endpoint path, the one with
        the latest `updated_at` timestamp takes precedence. This allows administrators
        to override policies dynamically without race conditions.
        """
        with self._lock:
            matching = [
                rule for rule in self._policies.values() if rule.active and rule.endpoint == path
            ]
            if not matching:
                return None
            # Most recently updated wins
            matching.sort(key=lambda r: r.updated_at, reverse=True)
            return matching[0]
