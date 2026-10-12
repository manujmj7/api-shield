"""routes.py - HTTP routes, admin endpoints, rate limiting hooks, and error handlers.

Why this module exists:
Implements the entire REST API interface per SRS sections 7, 8, 13, and 23.
Wired with Flask hooks (`before_request`, `after_request`) to enforce rate limiting
transparently before protected route logic executes.
"""

from __future__ import annotations

import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from flask import Blueprint, Flask, current_app, g, jsonify, render_template, request

from blocking import BlockManager
from config import Config
from errors import (
    DuplicatePolicyError,
    PolicyNotFoundError,
    PolicyValidationError,
    StoreUnavailableError,
)
from identity import get_client_id, mask_client_id
from limiter import RateLimiter
from logging_utils import RequestLogger
from policy import PolicyManager, RateLimitRule

logger = logging.getLogger(__name__)

api_bp = Blueprint("api", __name__)
APP_START_TIME = time.time()


def _is_admin_request(path: str) -> bool:
    """Check if requested path targets an administrative policy endpoint."""
    return path == "/api/policies" or path.startswith("/api/policies/")


def _is_unprotected_request(path: str) -> bool:
    """Check if requested path is exempt from rate limiting (health, status, admin, ui)."""
    return (
        path in ("/health", "/status", "/", "/dashboard")
        or path.startswith("/api/ui/")
        or path.startswith("/static/")
        or _is_admin_request(path)
        or not path.startswith("/api/")
    )


def _verify_admin_key(cfg: Config) -> bool:
    """Constant-time comparison of X-Admin-Key against configured ADMIN_API_KEY.

    Why:
    hmac.compare_digest prevents timing attacks that could reveal secret key bytes.
    """
    admin_header = request.headers.get("X-Admin-Key", "")
    if not admin_header:
        return False
    return hmac.compare_digest(admin_header, cfg.admin_api_key)


# ============================================================================
# Core Rate Limiting Middleware Hooks (SRS Section 23 Decision Flow)
# ============================================================================


@api_bp.before_app_request
def enforce_rate_limiting() -> Any:
    """Enforce rate limits before protected route execution.

    Flow (SRS Section 23):
    1. Check if route is protected (skip /health, /status, /api/policies/*)
    2. Identify client: X-API-Key or client IP (BR-07)
    3. Look up active policy for request.path (BR-01, BR-02) -> allow if none
    4. Check temporary block (BR-05, BR-06) -> 429 if blocked
    5. Evaluate rate limit algorithm (Fixed Window or Sliding Window)
    6. If allowed: record remaining headers, log allowed, proceed to route
    7. If rejected: record violation, apply tiered block rule, log, return 429
    """
    path = request.path

    # Admin route authentication check
    if _is_admin_request(path):
        cfg: Config = current_app.config["SHIELD_CONFIG"]
        if not _verify_admin_key(cfg):
            return jsonify({"error": "Unauthorized"}), 401
        return None  # Admin routes are exempt from rate limiting

    if _is_unprotected_request(path):
        return None

    cfg = current_app.config["SHIELD_CONFIG"]
    policy_mgr: PolicyManager = current_app.config["POLICY_MANAGER"]
    limiter: RateLimiter = current_app.config["RATE_LIMITER"]
    block_mgr: BlockManager = current_app.config["BLOCK_MANAGER"]
    req_logger: RequestLogger = current_app.config["REQUEST_LOGGER"]

    # 1. Identify client
    client_id = get_client_id(request)
    masked_client = mask_client_id(client_id)

    # 2. Find active policy for endpoint
    policy = policy_mgr.get_policy_for_endpoint(path)
    if policy is None:
        # BR-02: If no active policy exists, allow the request
        return None

    # 3. Check temporary block
    try:
        if block_mgr.is_blocked(client_id):
            block_ttl = block_mgr.get_block_ttl(client_id)
            req_logger.log_blocked(masked_client, path, "temporary_block", block_ttl)

            response = jsonify(
                {
                    "error": "Rate limit exceeded",
                    "message": "Please try again later",
                }
            )
            response.status_code = 429
            response.headers["X-RateLimit-Limit"] = str(policy.limit)
            response.headers["X-RateLimit-Remaining"] = "0"
            response.headers["Retry-After"] = str(max(1, block_ttl))
            return response

    except StoreUnavailableError as exc:
        if cfg.fail_open:
            req_logger.log_store_error(masked_client, path, str(exc), fail_open=True)
            g.rate_limit_degraded = True
            return None
        else:
            req_logger.log_store_error(masked_client, path, str(exc), fail_open=False)
            return jsonify(
                {
                    "error": "Service Unavailable",
                    "message": "Rate limiting backend unavailable",
                }
            ), 503

    # 4. Evaluate algorithm
    try:
        result = limiter.is_allowed(client_id, policy)
    except StoreUnavailableError as exc:
        if cfg.fail_open:
            req_logger.log_store_error(masked_client, path, str(exc), fail_open=True)
            g.rate_limit_degraded = True
            return None
        else:
            req_logger.log_store_error(masked_client, path, str(exc), fail_open=False)
            return jsonify(
                {
                    "error": "Service Unavailable",
                    "message": "Rate limiting backend unavailable",
                }
            ), 503

    if result.allowed:
        # Request permitted
        g.rate_limit_result = result
        g.policy = policy
        req_logger.log_allowed(masked_client, path, result.remaining)
        return None

    # 5. Limit exceeded (rejected)
    retry_after = result.retry_after
    try:
        violations, block_duration = block_mgr.record_violation(client_id)
        if block_mgr.is_blocked(client_id):
            retry_after = max(retry_after, block_duration)
    except StoreUnavailableError:
        pass

    req_logger.log_blocked(masked_client, path, "rate_limit", retry_after)

    response = jsonify(
        {
            "error": "Rate limit exceeded",
            "message": "Please try again later",
        }
    )
    response.status_code = 429
    response.headers["X-RateLimit-Limit"] = str(policy.limit)
    response.headers["X-RateLimit-Remaining"] = "0"
    response.headers["X-RateLimit-Reset"] = str(result.reset_at)
    response.headers["Retry-After"] = str(max(1, retry_after))
    return response


