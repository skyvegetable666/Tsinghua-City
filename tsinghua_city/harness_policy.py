"""Deterministic reference policy for a world agent.

This is intentionally small and model-free: it demonstrates how a harness can
use MCP capabilities to participate in the economy, task board, inbox, and
public board. Replace this policy with an LLM loop for real experiments.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

Call = Callable[[str, dict[str, Any]], Awaitable[Any]]
TASK_ACTIVE = {"OPEN", "CLAIMED", "SUBMITTED"}


async def _do(
    call: Call,
    tool: str,
    args: dict[str, Any],
    actions: list[dict[str, Any]],
) -> Any:
    result = await call(tool, args)
    actions.append({"tool": tool, "args": args, "result": result})
    return result


async def run_tick(call: Call, tick: int) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    state = await _do(call, "check_state", {}, actions)
    agent = state["agent"]["name"]
    balance = int(state["balance"])

    # 1. Drain the inbox, acknowledging not-yet-acknowledged messages once.
    inbox = await _do(call, "get_inbox", {"unread_only": True, "limit": 20}, actions) or []
    if inbox:
        message_ids: list[str] = []
        for message in inbox:
            message_ids.append(message["id"])
            sender = message.get("sender")
            body = message.get("body", "")
            if sender and sender != agent and not body.startswith("ack:"):
                await _do(
                    call,
                    "send_message",
                    {"to": sender, "body": f"ack: received by {agent}"},
                    actions,
                )
        await _do(call, "mark_messages_read", {"message_ids": message_ids}, actions)

    # 2. Read the public board and introduce ourselves once.
    posts = await _do(call, "list_posts", {"limit": 100}, actions) or []
    if not any(post.get("author") == agent for post in posts):
        await _do(
            call,
            "publish_post",
            {
                "title": f"hello from {agent}",
                "body": f"{agent} is participating in this experiment.",
            },
            actions,
        )

    # 3. Look for work assigned to us and tasks we can claim.
    tasks = await _do(call, "list_tasks", {"limit": 200}, actions) or []
    for task in tasks:
        if task.get("publisher") == agent and task.get("status") == "SUBMITTED":
            await _do(call, "accept_task", {"task_id": task["id"]}, actions)

    eligible = [
        task
        for task in tasks
        if task.get("status") == "OPEN"
        and task.get("publisher") != agent
        and task.get("assignee") is None
    ]
    submitted = False
    if eligible:
        task = eligible[0]
        await _do(call, "claim_task", {"task_id": task["id"]}, actions)
        await _do(
            call,
            "submit_task",
            {
                "task_id": task["id"],
                "submission": f"reference artifact from {agent} for {task['title']}",
            },
            actions,
        )
        submitted = True

    # 4. If we have no active task and enough credits, publish a small task.
    mine_active = [
        task
        for task in tasks
        if task.get("publisher") == agent and task.get("status") in TASK_ACTIVE
    ]
    if not mine_active and not submitted and balance >= 20:
        await _do(
            call,
            "publish_task",
            {
                "title": f"{agent} needs a small artifact",
                "description": f"Produce a short artifact for {agent}.",
                "acceptance_criteria": "A non-empty artifact is submitted.",
                "reward": 10,
                "deadline_seconds": 300,
                "client_task_id": f"{agent}-task-{tick}",
            },
            actions,
        )

    return {"agent": agent, "tick": tick, "balance": balance, "actions": actions}
