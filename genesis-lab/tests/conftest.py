"""Shared helpers for the test suite."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


class EventStream:
    """Minimal SSE client used by broker tests."""

    def __init__(self, session, url: str, headers: dict[str, str]) -> None:
        self._session = session
        self._url = url
        self._headers = headers
        self.queue: asyncio.Queue = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.status: int | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run())
        await asyncio.sleep(0.05)

    async def _run(self) -> None:
        try:
            async with self._session.get(self._url, headers=self._headers) as response:
                self.status = response.status
                if response.status != 200:
                    await self.queue.put({"error": f"status {response.status}"})
                    return
                event_type = "message"
                event_id = None
                data: list[str] = []
                async for raw in response.content:
                    line = raw.decode("utf-8").rstrip("\r\n")
                    if line == "":
                        if data:
                            try:
                                payload = json.loads("\n".join(data))
                            except json.JSONDecodeError:
                                payload = {"raw": "\n".join(data)}
                            await self.queue.put(
                                {"event": event_type, "id": event_id, "data": payload}
                            )
                        event_type = "message"
                        event_id = None
                        data = []
                    elif line.startswith(":"):
                        continue
                    elif line.startswith("event:"):
                        event_type = line[len("event:") :].strip()
                    elif line.startswith("data:"):
                        data.append(line[len("data:") :].lstrip())
                    elif line.startswith("id:"):
                        event_id = line[len("id:") :].strip()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # pragma: no cover - test helper
            await self.queue.put({"error": repr(exc)})

    async def next(self, timeout: float = 5.0) -> dict:
        return await asyncio.wait_for(self.queue.get(), timeout=timeout)

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
            self.task = None


@pytest.fixture
def prompt_files(tmp_path: Path) -> dict[str, Path]:
    config = tmp_path / "config"
    config.mkdir()
    world = config / "world.md"
    mission = config / "mission.md"
    genesis = config / "genesis.md"
    world.write_text("world rules", encoding="utf-8")
    mission.write_text("the mission", encoding="utf-8")
    genesis.write_text(
        "You are $agent_name. Peer $peer_name. Mission: $mission. Run $run_id at $genesis_time.",
        encoding="utf-8",
    )
    return {"world": world, "mission": mission, "genesis": genesis}