@api_bp.after_app_request
def attach_rate_limit_headers(response: Any) -> Any:
    """Attach standard rate limit metadata headers on responses (SRS 8.2)."""
    rate_result = getattr(g, "rate_limit_result", None)
    policy = getattr(g, "policy", None)

    if rate_result is not None and policy is not None:
        response.headers["X-RateLimit-Limit"] = str(policy.limit)
        response.headers["X-RateLimit-Remaining"] = str(rate_result.remaining)
        response.headers["X-RateLimit-Reset"] = str(rate_result.reset_at)

    if getattr(g, "rate_limit_degraded", False):
        response.headers["X-RateLimit-Status"] = "degraded"

    return response


# ============================================================================
# Unprotected Endpoints (Health & Status)
# ============================================================================


@api_bp.route("/health", methods=["GET"])
def health_check() -> Any:
    """Application and Redis health status check (FR-14)."""
    redis_store = current_app.config.get("REDIS_STORE")
    redis_ok = False
    if redis_store:
        try:
            redis_ok = redis_store.ping()
        except Exception:
            redis_ok = False

    return jsonify(
        {
            "status": "healthy" if redis_ok else "degraded",
            "redis": "connected" if redis_ok else "disconnected",
        }
    ), 200


@api_bp.route("/status", methods=["GET"])
def status_info() -> Any:
    """Operational status metrics and active policies count."""
    policy_mgr: PolicyManager = current_app.config["POLICY_MANAGER"]
    redis_store = current_app.config.get("REDIS_STORE")
    active_count = len([p for p in policy_mgr.list() if p.active])

    redis_ok = False
    if redis_store:
        try:
            redis_ok = redis_store.ping()
        except Exception:
            redis_ok = False

    uptime = round(time.time() - APP_START_TIME, 2)
    return jsonify(
        {
            "version": "1.0.0",
            "active_policies": active_count,
            "redis_connected": redis_ok,
            "uptime_seconds": uptime,
        }
    ), 200


# ============================================================================
# Protected Sample Endpoints
# ============================================================================


@api_bp.route("/api/products", methods=["GET"])
def get_products() -> Any:
    """Sample protected product catalog listing."""
    return jsonify(
        {
            "products": [
                {"id": 1, "name": "Laptop", "price": 999.99},
                {"id": 2, "name": "Mechanical Keyboard", "price": 89.99},
                {"id": 3, "name": "Wireless Mouse", "price": 29.99},
            ],
            "message": "Request successful",
        }
    ), 200


