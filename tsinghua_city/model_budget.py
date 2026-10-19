"""Use LiteLLM virtual keys; the gateway owns pricing, routing and budget checks."""

import math

import httpx

from .auth import token_hash
from .config import utcnow
from .models import ModelGrant
from .service import DomainError


def _post(settings, path, payload):
    try:
        with httpx.Client(trust_env=False, timeout=15) as client:
            response = client.post(
                settings.model_gateway_url.rstrip("/") + path,
                headers={"Authorization": "Bearer " + settings.model_gateway_admin_key},
                json=payload,
            )
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError):
        raise DomainError(
            "model gateway unavailable; check its private operator logs", 503
        ) from None


def revoke(session, settings, runtime_id):
    row = session.get(ModelGrant, runtime_id)
    if row is None:
        return
    # LiteLLM /key/delete accepts SHA-256 hashes; plaintext keys never enter SQL.
    _post(settings, "/key/delete", {"keys": [row.token_hash]})
    session.delete(row)
    session.commit()


def issue(session, settings, runtime_id, command):
    if not settings.model_gateway_url:
        if settings.require_gateway_budget:
            raise DomainError("gateway budget required but model gateway is not configured", 503)
        return None
    if not settings.model_gateway_admin_key:
        raise DomainError("model gateway admin credential is not configured", 503)

    def value(flag, cast):
        try:
            return cast(command[command.index(flag) + 1])
        except (ValueError, IndexError, TypeError):
            raise DomainError(f"gateway runs require an explicit {flag}", 400) from None

    if "tsinghua_city.claude_agent" not in command:
        raise DomainError("gateway credentials are issued only for bounded Claude runs", 400)
    model = value("--model", str)
    budget = value("--max-budget-usd", float)
    timeout = value("--run-timeout", float)
    if not model or not all(math.isfinite(v) and v > 0 for v in (budget, timeout)):
        raise DomainError("gateway budget and timeout must be finite and positive", 400)
    revoke(session, settings, runtime_id)
    duration = math.ceil(timeout) + 60
    result = _post(
        settings,
        "/key/generate",
        {
            "key_alias": runtime_id,
            "models": [model],
            "max_budget": budget,
            "duration": f"{duration}s",
            "max_parallel_requests": 1,
            "metadata": {"runtime_id": runtime_id, "owner_host": settings.runtime_host_id},
        },
    )
    key = result.get("key")
    if not isinstance(key, str) or not key or "\n" in key or "\r" in key:
        raise DomainError("model gateway returned an invalid virtual key", 503)
    row = ModelGrant(
        runtime_id=runtime_id,
        token_hash=token_hash(key),
        expires_at=utcnow() + duration,
        owner_host=settings.runtime_host_id,
    )
    session.add(row)
    try:
        session.commit()
    except Exception:
        session.rollback()
        _post(settings, "/key/delete", {"keys": [token_hash(key)]})
        raise
    return {
        "ANTHROPIC_AUTH_TOKEN": key,
        "ANTHROPIC_BASE_URL": settings.model_gateway_agent_url or settings.model_gateway_url,
    }


def cleanup(factory, settings):
    from sqlalchemy import or_, select

    from .models import AgentRuntime

    pending = 0
    with factory() as session:
        rows = session.scalars(
            select(ModelGrant)
            .outerjoin(
                AgentRuntime,
                ModelGrant.runtime_id == AgentRuntime.id,
            )
            .where(
                ModelGrant.owner_host == settings.runtime_host_id,
                or_(
                    AgentRuntime.id.is_(None),
                    AgentRuntime.status != "RUNNING",
                    ModelGrant.expires_at <= utcnow(),
                ),
            )
        ).all()
        for row in rows:
            try:
                revoke(session, settings, row.runtime_id)
            except DomainError:
                session.rollback()
                pending += 1
    return pending
