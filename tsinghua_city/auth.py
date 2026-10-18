"""Bearer-token helpers.

Agent tokens are opaque secrets; only their SHA-256 digest is stored. The admin
token is process configuration (CITY_ADMIN_TOKEN), not a database row.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets


def generate_agent_token() -> str:
    return "city_" + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def extract_bearer(header: str | None) -> str | None:
    if not header:
        return None
    prefix = "Bearer "
    if not header.startswith(prefix):
        return None
    value = header[len(prefix) :].strip()
    return value or None


def constant_compare(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def admin_matches(provided: str | None, expected: str) -> bool:
    return provided is not None and bool(provided) and constant_compare(provided, expected)