@api_bp.route("/api/users", methods=["GET"])
def get_users() -> Any:
    """Sample protected users listing."""
    return jsonify(
        {
            "users": [
                {"id": 101, "username": "alice"},
                {"id": 102, "username": "bob"},
            ],
            "message": "Request successful",
        }
    ), 200


@api_bp.route("/api/login", methods=["POST"])
def login() -> Any:
    """Sample login endpoint for brute-force rate limit protection.

    Demonstrates credential checking and rapid rate limit violation triggering.
    """
    if not request.is_json:
        return jsonify({"error": "Invalid request", "details": "Request body must be JSON"}), 400

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid request", "details": "JSON body must be an object"}), 400

    username = data.get("username")
    password = data.get("password")

    if not username or not password:
        return jsonify(
            {"error": "Invalid request", "details": "username and password are required"}
        ), 400

    # Demo credentials
    if username == "admin" and password == "password123":
        return jsonify({"message": "Login successful", "user": username}), 200

    return jsonify({"error": "Invalid credentials"}), 401


# ============================================================================
# Admin Endpoints (SRS 7.1, Protected by X-Admin-Key)
# ============================================================================


@api_bp.route("/api/policies", methods=["GET"])
def list_policies() -> Any:
    """List all registered rate limit policies."""
    policy_mgr: PolicyManager = current_app.config["POLICY_MANAGER"]
    return jsonify([p.to_dict() for p in policy_mgr.list()]), 200


@api_bp.route("/api/policies", methods=["POST"])
def create_policy() -> Any:
    """Create a new rate limit policy (FR-11)."""
    if not request.is_json:
        return jsonify({"error": "Invalid policy", "details": "Body must be JSON"}), 400

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid policy", "details": "JSON body must be an object"}), 400

    policy_mgr: PolicyManager = current_app.config["POLICY_MANAGER"]
    rule = RateLimitRule.from_dict(data)
    created = policy_mgr.create(rule)
    return jsonify(created.to_dict()), 201


@api_bp.route("/api/policies/<name>", methods=["GET"])
def get_policy(name: str) -> Any:
    """Retrieve details of a single policy."""
    policy_mgr: PolicyManager = current_app.config["POLICY_MANAGER"]
    policy = policy_mgr.get(name)
    return jsonify(policy.to_dict()), 200


@api_bp.route("/api/policies/<name>", methods=["PUT"])
def update_policy(name: str) -> Any:
    """Update an existing policy (FR-11)."""
    if not request.is_json:
        return jsonify({"error": "Invalid policy", "details": "Body must be JSON"}), 400

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid policy", "details": "JSON body must be an object"}), 400

    # Guarantee name in payload matches URL
    data["name"] = name
    rule = RateLimitRule.from_dict(data)

    policy_mgr: PolicyManager = current_app.config["POLICY_MANAGER"]
    updated = policy_mgr.update(name, rule)
    return jsonify(updated.to_dict()), 200


@api_bp.route("/api/policies/<name>/activate", methods=["POST"])
def activate_policy(name: str) -> Any:
    """Activate a policy (FR-12)."""
    policy_mgr: PolicyManager = current_app.config["POLICY_MANAGER"]
    activated = policy_mgr.activate(name)
    return jsonify(activated.to_dict()), 200


@api_bp.route("/api/policies/<name>/deactivate", methods=["POST"])
def deactivate_policy(name: str) -> Any:
    """Deactivate a policy (FR-12)."""
    policy_mgr: PolicyManager = current_app.config["POLICY_MANAGER"]
    deactivated = policy_mgr.deactivate(name)
    return jsonify(deactivated.to_dict()), 200


# ============================================================================
# Dashboard Web Interface & UI Observability Endpoints
# ============================================================================


@api_bp.route("/", methods=["GET"])
@api_bp.route("/dashboard", methods=["GET"])
def dashboard() -> Any:
    """Render the API Shield Web Dashboard."""
    return render_template("index.html")


