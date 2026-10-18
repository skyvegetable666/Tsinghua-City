"""HTTP + SSE client for the supervisor inside the agent container."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import aiohttp

log = logging.getLogger("runtime.broker")


class BrokerError(RuntimeError):
    pass


class BrokerClient:
    """Talks to the host broker. Owns the SSE connection and trace batching."""

    def __init__(self, base_url: str, token: str, agent_id: str, run_id: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.agent_id = agent_id
        self.run_id = run_id
        self._session: aiohttp.ClientSession | None = None
        self._headers = {"Authorization": f"Bearer {token}"}
        self._trace_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._trace_task: asyncio.Task | None = None
        self._closed = False
        self.last_seq = 0
        self.on_event: Callable[[str, dict], Awaitable[None]] | None = None
        self.connected = False

    # -------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=45)
        self._session = aiohttp.ClientSession(
            timeout=timeout, read_bufsize=4 * 1024 * 1024
        )
        self._trace_task = asyncio.create_task(self._trace_loop())

    async def close(self) -> None:
        self._closed = True
        if self._trace_task:
            self._trace_task.cancel()
            try:
                await self._trace_task
            except (asyncio.CancelledError, Exception):
                pass
        await self._flush_traces()
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ---------------------------------------------------------------- requests
    async def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        retries: int = 4,
        timeout: float = 10.0,
    ) -> dict:
        if self._session is None:
            raise BrokerError("client not started")
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                async with self._session.request(
                    method,
                    self.base_url + path,
                    json=body,
                    headers=self._headers,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as response:
                    try:
                        data = await response.json(content_type=None)
                    except (aiohttp.ContentTypeError, json.JSONDecodeError):
                        text = await response.text()
                        data = {"ok": False, "error": text[:500]}
                    if 400 <= response.status < 500:
                        raise BrokerError(f"{response.status}: {data.get('error', data)}")
                    if response.status >= 500:
                        raise BrokerError(f"{response.status}: {data}")
                    return data
            except BrokerError:
                raise
            except Exception as exc:  # network problems: retry
                last_error = exc
                if attempt + 1 < retries:
                    await asyncio.sleep(min(0.5 * (2**attempt), 5.0))
        raise BrokerError(f"{method} {path} failed: {last_error!r}")

    async def register(self, *, agent_name: str, model: str, container: str) -> dict:
        return await self._request(
            "POST",
            f"/v1/agents/{self.agent_id}/register",
            {
                "run_id": self.run_id,
                "agent_name": agent_name,
                "model": model,
                "container": container,
            },
            retries=1,
        )

    async def send(self, recipient: str, text: str) -> dict:
        return await self._request(
            "POST",
            f"/v1/agents/{self.agent_id}/messages",
            {"recipient": recipient, "text": text},
        )

    async def post_state(self, state: str, turn: int, detail: str = "") -> None:
        try:
            await self._request(
                "POST",
                f"/v1/agents/{self.agent_id}/state",
                {"state": state, "turn": turn, "detail": detail},
                timeout=5,
            )
        except Exception as exc:
            log.warning("could not post state %s: %s", state, exc)

    async def ack(
        self,
        message_ids: list[str],
        channel: str,
        turn: int,
        received_at: dict[str, float],
    ) -> None:
        if not message_ids:
            return
        try:
            await self._request(
                "POST",
                f"/v1/agents/{self.agent_id}/delivered",
                {
                    "message_ids": message_ids,
                    "channel": channel,
                    "turn": turn,
                    "received_at": received_at,
                },
                timeout=5,
            )
        except Exception as exc:
            log.warning("delivery ack failed for %s: %s", message_ids, exc)

    # ------------------------------------------------------------------ trace
    def trace(self, event: dict) -> None:
        if self._closed:
            return
        try:
            self._trace_queue.put_nowait(event)
        except asyncio.QueueFull:  # pragma: no cover
            pass

    async def _trace_loop(self) -> None:
        while not self._closed:
            try:
                await asyncio.sleep(0.5)
                await self._flush_traces()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("trace loop error: %s", exc)

    async def _flush_traces(self) -> None:
        batch: list[dict] = []
        while not self._trace_queue.empty() and len(batch) < 100:
            batch.append(self._trace_queue.get_nowait())
        if not batch or self._session is None:
            return
        try:
            await self._request(
                "POST",
                f"/v1/agents/{self.agent_id}/trace",
                {"events": batch},
                retries=2,
                timeout=5,
            )
        except Exception as exc:
            log.debug("could not flush %d trace events: %s", len(batch), exc)

    # --------------------------------------------------------------- stream
    async def stream_forever(self) -> None:
        backoff = 0.5
        while not self._closed:
            try:
                await self._stream_once()
                self.connected = False
                backoff = 0.5
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                if self._closed:
                    break
                log.warning("event stream lost (%s); reconnecting in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

    async def _stream_once(self) -> None:
        assert self._session is not None
        url = f"{self.base_url}/v1/agents/{self.agent_id}/stream"
        timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=45)
        params = {"since": str(self.last_seq)}
        async with self._session.get(
            url, params=params, headers=self._headers, timeout=timeout
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise BrokerError(f"stream status {response.status}: {text[:200]}")
            self.connected = True
            log.info("event stream connected (since=%s)", self.last_seq)
            event_type = "message"
            event_id: str | None = None
            data_lines: list[str] = []
            async for raw in response.content:
                line = raw.decode("utf-8").rstrip("\r\n")
                if line == "":
                    if data_lines:
                        try:
                            payload = json.loads("\n".join(data_lines))
                        except json.JSONDecodeError:
                            payload = {"raw": "\n".join(data_lines)}
                        if event_id:
                            try:
                                self.last_seq = max(self.last_seq, int(event_id))
                            except ValueError:
                                pass
                        if self.on_event is not None:
                            try:
                                await self.on_event(event_type, payload)
                            except Exception:
                                log.exception("event handler failed for %s", event_type)
                    event_type = "message"
                    event_id = None
                    data_lines = []
                elif line.startswith(":"):
                    continue
                elif line.startswith("event:"):
                    event_type = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:") :].lstrip())
                elif line.startswith("id:"):
                    event_id = line[len("id:") :].strip()
