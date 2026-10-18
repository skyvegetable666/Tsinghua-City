"""Web console API tests (no Docker, no browser)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from supervisor.settings import RunContext
from supervisor.webconsole import WebConsole


class FakeBroker:
    phase = "running"

    def __init__(self) -> None:
        self.said: list[tuple[str, str]] = []
        self.end_reason: str | None = None

    def status(self) -> dict:
        return {
            "run_id": "run-web",
            "phase": self.phase,
            "agents": {
                "a": {"name": "alice", "state": "idle", "connected": True, "registered": True,
                      "turn": 2, "model": "mock", "cost_usd": 0.01,
                      "usage": {"input_tokens": 10, "output_tokens": 4}},
                "b": {"name": "bob", "state": "working", "connected": True, "registered": True,
                      "turn": 1, "model": "mock", "cost_usd": 0.02,
                      "usage": {"input_tokens": 20, "output_tokens": 8}},
            },
        }

    async def inject_system_message(self, text: str, target: str = "all") -> list[str]:
        if not text.strip():
            raise ValueError("message text is empty")
        self.said.append((text, target))
        return ["s000001"]

    async def request_end(self, reason: str) -> bool:
        self.end_reason = reason
        return True


@pytest.fixture
async def console(tmp_path: Path):
    run_dir = tmp_path / "run-web"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text(
        json.dumps({"event": "run_started", "run_id": "run-web", "ts": time.time()}) + "\n"
        + json.dumps({"event": "message_sent", "message": {"id": "m000001", "sender": "alice", "recipient": "bob", "text": "hi"}, "ts": time.time()}) + "\n",
        encoding="utf-8",
    )
    run = RunContext(run_id="run-web", run_dir=run_dir, token="t", started_at=time.time())
    broker = FakeBroker()
    settings = SimpleNamespace(web_host="127.0.0.1", web_port=0)
    app_console = WebConsole(settings, run, broker)  # type: ignore[arg-type]
    server = TestServer(app_console.make_app())
    await server.start_server()
    session = aiohttp.ClientSession()
    base = str(server.make_url("/")).rstrip("/")
    try:
        yield {"console": app_console, "server": server, "session": session, "base": base, "broker": broker, "run": run}
    finally:
        await session.close()
        await server.close()


async def test_index_and_status(console):
    async with console["session"].get(console["base"] + "/") as response:
        assert response.status == 200
        text = await response.text()
        assert "Genesis Lab Console" in text
        assert "/api/events/stream" in text
    async with console["session"].get(console["base"] + "/api/status") as response:
        assert response.status == 200
        data = await response.json()
        assert data["status"]["run_id"] == "run-web"
        assert len(data["status"]["agents"]) == 2


async def test_say_and_end(console):
    async with console["session"].post(
        console["base"] + "/api/say",
        json={"text": "hello", "to": "bob"},
    ) as response:
        assert response.status == 200
        data = await response.json()
        assert data["message_ids"] == ["s000001"]
    assert console["broker"].said == [("hello", "bob")]

    async with console["session"].post(
        console["base"] + "/api/end",
        json={"reason": "test"},
    ) as response:
        data = await response.json()
        assert data["ok"] is True
    assert console["broker"].end_reason == "test"


async def test_events_and_report(console):
    async with console["session"].get(console["base"] + "/api/events?since=1") as response:
        data = await response.json()
        assert data["events"][0]["event"] == "message_sent"
        assert data["next"] == 2
    async with console["session"].get(console["base"] + "/api/report") as response:
        data = await response.json()
        assert data["ok"] is True
        assert (console["run"].run_dir / "metrics.json").exists()
        assert (console["run"].run_dir / "transcript.md").exists()


async def test_start_stop(console):
    settings = SimpleNamespace(web_host="127.0.0.1", web_port=0)
    run = console["run"]
    broker = FakeBroker()
    real = WebConsole(settings, run, broker)  # type: ignore[arg-type]
    await real.start()
    try:
        assert real.url.startswith("http://127.0.0.1:")
        async with (
            aiohttp.ClientSession() as session,
            session.get(real.url + "/api/status") as response,
        ):
            assert response.status == 200
    finally:
        await real.stop()