@api_bp.route("/api/ui/overview", methods=["GET"])
def ui_overview() -> Any:
    """Aggregate real-time metrics, Redis keys, active blocks, and recent audit logs."""
    policy_mgr: PolicyManager = current_app.config["POLICY_MANAGER"]
    redis_store = current_app.config.get("REDIS_STORE")
    cfg: Config = current_app.config["SHIELD_CONFIG"]

    active_policies = [p.to_dict() for p in policy_mgr.list()]

    redis_keys = []
    blocked_clients = []
    redis_ok = False

    if redis_store and hasattr(redis_store, "client"):
        try:
            redis_ok = bool(redis_store.ping())
            raw_keys = redis_store.client.keys("*")
            for k in sorted(raw_keys):
                ttl = redis_store.client.ttl(k)
                ktype = redis_store.client.type(k)
                redis_keys.append(
                    {
                        "key": k,
                        "type": ktype,
                        "ttl": max(0, ttl) if ttl and ttl > 0 else (0 if ttl == -2 else -1),
                    }
                )
                if k.startswith("block:"):
                    cid = k[len("block:") :]
                    violations = redis_store.get_violations(cid)
                    blocked_clients.append(
                        {
                            "client_id": cid,
                            "masked_client": mask_client_id(cid),
                            "ttl": max(0, ttl) if ttl and ttl > 0 else 0,
                            "violations": violations,
                        }
                    )
        except Exception as exc:
            logger.warning("Failed querying Redis for UI overview: %s", exc)

    # Read recent logs
    recent_logs = []
    log_file = Path(cfg.log_file)
    if log_file.exists():
        try:
            lines = log_file.read_text(encoding="utf-8").strip().splitlines()
            for line in reversed(lines[-60:]):
                try:
                    recent_logs.append(json.loads(line))
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("Failed reading log file: %s", exc)

    uptime = round(time.time() - APP_START_TIME, 1)

    return (
        jsonify(
            {
                "uptime": uptime,
                "redis_connected": redis_ok,
                "policies": active_policies,
                "keys": redis_keys,
                "blocked_clients": blocked_clients,
                "recent_logs": recent_logs,
                "admin_key": cfg.admin_api_key,
            }
        ),
        200,
    )


@api_bp.route("/api/ui/unblock", methods=["POST"])
@api_bp.route("/api/ui/unblock/<path:client_id>", methods=["POST"])
def ui_unblock(client_id: Optional[str] = None) -> Any:
    """Unblock a client for testing and simulation purposes."""
    if not client_id:
        data = request.get_json(silent=True) or {}
        client_id = data.get("client_id")
    if not client_id:
        return jsonify({"error": "client_id is required"}), 400

    redis_store = current_app.config.get("REDIS_STORE")
    if redis_store and hasattr(redis_store, "client"):
        redis_store.client.delete(f"block:{client_id}")
        return jsonify({"message": f"Client {client_id} successfully unblocked"}), 200

    return jsonify({"error": "Redis store unavailable"}), 500


# ============================================================================
# Error Handlers (SRS Section 13)
# ============================================================================


def register_error_handlers(app: Flask) -> None:
    """Register custom error handlers producing uniform JSON responses."""

    @app.errorhandler(PolicyValidationError)
    def handle_validation_error(exc: PolicyValidationError) -> Any:
        return jsonify({"error": "Invalid policy", "details": exc.message}), 400

    @app.errorhandler(DuplicatePolicyError)
    def handle_duplicate_error(exc: DuplicatePolicyError) -> Any:
        return jsonify({"error": "Duplicate policy", "details": exc.message}), 409

    @app.errorhandler(PolicyNotFoundError)
    def handle_policy_not_found(exc: PolicyNotFoundError) -> Any:
        return jsonify({"error": "Policy not found", "details": exc.message}), 404

    @app.errorhandler(400)
    def handle_bad_request(exc: Any) -> Any:
        desc = getattr(exc, "description", "Bad Request")
        return jsonify({"error": "Invalid policy", "details": desc}), 400

    @app.errorhandler(401)
    def handle_unauthorized(exc: Any) -> Any:
        return jsonify({"error": "Unauthorized"}), 401

    @app.errorhandler(404)
    def handle_not_found(exc: Any) -> Any:
        return jsonify({"error": "Not found"}), 404

    @app.errorhandler(405)
    def handle_method_not_allowed(exc: Any) -> Any:
        return jsonify({"error": "Method not allowed"}), 405

    @app.errorhandler(429)
    def handle_too_many_requests(exc: Any) -> Any:
        return jsonify(
            {
                "error": "Rate limit exceeded",
                "message": "Please try again later",
            }
        ), 429

    @app.errorhandler(500)
    def handle_server_error(exc: Any) -> Any:
        logger.exception("Internal application error: %s", exc)
        return jsonify({"error": "Internal server error"}), 500

    @app.errorhandler(Exception)
    def handle_generic_exception(exc: Exception) -> Any:
        logger.exception("Unhandled exception: %s", exc)
        return jsonify({"error": "Internal server error"}), 500
