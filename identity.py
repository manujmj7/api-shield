"""identity.py - Client identification and privacy masking for API Shield.

Why this module exists:
Accurate client identification is required to track request rates independently per client
(SRS 3.1, BR-07). We prioritize authenticated API keys over raw IP addresses.
To prevent security leaks, raw API keys must never be logged or stored in plain text
in telemetry (NFR Security).

Security Note on X-Forwarded-For:
We deliberately do NOT trust X-Forwarded-For headers. Any untrusted client can send
arbitrary X-Forwarded-For headers to rotate identities and bypass IP-based rate limiting.
Unless the application sits behind a verified reverse proxy configured to strip
untrusted headers, request.remote_addr is the only trustworthy network identity.
"""

from __future__ import annotations

import hashlib
from typing import Any


def get_client_id(request: Any) -> str:
    """Extract a consistent client identity identifier from an incoming Flask request.

    Priority (BR-07):
    1. 'X-API-Key' header if present and non-empty -> 'key:<value>'
    2. Fallback to client IP -> 'ip:<remote_addr>'

    Why:
    API keys represent authenticated, explicit tenant/user identities, while IP addresses
    represent shared networks (such as NATs or offices). Stripping whitespace prevents
    subtle identity mismatch caused by accidental spaces.
    """
    api_key = request.headers.get("X-API-Key")
    if api_key is not None:
        cleaned_key = api_key.strip()
        if cleaned_key:
            return f"key:{cleaned_key}"

    remote_ip = getattr(request, "remote_addr", None) or "127.0.0.1"
    return f"ip:{str(remote_ip).strip()}"


def mask_client_id(client_id: str) -> str:
    """Mask sensitive client identifiers before logging or telemetry.

    Format:
    - 'key:secret-key-123' -> 'key:se***<sha256_prefix_6>'
    - 'ip:192.168.1.1' -> 'ip:192.168.1.1' (IP addresses remain unmasked for operational visibility)

    Why:
    Secret API keys must never appear in application logs or metrics (NFR Security).
    Using the first two characters plus a 6-character SHA-256 fingerprint allows developers
    to correlate requests in logs without exposing credentials.
    """
    if not client_id.startswith("key:"):
        return client_id

    raw_key = client_id[4:]
    first_two = raw_key[:2] if len(raw_key) >= 2 else raw_key
    sha_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:6]
    return f"key:{first_two}***{sha_hash}"
