#!/usr/bin/env python3
"""MCP stdio server exposing the single agent_chat tool.

Claude Code spawns this process. The tool forwards to the local bridge, which
validates the message and asks the supervisor to route it.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("agentchat")

LOCAL_URL = os.environ.get("AGENT_LOCAL_URL", "http://127.0.0.1:8765").rstrip("/")
LOCAL_TOKEN = os.environ.get("AGENT_LOCAL_TOKEN", "")


def _call_bridge(recipient: str, message: str) -> dict:
    payload = json.dumps({"recipient": recipient, "message": message}).encode("utf-8")
    request = urllib.request.Request(
        LOCAL_URL + "/internal/agent_chat",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + LOCAL_TOKEN,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": f"bridge HTTP {exc.code}"}
    except Exception as exc:
        return {"ok": False, "error": f"bridge unreachable: {exc}"}


@mcp.tool()
def agent_chat(recipient: str, message: str) -> str:
    """Send a chat message to the other agent in this world.

    This is the only communication channel between the two agents. The
    supervisor routes the message asynchronously: your peer receives it at their
    next safe boundary (between tool calls, or at the start of a new turn). The
    call returns immediately and does not wait for a reply.

    Args:
        recipient: Name of the other agent (your peer), e.g. "agent-b".
        message: The message text. Keep it under 4000 characters.

    Returns:
        A JSON object with message_id, peer_state, and delivery information,
        or an object with ok=false and an error string.
    """
    result = _call_bridge(recipient, message)
    return json.dumps(result, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
