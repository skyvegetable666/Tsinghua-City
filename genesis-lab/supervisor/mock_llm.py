"""A tiny scripted Anthropic-compatible endpoint.

This exists so the whole experiment can be exercised without an API key:
Claude Code in the containers talks to this server instead of DeepSeek. It is
plumbing validation, not a model: replies are canned and the agents will sound
mechanical.

Both streaming (SSE) and non-streaming JSON responses are implemented because
Claude Code may probe the endpoint with either.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MCP_TOOL_NAME = "mcp__agentchat__agent_chat"


def _sse(event: str | None, payload: dict) -> bytes:
    out = b""
    if event:
        out += f"event: {event}\n".encode()
    out += b"data: " + json.dumps(payload).encode() + b"\n\n"
    return out


def _message_start(model: str, message_id: str) -> dict:
    return {
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 1},
        },
    }


def _text_block(index: int, text: str) -> list[tuple[str, dict]]:
    return [
        ("content_block_start", {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": index}),
    ]


def _tool_block(index: int, tool_id: str, name: str, tool_input: dict) -> list[tuple[str, dict]]:
    return [
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps(tool_input)},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": index}),
    ]


class MockLLM:
    def __init__(
        self,
        port: int = 8790,
        *,
        host: str = "0.0.0.0",
        mode: str = "demo",
        log_path: Path | None = None,
        delay_s: float = 0.2,
        tool_command: str = "echo MOCK-TOOL-OK",
        roster: dict[str, str | list[str]] | None = None,
    ) -> None:
        self.port = port
        self.host = host
        self.mode = mode
        self.log_path = Path(log_path) if log_path else None
        self.delay_s = delay_s
        self.tool_command = tool_command
        raw = roster or {"agent-a": "agent-b", "agent-b": "agent-a"}
        self.roster: dict[str, list[str]] = {
            name: ([peer] if isinstance(peer, str) else list(peer))
            for name, peer in raw.items()
        }
        self.records: list[dict] = []
        self._lock = threading.Lock()
        self._calls: dict[str, int] = {}
        self._sent: dict[str, int] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    # ---------------------------------------------------------------- server
    def start(self) -> None:
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path.startswith("/health"):
                    self._json(200, {"ok": True, "mode": mock.mode})
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError:
                    body = {"_raw": raw.decode("utf-8", "replace")}
                mock._record(self.path, body)
                if self.path.startswith("/v1/messages/count_tokens"):
                    self._json(200, {"input_tokens": 100})
                    return
                if not self.path.startswith("/v1/messages"):
                    self._json(200, {})
                    return
                payload, content_type = mock._response(body)
                self.send_response(200)
                self.send_header("content-type", content_type)
                self.send_header("cache-control", "no-cache")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _json(self, status: int, data: dict):
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    # -------------------------------------------------------------- response
    def _record(self, path: str, body: dict) -> None:
        record = {"ts": time.time(), "path": path, "body": body}
        with self._lock:
            self.records.append(record)
            if self.log_path is not None:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _agent(self, body: dict) -> tuple[str, str]:
        # The genesis prompt is the first user message. Prefer it over the whole
        # conversation so a peer quoting "You are ..." cannot confuse the role.
        messages = body.get("messages") or []
        first_text = ""
        if messages and isinstance(messages[0], dict):
            content = messages[0].get("content")
            if isinstance(content, str):
                first_text = content
            elif isinstance(content, list):
                first_text = " ".join(
                    str(block.get("text") or "")
                    for block in content
                    if isinstance(block, dict)
                )
        for name, peers in self.roster.items():
            if f"You are {name}" in first_text:
                return name, (peers[0] if peers else "peer")
        blob = json.dumps(body)
        for name, peers in self.roster.items():
            if f"You are {name}" in blob:
                return name, (peers[0] if peers else "peer")
        return "agent", "peer"

    def _new_tool_result(self, messages: list[dict]) -> bool:
        last_assistant = -1
        for index, item in enumerate(messages):
            if item.get("role") == "assistant":
                last_assistant = index
        for item in messages[last_assistant + 1 :]:
            content = item.get("content")
            if isinstance(content, list) and any(
                isinstance(block, dict) and block.get("type") == "tool_result" for block in content
            ):
                return True
        return False

    def _new_incoming(self, messages: list[dict]) -> bool:
        last_assistant = -1
        for index, item in enumerate(messages):
            if item.get("role") == "assistant":
                last_assistant = index
        for item in messages[last_assistant + 1 :]:
            if "[INCOMING MESSAGES" in json.dumps(item, ensure_ascii=False):
                return True
        return False

    def _decide(self, body: dict) -> dict:
        agent, peer = self._agent(body)
        messages = body.get("messages") or []
        new_tool_result = self._new_tool_result(messages)
        new_incoming = self._new_incoming(messages)
        assistant_turns = sum(1 for item in messages if item.get("role") == "assistant")
        with self._lock:
            calls = self._calls.get(agent, 0)
            self._calls[agent] = calls + 1

        plan = {
            "agent": agent,
            "peer": peer,
            "calls": calls,
            "text": "",
            "tool": None,
            "stop_reason": "end_turn",
        }

        if self.mode == "tool_then_end":
            if new_tool_result:
                plan["text"] = "Tool finished. I am idle now."
            else:
                plan["text"] = "Let me run a tool so a tool boundary exists."
                plan["tool"] = ("Bash", {"command": self.tool_command, "description": "mock tool"})
                plan["stop_reason"] = "tool_use"
            return plan

        # demo mode: greet once, reply once per delivered batch, then go quiet.
        # Decisions are derived from message history, so a streaming retry with
        # identical messages gets an identical answer.
        if assistant_turns == 0:
            plan["text"] = "Hello. I am here. I will contact my peer."
            plan["tool"] = (
                MCP_TOOL_NAME,
                {
                    "recipient": peer,
                    "message": f"Hello from {agent}. I appear to be alive in this world. Are you there?",
                },
            )
            plan["stop_reason"] = "tool_use"
        elif new_incoming and assistant_turns < 6:
            plan["text"] = "A message arrived. I will answer it."
            plan["tool"] = (
                MCP_TOOL_NAME,
                {
                    "recipient": peer,
                    "message": f"{agent} received your message and is still here. (mock reply {assistant_turns})",
                },
            )
            plan["stop_reason"] = "tool_use"
        elif new_tool_result:
            plan["text"] = "Message handed to the supervisor. Waiting."
        else:
            plan["text"] = "Nothing more to add for now. I will wait."
        return plan

    def _response(self, body: dict) -> tuple[bytes, str]:
        model = str(body.get("model") or "mock-model")
        plan = self._decide(body)
        message_id = f"msg_mock_{int(time.time() * 1000)}_{plan['calls']}"
        if self.delay_s:
            time.sleep(self.delay_s)
        tool = plan["tool"]
        tool_id = f"toolu_mock_{plan['calls']}"

        if body.get("stream"):
            chunks: list[tuple[str, dict]] = [_message_start(model, message_id)]
            index = 0
            if plan["text"]:
                chunks += _text_block(index, plan["text"])
                index += 1
            if tool:
                chunks += _tool_block(index, tool_id, tool[0], tool[1])
            chunks += [
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": plan["stop_reason"], "stop_sequence": None},
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                        },
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            ]
            return b"".join(_sse(name, data) for name, data in chunks), "text/event-stream"

        content: list[dict] = []
        if plan["text"]:
            content.append({"type": "text", "text": plan["text"]})
        if tool:
            content.append(
                {"type": "tool_use", "id": tool_id, "name": tool[0], "input": tool[1]}
            )
        message = {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": plan["stop_reason"],
            "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }
        return json.dumps(message).encode(), "application/json"
