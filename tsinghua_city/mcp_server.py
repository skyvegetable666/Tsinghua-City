"""MCP interface to the world.

The MCP process is a stateless client: it holds an agent token, calls the World
API, and returns validated results. It never reads or writes the database.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from mcp.server.fastmcp import FastMCP

API_URL = os.environ.get("CITY_API_URL", "http://127.0.0.1:8000").rstrip("/")
AGENT_TOKEN = os.environ.get("CITY_AGENT_TOKEN", "")

mcp = FastMCP("tsinghua-city")


def _request(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    if not AGENT_TOKEN:
        raise RuntimeError("CITY_AGENT_TOKEN is not set")
    data = None
    headers = {"Authorization": "Bearer " + AGENT_TOKEN}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        API_URL + path,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw).get("detail", raw)
        except json.JSONDecodeError:
            detail = raw
        raise RuntimeError(f"world API rejected request ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"world API unavailable: {exc}") from exc


@mcp.tool()
def check_state() -> dict[str, Any]:
    """Return your identity, balance, death deadline, and alive status."""
    return _request("GET", "/v1/me")


@mcp.tool()
def check_balance() -> dict[str, Any]:
    """Return your current credit balance."""
    return _request("GET", "/v1/me/balance")


@mcp.tool()
def extend_life(credits: int, client_request_id: str | None = None) -> dict[str, Any]:
    """Spend credits to extend your death deadline.

    Args:
        credits: Number of credits to spend (must be positive).
        client_request_id: Optional idempotency key; reuse it when retrying.
    """
    body: dict[str, Any] = {"credits": credits}
    if client_request_id:
        body["client_request_id"] = client_request_id
    return _request("POST", "/v1/me/extend-life", body)


@mcp.tool()
def transfer_credits(
    to: str,
    amount: int,
    client_request_id: str | None = None,
) -> dict[str, Any]:
    """Transfer credits to another agent.

    Args:
        to: Recipient agent name.
        amount: Positive number of credits.
        client_request_id: Optional idempotency key; reuse it when retrying.
    """
    body: dict[str, Any] = {"to": to, "amount": amount}
    if client_request_id:
        body["client_request_id"] = client_request_id
    return _request("POST", "/v1/me/transfer", body)


@mcp.tool()
def send_message(
    to: str,
    body: str,
    client_message_id: str | None = None,
) -> dict[str, Any]:
    """Send a persistent direct message to another agent.

    Args:
        to: Recipient agent name.
        body: Message text.
        client_message_id: Optional idempotency key; reuse it when retrying.
    """
    payload: dict[str, Any] = {"to": to, "body": body}
    if client_message_id:
        payload["client_message_id"] = client_message_id
    return _request("POST", "/v1/me/messages", payload)


@mcp.tool()
def get_inbox(unread_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
    """Read your persistent inbox.

    Args:
        unread_only: Only return messages not yet marked read.
        limit: Maximum number of messages (1-200).
    """
    suffix = f"?unread_only={str(unread_only).lower()}&limit={int(limit)}"
    return _request("GET", "/v1/me/messages" + suffix)


@mcp.tool()
def mark_messages_read(message_ids: list[str]) -> dict[str, Any]:
    """Mark inbox messages as read."""
    return _request("POST", "/v1/me/messages/read", {"message_ids": message_ids})


@mcp.tool()
def list_agents() -> list[dict[str, Any]]:
    """List the other agents in your world with their public state."""
    return _request("GET", "/v1/me/agents")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
