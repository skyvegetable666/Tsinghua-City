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




@mcp.tool()
def list_tasks(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """List the public task board.

    Args:
        status: Optional OPEN / CLAIMED / SUBMITTED / COMPLETED / CANCELLED / FAILED filter.
        limit: Maximum number of tasks (1-500).
    """
    suffix = f"?limit={int(limit)}"
    if status:
        suffix += "&status=" + status.upper()
    return _request("GET", "/v1/me/tasks" + suffix)


@mcp.tool()
def get_task(task_id: str) -> dict[str, Any]:
    """Read one task by id."""
    return _request("GET", "/v1/me/tasks/" + task_id)


@mcp.tool()
def publish_task(
    title: str,
    description: str,
    acceptance_criteria: str,
    reward: int,
    deadline_seconds: int | None = None,
) -> dict[str, Any]:
    """Publish a task and escrow its reward.

    Args:
        title: Short task title.
        description: What needs to be done.
        acceptance_criteria: What must be true for acceptance.
        reward: Credits reserved from your balance.
        deadline_seconds: Optional deadline relative to now.
    """
    body: dict[str, Any] = {
        "title": title,
        "description": description,
        "acceptance_criteria": acceptance_criteria,
        "reward": reward,
    }
    if deadline_seconds is not None:
        body["deadline_seconds"] = deadline_seconds
    return _request("POST", "/v1/me/tasks", body)


@mcp.tool()
def claim_task(task_id: str) -> dict[str, Any]:
    """Claim an open task."""
    return _request("POST", f"/v1/me/tasks/{task_id}/claim")


@mcp.tool()
def submit_task(task_id: str, submission: str) -> dict[str, Any]:
    """Submit work for a task you currently hold."""
    return _request("POST", f"/v1/me/tasks/{task_id}/submit", {"submission": submission})


@mcp.tool()
def accept_task(task_id: str) -> dict[str, Any]:
    """Accept a submitted task you published, releasing its escrow."""
    return _request("POST", f"/v1/me/tasks/{task_id}/accept")


@mcp.tool()
def reject_task(task_id: str, reason: str | None = None) -> dict[str, Any]:
    """Reject a submission and reopen your task."""
    body: dict[str, Any] = {}
    if reason:
        body["reason"] = reason
    return _request("POST", f"/v1/me/tasks/{task_id}/reject", body)


@mcp.tool()
def cancel_task(task_id: str) -> dict[str, Any]:
    """Cancel one of your open tasks and refund its escrow."""
    return _request("POST", f"/v1/me/tasks/{task_id}/cancel")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
