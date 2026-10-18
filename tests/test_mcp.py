"""MCP tool functions are thin, stateless API clients."""

from __future__ import annotations

from typing import Any

from tsinghua_city import mcp_server


def test_tools_delegate_to_world_api(monkeypatch):
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        calls.append((method, path, body))
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "_request", fake)

    assert mcp_server.check_state() == {"ok": True}
    assert mcp_server.check_balance() == {"ok": True}
    assert mcp_server.extend_life(3) == {"ok": True}
    assert mcp_server.transfer_credits("bob", 2) == {"ok": True}
    assert mcp_server.send_message("bob", "hi") == {"ok": True}
    assert mcp_server.mark_messages_read(["m1"]) == {"ok": True}
    assert mcp_server.list_agents() == {"ok": True}
    assert mcp_server.get_inbox(unread_only=True, limit=5) == {"ok": True}
    assert mcp_server.list_tasks(status="open", limit=5) == {"ok": True}
    assert mcp_server.get_task("t1") == {"ok": True}
    assert mcp_server.publish_task("t", "d", "a", 10) == {"ok": True}
    assert mcp_server.claim_task("t1") == {"ok": True}
    assert mcp_server.submit_task("t1", "done") == {"ok": True}
    assert mcp_server.accept_task("t1") == {"ok": True}
    assert mcp_server.reject_task("t1", "more") == {"ok": True}
    assert mcp_server.cancel_task("t1") == {"ok": True}

    assert calls == [
        ("GET", "/v1/me", None),
        ("GET", "/v1/me/balance", None),
        ("POST", "/v1/me/extend-life", {"credits": 3}),
        ("POST", "/v1/me/transfer", {"to": "bob", "amount": 2}),
        ("POST", "/v1/me/messages", {"to": "bob", "body": "hi"}),
        ("POST", "/v1/me/messages/read", {"message_ids": ["m1"]}),
        ("GET", "/v1/me/agents", None),
        ("GET", "/v1/me/messages?unread_only=true&limit=5", None),
        ("GET", "/v1/me/tasks?limit=5&status=OPEN", None),
        ("GET", "/v1/me/tasks/t1", None),
        (
            "POST",
            "/v1/me/tasks",
            {
                "title": "t",
                "description": "d",
                "acceptance_criteria": "a",
                "reward": 10,
            },
        ),
        ("POST", "/v1/me/tasks/t1/claim", None),
        ("POST", "/v1/me/tasks/t1/submit", {"submission": "done"}),
        ("POST", "/v1/me/tasks/t1/accept", None),
        ("POST", "/v1/me/tasks/t1/reject", {"reason": "more"}),
        ("POST", "/v1/me/tasks/t1/cancel", None),
    ]
