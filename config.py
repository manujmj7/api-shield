"""config.py - Environment configuration and validation for API Shield.

Why this module exists:
Centralizing configuration ensures all settings (ports, hostnames, policy files,
security keys, and fallback toggles) are loaded from one place with strict validation.
Failing fast on startup prevents hidden misconfigurations during live request handling.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass

from dotenv import load_dotenv

from errors import ConfigurationError

# Load variables from .env file if present
load_dotenv()


def _parse_bool(value: str | bool) -> bool:
    """Helper to convert environment variable string to boolean.

    Why:
    Environment variables are always strings, but boolean flags like FAIL_OPEN
    need robust parsing (e.g. 'true', '1', 'yes').
    """
    if isinstance(value, bool):
        return value
    val = str(value).strip().lower()
    return val in ("true", "1", "yes", "on")


@dataclass
class Config:
    """Configuration class containing all application settings.

    Why:
    Using a typed dataclass provides clear IDE autocompletion, type guarantees,
    and straightforward test overrides without mutating os.environ globally.
    """

    app_host: str = "127.0.0.1"
    app_port: int = 5000
    redis_host: str = "localhost"
    redis_port: int = 6379
    default_limit: int = 10
    default_window: int = 60
    log_level: str = "INFO"
    admin_api_key: str = ""
    violation_ttl_seconds: int = 86400
    policy_file: str = "policies.json"
    fail_open: bool = True
    log_file: str = "logs/api_shield.log"

    @classmethod
    def load(cls, **overrides: object) -> Config:
        """Load configuration from environment with optional overrides and validate.

        Why:
        Allows production to read environment variables while allowing unit tests
        to inject custom mock values cleanly.
        """
        raw_admin_key = str(overrides.get("admin_api_key", os.getenv("ADMIN_API_KEY", ""))).strip()
        admin_key = raw_admin_key
        if not admin_key:
            # Generate a secure random token if no admin key was supplied
            admin_key = secrets.token_urlsafe(32)
            print(
                f"[SECURITY NOTICE] ADMIN_API_KEY was unset. Generated ephemeral admin key: {admin_key}"
            )

        def _get_int(key: str, default: int) -> int:
            val = overrides.get(key.lower(), os.getenv(key, default))
            try:
                return int(val)  # type: ignore[arg-type]
            except (ValueError, TypeError) as exc:
                raise ConfigurationError(
                    f"Configuration key '{key}' must be a valid integer, got '{val}'"
                ) from exc

        app_port = _get_int("APP_PORT", 5000)
        redis_port = _get_int("REDIS_PORT", 6379)
        default_limit = _get_int("DEFAULT_LIMIT", 10)
        default_window = _get_int("DEFAULT_WINDOW", 60)
        violation_ttl_seconds = _get_int("VIOLATION_TTL_SECONDS", 86400)

        # Validation rules
        if not (1 <= app_port <= 65535):
            raise ConfigurationError(f"APP_PORT must be between 1 and 65535, got {app_port}")
        if not (1 <= redis_port <= 65535):
            raise ConfigurationError(f"REDIS_PORT must be between 1 and 65535, got {redis_port}")
        if default_limit <= 0:
            raise ConfigurationError(
                f"DEFAULT_LIMIT must be positive integer (> 0), got {default_limit}"
            )
        if default_window <= 0:
            raise ConfigurationError(
                f"DEFAULT_WINDOW must be positive integer (> 0), got {default_window}"
            )
        if violation_ttl_seconds <= 0:
            raise ConfigurationError(
                f"VIOLATION_TTL_SECONDS must be positive integer (> 0), got {violation_ttl_seconds}"
            )

        fail_open_val = overrides.get("fail_open", os.getenv("FAIL_OPEN", "true"))
        fail_open = _parse_bool(fail_open_val)  # type: ignore[arg-type]

        app_host = str(overrides.get("app_host", os.getenv("APP_HOST", "127.0.0.1")))
        redis_host = str(overrides.get("redis_host", os.getenv("REDIS_HOST", "localhost")))
        log_level = str(overrides.get("log_level", os.getenv("LOG_LEVEL", "INFO"))).upper()
        policy_file = str(overrides.get("policy_file", os.getenv("POLICY_FILE", "policies.json")))
        log_file = str(overrides.get("log_file", os.getenv("LOG_FILE", "logs/api_shield.log")))

        return cls(
            app_host=app_host,
            app_port=app_port,
            redis_host=redis_host,
            redis_port=redis_port,
            default_limit=default_limit,
            default_window=default_window,
            log_level=log_level,
            admin_api_key=admin_key,
            violation_ttl_seconds=violation_ttl_seconds,
            policy_file=policy_file,
            fail_open=fail_open,
            log_file=log_file,
        )
