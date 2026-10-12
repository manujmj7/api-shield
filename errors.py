"""errors.py - Custom exceptions for the API Shield rate limiting system.

Why this module exists:
Centralizing exceptions provides a unified error hierarchy for the application.
Interns and API handlers can catch specific, domain-relevant errors (like
DuplicatePolicyError or PolicyValidationError) rather than generic runtime errors,
enabling exact HTTP status codes and actionable error messages.
"""

from typing import Optional


class ApiShieldError(Exception):
    """Base exception for all domain-specific errors in API Shield.

    Catching this allows middleware or error handlers to distinguish internal
    framework errors from domain-level rate limiting failures.
    """

    def __init__(self, message: str, details: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class ConfigurationError(ApiShieldError):
    """Raised when environment variables or application configuration are invalid.

    Why:
    Failing fast at boot time with a descriptive error prevents the system from running
    in an unpredictable state (e.g. non-numeric ports or negative rate limits).
    """


class PolicyValidationError(ApiShieldError):
    """Raised when a rate limit policy fails structure or value validation.

    Why:
    Validating policies strictly upfront (ensuring limits > 0, windows > 0, valid algorithms)
    prevents runtime crashes during request evaluation.
    """


class PolicyNotFoundError(ApiShieldError):
    """Raised when looking up, updating, or toggling a non-existent policy name.

    Why:
    Allows REST endpoints to translate missing resources into clean HTTP 404 responses.
    """


class DuplicatePolicyError(ApiShieldError):
    """Raised when attempting to create a policy whose name already exists.

    Why:
    Policy names act as unique identifiers in the storage registry; duplicates would cause
    unexpected overwrites or ambiguity.
    """


class StoreUnavailableError(ApiShieldError):
    """Raised when Redis or the persistence backend cannot be reached or fails mid-operation.

    Why:
    Abstracts backend-specific client exceptions (such as redis.RedisError) so upper layers
    can implement fail-open or fail-closed decisions cleanly.
    """
