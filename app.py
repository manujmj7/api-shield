"""app.py - Flask application factory and server startup for API Shield.

Why this module exists:
Uses the Application Factory pattern (`create_app`) to wire up dependencies cleanly.
Allows tests to inject fake Redis, custom test policies, and injectable clocks
without starting external processes or relying on global mutable state.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

import redis
from flask import Flask

from blocking import BlockManager
from config import Config
from limiter import RateLimiter
from logging_utils import RequestLogger
from policy import PolicyManager
from redis_store import RedisStore
from routes import api_bp, register_error_handlers

logger = logging.getLogger(__name__)


def create_app(
    config: Optional[Config] = None,
    redis_client: Optional[redis.Redis] = None,
    policy_manager: Optional[PolicyManager] = None,
    clock: Optional[Callable[[], float]] = None,
) -> Flask:
    """Create and configure the Flask application instance.

    Why:
    Application factory enables complete dependency injection. Tests can supply
    fakeredis, an in-memory policy file, and a controllable virtual clock.
    """
    app = Flask(__name__)

    # 1. Resolve configuration
    cfg = config or Config.load()
    app.config["SHIELD_CONFIG"] = cfg
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    # 2. Clock injection
    time_func = clock or time.time

    # 3. Redis Client and Store
    if redis_client is None:
        redis_client = redis.Redis(
            host=cfg.redis_host,
            port=cfg.redis_port,
            decode_responses=True,
            socket_connect_timeout=2.0,
        )
    redis_store = RedisStore(redis_client)
    app.config["REDIS_STORE"] = redis_store

    # 4. Policy Manager
    if policy_manager is None:
        policy_manager = PolicyManager(storage_path=cfg.policy_file)
    app.config["POLICY_MANAGER"] = policy_manager

    # 5. Rate Limiter
    rate_limiter = RateLimiter(store=redis_store, time_func=time_func)
    app.config["RATE_LIMITER"] = rate_limiter

    # 6. Block Manager
    block_manager = BlockManager(
        store=redis_store,
        violation_ttl=cfg.violation_ttl_seconds,
    )
    app.config["BLOCK_MANAGER"] = block_manager

    # 7. Request Logger
    request_logger = RequestLogger(
        log_file=cfg.log_file,
        log_level=cfg.log_level,
    )
    app.config["REQUEST_LOGGER"] = request_logger

    # 8. Register Routes and Error Handlers
    app.register_blueprint(api_bp)
    register_error_handlers(app)

    logger.info("API Shield initialized on %s:%d", cfg.app_host, cfg.app_port)
    return app


if __name__ == "__main__":
    app_instance = create_app()
    server_cfg: Config = app_instance.config["SHIELD_CONFIG"]
    app_instance.run(
        host=server_cfg.app_host,
        port=server_cfg.app_port,
        debug=False,
    )
