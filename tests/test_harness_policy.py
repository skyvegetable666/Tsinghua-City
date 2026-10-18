"""The reference harness policy is deterministic and MCP-only."""

from __future__ import annotations

from typing import Any

from tsinghua_city.harness_policy import run_tick


async def test_reference_tick_publishes_intro_and_task():
    calls: list[tuple[str, dict[str, Any]]] = []

    async def call(tool: str, args: dict[str, Any]) -> Any:
        calls.append((tool, args))
        return {
            "check_state": {"agent": {"name": "alice"}, "balance": 100},
            "get_inbox": None,
            "list_posts": None,
            "list_tasks": None,
            "publish_post": {"id": "p1"},
            "publish_task": {"id": "t1"},
        }[tool]

    result = await run_tick(call, 0)
    assert result["agent"] == "alice"
    assert [name for name, _ in calls] == [
        "check_state",
        "get_inbox",
        "list_posts",
        "publish_post",
        "list_tasks",
        "publish_task",
    ]


async def test_reference_tick_acknowledges_claims_and_accepts():
    calls: list[tuple[str, dict[str, Any]]] = []

    async def call(tool: str, args: dict[str, Any]) -> Any:
        calls.append((tool, args))
        return {
            "check_state": {"agent": {"name": "alice"}, "balance": 100},
            "get_inbox": [
                {"id": "m1", "sender": "bob", "body": "hello"},
                {"id": "m2", "sender": "bob", "body": "ack: received"},
            ],
            "list_posts": [{"id": "p1", "author": "alice"}],
            "list_tasks": [
                {
                    "id": "t1",
                    "publisher": "alice",
                    "assignee": "bob",
                    "status": "SUBMITTED",
                },
                {
                    "id": "t2",
                    "publisher": "bob",
                    "assignee": None,
                    "status": "OPEN",
                    "title": "job",
                },
            ],
            "send_message": {"id": "m3"},
            "mark_messages_read": {"changed": 2},
            "accept_task": {"id": "t1"},
            "claim_task": {"id": "t2"},
            "submit_task": {"id": "t2"},
        }[tool]

    result = await run_tick(call, 0)
    names = [name for name, _ in calls]
    assert names == [
        "check_state",
        "get_inbox",
        "send_message",
        "mark_messages_read",
        "list_posts",
        "list_tasks",
        "accept_task",
        "claim_task",
        "submit_task",
    ]
    assert result["actions"]
