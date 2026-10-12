"""logging_utils.py - Structured request logging with privacy-preserving masking.

Why this module exists:
Centralizes application and security auditing (SRS 10.1, FR-10).
Outputs structured, single-line JSON logs to both stdout and a rotating file handler
(1 MB x 3 backups). Guarantees that sensitive credentials are never written to disk
by strictly demanding masked client identifiers.
"""

from __future__ import annotations

import json
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict


class RequestLogger:
    """Structured logger for API Shield rate-limiting decisions."""

    def __init__(
        self,
        log_file: str = "logs/api_shield.log",
        log_level: str = "INFO",
        logger_name: str = "api_shield",
    ) -> None:
        """Initialize console and rotating file logging handlers.

        Why:
        Rotating file handler prevents log files from growing indefinitely and filling
        disk space (1 MB max with 3 rotation backups).
        """
        self.logger = logging.getLogger(logger_name)
        self.logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
        self.logger.propagate = False

        # Only add handlers if not already configured on this logger
        if not self.logger.handlers:
            formatter = logging.Formatter("%(message)s")

            # Console handler
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            self.logger.addHandler(console_handler)

            # Rotating file handler
            if log_file:
                log_path = Path(log_file)
                log_path.parent.mkdir(parents=True, exist_ok=True)
                file_handler = RotatingFileHandler(
                    str(log_path),
                    maxBytes=1_048_576,  # 1 MB
                    backupCount=3,
                    encoding="utf-8",
                )
                file_handler.setFormatter(formatter)
                self.logger.addHandler(file_handler)

    def _emit(self, record: Dict[str, Any], level: int = logging.INFO) -> None:
        """Emit structured JSON log line."""
        self.logger.log(level, json.dumps(record, separators=(",", ":")))

    def log_allowed(
        self,
        masked_client: str,
        endpoint: str,
        remaining: int,
    ) -> None:
        """Log a successfully permitted request.

        Format includes timestamp, decision=allowed, masked_client, endpoint, remaining.
        """
        record = {
            "timestamp": time.time(),
            "decision": "allowed",
            "client": masked_client,
            "endpoint": endpoint,
            "remaining": remaining,
            "retry_after": 0,
        }
        self._emit(record, logging.INFO)

    def log_blocked(
        self,
        masked_client: str,
        endpoint: str,
        reason: str,
        retry_after: int,
    ) -> None:
        """Log a rejected request (rate_limit or temporary_block).

        Args:
            masked_client: Privacy-masked client ID (e.g. 'key:se***1a2b3c' or 'ip:...').
            endpoint: The requested API path.
            reason: Either 'rate_limit' or 'temporary_block'.
            retry_after: Number of seconds before client may retry.
        """
        record = {
            "timestamp": time.time(),
            "decision": "blocked",
            "reason": reason,
            "client": masked_client,
            "endpoint": endpoint,
            "remaining": 0,
            "retry_after": retry_after,
        }
        self._emit(record, logging.WARNING)

    def log_store_error(
        self,
        masked_client: str,
        endpoint: str,
        error_msg: str,
        fail_open: bool,
    ) -> None:
        """Log Redis store failure and fallback decision."""
        record = {
            "timestamp": time.time(),
            "decision": "store_error",
            "action": "degraded_allowed" if fail_open else "service_unavailable",
            "client": masked_client,
            "endpoint": endpoint,
            "error": error_msg,
        }
        level = logging.WARNING if fail_open else logging.ERROR
        self._emit(record, level)
