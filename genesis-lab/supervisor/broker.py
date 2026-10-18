"""The supervisor: a small HTTP broker that owns the world's message bus.

Agents in the two containers can only reach the outside world through this
service. It performs four jobs:

1. Barrier / genesis: wait until both agents are registered and connected, then
   hand each of them its genesis prompt at the same instant.
2. Routing: every chat message goes through here. It is assigned an id and a
   per-recipient sequence number, appended to the run log, and pushed to the
   recipient's server-sent-events stream.
3. Delivery accounting: agents acknowledge messages when they are actually
   injected into the model context, with the channel (hook vs turn) and the
   queueing latency.
4. Control: system notices, presence, budgets, and the end of the experiment.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
import uuid
from dataclasses import dataclass, field

from aiohttp import web

from .prompts import PromptBundle, render_genesis
from .settings import AgentSpec, RunContext, Settings, agent_token
from .store import RunStore, utc_iso

PROTOCOL_VERSION = 1
OFFLINE_GRACE_SECONDS = 15.0
MAX_TRACE_EVENTS = 200
AUTH_AGENT: web.RequestKey = web.RequestKey("auth_agent", str | None)


@dataclass
class Message:
    id: str
    seq: int
    sender_id: str | None
    sender: str
    recipient_id: str
    recipient: str
    kind: str  # "chat" | "system"
    text: str
    sent_at: float
    delivered_at: float | None = None
    delivered_channel: str | None = None
    delivered_turn: int | None = None
    received_at: float | None = None

    def to_wire(self) -> dict:
        return {
            "id": self.id,
            "seq": self.seq,
            "sender_id": self.sender_id,
            "sender": self.sender,
            "recipient_id": self.recipient_id,
            "recipient": self.recipient,
            "kind": self.kind,
            "text": self.text,
            "sent_at": self.sent_at,
            "sent_at_iso": utc_iso(self.sent_at),
        }


@dataclass
class ChannelEvent:
    seq: int
    type: str
    data: dict

    def encode(self) -> bytes:
        payload = json.dumps(self.data, ensure_ascii=False)
        return f"id: {self.seq}\nevent: {self.type}\ndata: {payload}\n\n".encode()


@dataclass
class AgentChannel:
    agent_id: str
    history: list[ChannelEvent] = field(default_factory=list)
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    connected: bool = False
    connection_id: str = ""

    def push(self, kind: str, data: dict) -> ChannelEvent:
        event = ChannelEvent(seq=len(self.history) + 1, type=kind, data=data)
        self.history.append(event)
        self.wakeup.set()
        return event


@dataclass
class AgentRuntime:
    spec: AgentSpec
    registered: bool = False
    registered_at: float | None = None
    state: str = "starting"
    turn: int = 0
    model: str = ""
    usage: dict[str, int] = field(
        default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
    )
    cost_usd: float = 0.0
    offline_since: float | None = None


class Broker:
    def __init__(
        self,
        settings: Settings,
        run: RunContext,
        prompts: PromptBundle,
        store: RunStore,
    ) -> None:
        self.settings = settings
        self.run = run
        self.prompts = prompts
        self.store = store

        self.agents: dict[str, AgentRuntime] = {
            spec.agent_id: AgentRuntime(spec=spec) for spec in settings.agents.values()
        }
        self.channels: dict[str, AgentChannel] = {
            agent_id: AgentChannel(agent_id) for agent_id in self.agents
        }
        self.messages: dict[str, Message] = {}

        self.phase = "waiting"  # waiting -> running -> ending -> ended
        self.genesis_at: float | None = None
        self.end_reason: str | None = None
        self.end_requested_at: float | None = None
        self.shutdown_sent_at: float | None = None
        self.started_at = run.started_at

        self.log_queue: asyncio.Queue[str] = asyncio.Queue()
        self.ended_event = asyncio.Event()
        self.agent_tokens = {
            agent_id: agent_token(run.token, agent_id) for agent_id in self.agents
        }
        self._stop_event = asyncio.Event()
        self._next_message = 1
        self._chat_count = 0
        self._offline_tasks: dict[str, asyncio.Task] = {}
        self._ending_task: asyncio.Task | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    # ------------------------------------------------------------------ http
    def make_app(self) -> web.Application:
        app = web.Application(middlewares=[self._auth], client_max_size=4 * 1024 * 1024)
        app.router.add_get("/health", self.h_health)
        app.router.add_post("/v1/agents/{agent_id}/register", self.h_register)
        app.router.add_get("/v1/agents/{agent_id}/stream", self.h_stream)
        app.router.add_post("/v1/agents/{agent_id}/state", self.h_state)
        app.router.add_post("/v1/agents/{agent_id}/delivered", self.h_delivered)
        app.router.add_post("/v1/agents/{agent_id}/trace", self.h_trace)
        app.router.add_post("/v1/agents/{agent_id}/messages", self.h_send_message)
        app.router.add_post("/v1/system/messages", self.h_system_message)
        app.router.add_post("/v1/end", self.h_end)
        app.router.add_get("/v1/status", self.h_status)
        return app

    @web.middleware
    async def _auth(self, request: web.Request, handler):
        if request.path == "/health":
            return await handler(request)
        provided = request.headers.get("Authorization", "")

        def matches(token: str) -> bool:
            return secrets.compare_digest(provided, "Bearer " + token)

        # The host (CLI, orchestrator) holds the run token and may act for any
        # agent. A container holds only its own derived token.
        auth_agent: str | None = None
        if matches(self.run.token):
            auth_agent = None
        else:
            for agent_id, token in self.agent_tokens.items():
                if matches(token):
                    auth_agent = agent_id
                    break
            if auth_agent is None:
                return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        path_agent = request.match_info.get("agent_id")
        if path_agent is not None and auth_agent is not None and path_agent != auth_agent:
            return web.json_response({"ok": False, "error": "forbidden"}, status=403)
        request[AUTH_AGENT] = auth_agent
        return await handler(request)

    async def start(self) -> None:
        app = self.make_app()
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.settings.broker_host, self.settings.broker_port)
        await self._site.start()
        self.store.append(
            "broker_started",
            host=self.settings.broker_host,
            port=self.settings.broker_port,
            protocol=PROTOCOL_VERSION,
        )
        self._log(f"broker listening on {self.settings.broker_host}:{self.settings.broker_port}")

    async def stop(self) -> None:
        self._stop_event.set()
        for channel in self.channels.values():
            channel.wakeup.set()
        if self._ending_task and not self._ending_task.done():
            self._ending_task.cancel()
        if self._runner is not None:
            try:
                await asyncio.wait_for(self._runner.cleanup(), timeout=10)
            except (TimeoutError, Exception):
                pass
        self.store.close()

    # ------------------------------------------------------------- handlers
    async def h_health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "run_id": self.run.run_id,
                "phase": self.phase,
                "agents": {a.agent_id: self.agents[a.agent_id].state for a in self.settings.agents.values()},
            }
        )

    async def h_register(self, request: web.Request) -> web.Response:
        agent = self._agent(request)
        if agent is None:
            return self._error("unknown agent", 404)
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return self._error("invalid json")
        if body.get("run_id") != self.run.run_id:
            return self._error("run_id mismatch", 409)
        if body.get("agent_name") not in (None, agent.spec.name):
            return self._error("agent_name mismatch")

        first = not agent.registered
        agent.registered = True
        agent.registered_at = time.time()
        agent.model = str(body.get("model") or "")
        self.store.append(
            "agent_registered",
            agent_id=agent.spec.agent_id,
            name=agent.spec.name,
            model=agent.model,
            container=str(body.get("container") or ""),
            first_registration=first,
        )
        if first:
            self._log(f"{agent.spec.name} registered (model={agent.model or 'unknown'})")
        await self._maybe_genesis()

        return web.json_response(
            {
                "ok": True,
                "protocol": PROTOCOL_VERSION,
                "run_id": self.run.run_id,
                "self": {"agent_id": agent.spec.agent_id, "name": agent.spec.name},
                "peer": {"agent_id": agent.spec.peer_id, "name": agent.spec.peer_name},
                "world_prompt": self.prompts.world,
                "config": {
                    "max_message_chars": self.settings.max_message_chars,
                    "hook_batch_max_chars": self.settings.hook_batch_max_chars,
                },
            }
        )

    async def h_stream(self, request: web.Request) -> web.StreamResponse:
        agent = self._agent(request)
        if agent is None:
            return self._error("unknown agent", 404)
        if not agent.registered:
            return self._error("register first", 409)

        since_raw = request.query.get("since", "0")
        try:
            since = max(0, int(since_raw))
        except ValueError:
            since = 0

        channel = self.channels[agent.spec.agent_id]
        conn_id = uuid.uuid4().hex
        was_offline = agent.offline_since is not None
        channel.connected = True
        channel.connection_id = conn_id
        self._cancel_offline(agent.spec.agent_id)
        if was_offline:
            await self._mark_online(agent.spec.agent_id)
        self._log(f"{agent.spec.name} stream connected (since={since})")
        await self._maybe_genesis()

        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        cursor = min(since, len(channel.history))
        try:
            while True:
                channel.wakeup.clear()
                while cursor < len(channel.history) and not self._stop_event.is_set():
                    event = channel.history[cursor]
                    cursor += 1
                    await response.write(event.encode())
                if self._stop_event.is_set():
                    break
                try:
                    await asyncio.wait_for(channel.wakeup.wait(), timeout=15.0)
                except TimeoutError:
                    await response.write(b": ping\n\n")
            try:
                await response.write_eof()
            except (ConnectionResetError, RuntimeError):
                pass
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            if channel.connection_id == conn_id:
                channel.connected = False
                if not self._stop_event.is_set() and self.phase in ("waiting", "running"):
                    self._schedule_offline(agent.spec.agent_id)
        return response

    async def h_state(self, request: web.Request) -> web.Response:
        agent = self._agent(request)
        if agent is None:
            return self._error("unknown agent", 404)
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return self._error("invalid json")
        state = str(body.get("state") or "")
        if state not in {"starting", "working", "idle", "offline"}:
            return self._error("invalid state")
        turn = int(body.get("turn") or 0)
        previous = agent.state
        agent.turn = turn
        if state == "offline":
            await self._mark_offline(agent.spec.agent_id, reason=str(body.get("detail") or "reported offline"))
        else:
            agent.state = state
            if previous == "offline" or agent.offline_since is not None:
                await self._mark_online(agent.spec.agent_id)
        if previous != state:
            self.store.append(
                "agent_state",
                agent_id=agent.spec.agent_id,
                name=agent.spec.name,
                state=state,
                turn=turn,
                detail=str(body.get("detail") or ""),
            )
            self._log(f"{agent.spec.name} state={state} turn={turn}")
        return web.json_response({"ok": True, "phase": self.phase})

    async def h_delivered(self, request: web.Request) -> web.Response:
        agent = self._agent(request)
        if agent is None:
            return self._error("unknown agent", 404)
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return self._error("invalid json")
        ids = body.get("message_ids") or []
        if not isinstance(ids, list):
            return self._error("message_ids must be a list")
        channel = str(body.get("channel") or "unknown")
        turn = int(body.get("turn") or 0)
        received_map = body.get("received_at") or {}
        now = time.time()
        accepted = 0
        for message_id in ids:
            message = self.messages.get(str(message_id))
            if message is None or message.recipient_id != agent.spec.agent_id:
                continue
            if message.delivered_at is not None:
                continue
            message.delivered_at = now
            message.delivered_channel = channel
            message.delivered_turn = turn
            message.received_at = float(received_map.get(message_id) or 0) or None
            accepted += 1
            latency = now - message.sent_at
            queue_time = (message.received_at - message.sent_at) if message.received_at else None
            self.store.append(
                "message_delivered",
                message_id=message.id,
                agent_id=agent.spec.agent_id,
                channel=channel,
                turn=turn,
                latency_s=round(latency, 4),
                network_s=round(queue_time, 4) if queue_time is not None else None,
            )
            self._log(
                f"delivered {message.id} -> {agent.spec.name} via {channel} "
                f"(turn {turn}, +{latency:.2f}s)"
            )
        return web.json_response({"ok": True, "accepted": accepted})

    async def h_trace(self, request: web.Request) -> web.Response:
        agent = self._agent(request)
        if agent is None:
            return self._error("unknown agent", 404)
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return self._error("invalid json")
        events = body.get("events") or []
        if not isinstance(events, list):
            return self._error("events must be a list")
        for event in events[:MAX_TRACE_EVENTS]:
            if not isinstance(event, dict):
                continue
            self.store.append("agent_trace", agent_id=agent.spec.agent_id, trace=event)
            kind = event.get("type")
            if kind == "assistant_text":
                text = " ".join(str(event.get("text") or "").split())
                if text:
                    self._log(f"[{agent.spec.name}] {text[:240]}")
            elif kind == "tool_use":
                self._log(f"[{agent.spec.name}] tool {event.get('name')}")
            elif kind == "turn_result":
                usage = event.get("usage") or {}
                for key in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
                    try:
                        agent.usage[key] = agent.usage.get(key, 0) + int(usage.get(key) or 0)
                    except (TypeError, ValueError):
                        pass
                try:
                    agent.cost_usd += float(event.get("cost_usd") or 0)
                except (TypeError, ValueError):
                    pass
                agent.turn = int(event.get("turn") or agent.turn)
            elif kind == "error":
                self._log(f"{agent.spec.name} error: {str(event.get('error'))[:200]}")
        return web.json_response({"ok": True})

    async def h_send_message(self, request: web.Request) -> web.Response:
        agent = self._agent(request)
        if agent is None:
            return self._error("unknown agent", 404)
        # During the ending grace period agents may still exchange final
        # messages; after shutdown the bridge stops calling this endpoint.
        if self.phase not in ("running", "ending"):
            return self._error(f"world is not running (phase={self.phase})", 409)
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return self._error("invalid json")

        recipient_raw = str(body.get("recipient") or agent.spec.peer_name).strip()
        peer = self.agents[agent.spec.peer_id]
        if recipient_raw not in (peer.spec.name, peer.spec.agent_id):
            return self._error(f"recipient must be {peer.spec.name!r}")
        text = str(body.get("text") or "").strip()
        if not text:
            return self._error("message text is empty")
        if len(text) > self.settings.max_message_chars:
            return self._error(
                f"message too long ({len(text)} chars); limit is {self.settings.max_message_chars}"
            )

        message = self._enqueue(
            recipient_id=peer.spec.agent_id,
            sender_id=agent.spec.agent_id,
            sender_name=agent.spec.name,
            kind="chat",
            text=text,
        )
        self._chat_count += 1
        if self.settings.run_max_messages and self._chat_count >= self.settings.run_max_messages:
            asyncio.create_task(self.request_end("message budget exhausted"))

        return web.json_response(
            {
                "ok": True,
                "message_id": message.id,
                "seq": message.seq,
                "queued_at": utc_iso(message.sent_at),
                "peer_state": peer.state,
                "max_message_chars": self.settings.max_message_chars,
            }
        )

    async def h_system_message(self, request: web.Request) -> web.Response:
        if request.get(AUTH_AGENT) is not None:
            return self._error("forbidden", 403)
        if self.phase != "running":
            return self._error(f"world is not running (phase={self.phase})", 409)
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return self._error("invalid json")
        text = str(body.get("text") or "").strip()
        if not text:
            return self._error("message text is empty")
        if len(text) > self.settings.max_message_chars:
            return self._error(f"message too long; limit is {self.settings.max_message_chars}")
        target = str(body.get("to") or "all").strip()
        if target == "all":
            recipients = list(self.agents)
        elif target in self.agents:
            recipients = [target]
        else:
            spec = self.settings.agent_by_name(target)
            if spec is None:
                return self._error("unknown recipient")
            recipients = [spec.agent_id]
        ids = [
            self._enqueue(
                recipient_id=recipient_id,
                sender_id=None,
                sender_name="supervisor",
                kind="system",
                text=text,
            ).id
            for recipient_id in recipients
        ]
        self.store.append("system_message_injected", ids=ids, to=target, text=text)
        return web.json_response({"ok": True, "message_ids": ids})

    async def h_end(self, request: web.Request) -> web.Response:
        if request.get(AUTH_AGENT) is not None:
            return self._error("forbidden", 403)
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        reason = str(body.get("reason") or "operator requested end")
        started = await self.request_end(reason)
        return web.json_response({"ok": True, "started": started, "phase": self.phase})

    async def h_status(self, request: web.Request) -> web.Response:
        if request.get(AUTH_AGENT) is not None:
            return self._error("forbidden", 403)
        return web.json_response(self.status())

    # -------------------------------------------------------------- control
    async def request_end(self, reason: str) -> bool:
        if self.phase != "running":
            return False
        self.phase = "ending"
        self.end_reason = reason
        self.end_requested_at = time.time()
        self.store.append("run_ending", reason=reason)
        self._log(f"ending the run: {reason}")
        self._enqueue_system_all(
            "The supervisor is ending the experiment. Reason: "
            f"{reason}. Finish your current thought and, if you wish, send a final message to your peer. "
            "The world will be switched off shortly."
        )
        self._ending_task = asyncio.create_task(self._ending_sequence())
        return True

    async def _ending_sequence(self) -> None:
        try:
            await asyncio.sleep(max(0.0, self.settings.end_grace_seconds))
            now = time.time()
            self.shutdown_sent_at = now
            for channel in self.channels.values():
                channel.push(
                    "shutdown",
                    {
                        "reason": self.end_reason or "ended",
                        "at": now,
                        "at_iso": utc_iso(now),
                        "hard_deadline_seconds": 90,
                    },
                )
            self.store.append("shutdown_sent", reason=self.end_reason, at=now)
            self._log("shutdown event sent to both agents")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            self.store.append("error", where="ending_sequence", error=repr(exc))

    async def finalize(self, reason: str = "completed") -> None:
        if self.phase == "ended":
            return
        self.phase = "ended"
        ended_at = time.time()
        self.store.append(
            "run_ended",
            reason=reason,
            ended_at=ended_at,
            status=self.status(),
        )
        try:
            from .report import build_report

            metrics = build_report(self.run.run_dir)
            self.store.append("report_written", metrics=metrics)
        except Exception as exc:  # pragma: no cover - defensive
            self.store.append("error", where="report", error=repr(exc))
        self._log("run finalized")
        self.ended_event.set()

    def status(self) -> dict:
        delivered = sum(1 for m in self.messages.values() if m.delivered_at is not None)
        chat_messages = [m for m in self.messages.values() if m.kind == "chat"]
        return {
            "run_id": self.run.run_id,
            "phase": self.phase,
            "started_at": self.run.started_iso,
            "genesis_at": utc_iso(self.genesis_at) if self.genesis_at else None,
            "end_reason": self.end_reason,
            "agents": {
                agent.spec.agent_id: {
                    "name": agent.spec.name,
                    "state": agent.state,
                    "connected": self.channels[agent.spec.agent_id].connected,
                    "registered": agent.registered,
                    "turn": agent.turn,
                    "model": agent.model,
                    "cost_usd": round(agent.cost_usd, 6),
                    "usage": dict(agent.usage),
                }
                for agent in self.agents.values()
            },
            "messages": {
                "chat": len(chat_messages),
                "system": len(self.messages) - len(chat_messages),
                "delivered": delivered,
                "pending": len(self.messages) - delivered,
            },
        }

    # -------------------------------------------------------------- helpers
    def _agent(self, request: web.Request) -> AgentRuntime | None:
        return self.agents.get(request.match_info.get("agent_id", ""))

    def _error(self, message: str, status: int = 400) -> web.Response:
        return web.json_response({"ok": False, "error": message}, status=status)

    def _log(self, text: str) -> None:
        self.log_queue.put_nowait(time.strftime("%H:%M:%S") + " " + text)

    def _enqueue(
        self,
        *,
        recipient_id: str,
        sender_id: str | None,
        sender_name: str,
        kind: str,
        text: str,
    ) -> Message:
        now = time.time()
        prefix = "s" if kind == "system" else "m"
        message_id = f"{prefix}{self._next_message:06d}"
        self._next_message += 1
        channel = self.channels[recipient_id]
        message = Message(
            id=message_id,
            seq=len(channel.history) + 1,
            sender_id=sender_id,
            sender=sender_name,
            recipient_id=recipient_id,
            recipient=self.agents[recipient_id].spec.name,
            kind=kind,
            text=text,
            sent_at=now,
        )
        self.messages[message.id] = message
        channel.push("message", message.to_wire())
        self.store.append("message_sent", message=message.to_wire())
        peer_state = self.agents[recipient_id].state
        self._log(
            f"{message.sender} -> {message.recipient} {message.id} "
            f"({len(text)} chars, peer={peer_state})"
        )
        return message

    def _enqueue_system_all(self, text: str) -> list[str]:
        return [
            self._enqueue(
                recipient_id=agent_id,
                sender_id=None,
                sender_name="supervisor",
                kind="system",
                text=text,
            ).id
            for agent_id in self.agents
        ]

    async def _maybe_genesis(self) -> None:
        if self.phase != "waiting":
            return
        if not all(agent.registered for agent in self.agents.values()):
            return
        if not all(self.channels[agent_id].connected for agent_id in self.agents):
            return
        self.phase = "running"
        self.genesis_at = time.time()
        genesis_iso = utc_iso(self.genesis_at)
        for agent_id, agent in self.agents.items():
            prompt = render_genesis(
                self.prompts,
                agent=agent.spec,
                run_id=self.run.run_id,
                genesis_time=genesis_iso,
            )
            self.channels[agent_id].push(
                "genesis",
                {
                    "prompt": prompt,
                    "genesis_at": self.genesis_at,
                    "genesis_at_iso": genesis_iso,
                    "run_id": self.run.run_id,
                },
            )
            self.store.append(
                "genesis",
                agent_id=agent_id,
                name=agent.spec.name,
                peer=agent.spec.peer_name,
                prompt=prompt,
                genesis_at=self.genesis_at,
            )
        self._log(f"GENESIS: both agents connected; world is running (run {self.run.run_id})")

    def _schedule_offline(self, agent_id: str) -> None:
        self._cancel_offline(agent_id)

        async def _later() -> None:
            try:
                await asyncio.sleep(OFFLINE_GRACE_SECONDS)
                agent = self.agents[agent_id]
                if not self.channels[agent_id].connected and agent.state != "offline":
                    await self._mark_offline(agent_id, reason="stream disconnected")
            except asyncio.CancelledError:
                pass

        self._offline_tasks[agent_id] = asyncio.create_task(_later())

    def _cancel_offline(self, agent_id: str) -> None:
        task = self._offline_tasks.pop(agent_id, None)
        if task and not task.done():
            task.cancel()

    async def _mark_offline(self, agent_id: str, *, reason: str) -> None:
        agent = self.agents[agent_id]
        if agent.state == "offline":
            return
        agent.state = "offline"
        agent.offline_since = time.time()
        self.store.append("agent_offline", agent_id=agent_id, name=agent.spec.name, reason=reason)
        self._log(f"{agent.spec.name} is offline ({reason})")
        if self.phase == "running":
            peer = self.agents[agent.spec.peer_id]
            text = (
                f"{agent.spec.name} is now offline. Messages sent to them will be queued "
                "by the supervisor."
            )
            if reason and reason != "reported offline":
                text += f" ({reason})"
            self._enqueue(
                recipient_id=peer.spec.agent_id,
                sender_id=None,
                sender_name="supervisor",
                kind="system",
                text=text,
            )

    async def _mark_online(self, agent_id: str) -> None:
        agent = self.agents[agent_id]
        was_offline = agent.offline_since is not None
        agent.offline_since = None
        if agent.state == "offline":
            agent.state = "starting"
        self.store.append("agent_online", agent_id=agent_id, name=agent.spec.name)
        self._log(f"{agent.spec.name} is back online")
        if was_offline and self.phase == "running":
            peer = self.agents[agent.spec.peer_id]
            self._enqueue(
                recipient_id=peer.spec.agent_id,
                sender_id=None,
                sender_name="supervisor",
                kind="system",
                text=f"{agent.spec.name} is back online.",
            )
