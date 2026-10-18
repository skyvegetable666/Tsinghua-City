"""The agent runtime inside a container.

Flow:
  1. start a localhost HTTP server for the Claude Code hook and tool bridge;
  2. register with the host supervisor and stream events from it;
  3. wait for the genesis event, then spawn exactly one Claude Code process;
  4. deliver mid-turn messages through the PostToolBatch hook;
  5. deliver messages at turn boundaries by writing one user message, but only
     when no turn is active;
  6. shut down cleanly when the supervisor sends the shutdown event.

The key invariant: Claude's stdin is never written during an active turn. The
delivery policy in this file is the thing the experiment is about.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web

from .broker_client import BrokerClient
from .formatting import format_delivery
from .harness import ClaudeHarness
from .inbox import Inbox, Message

log = logging.getLogger("runtime.bridge")


@dataclass
class BridgeConfig:
    agent_id: str
    agent_name: str
    peer_id: str
    peer_name: str
    run_id: str
    broker_url: str
    broker_token: str
    model: str
    workdir: Path
    local_port: int = 0
    harness: str = "claude"
    hook_batch_max_chars: int = 8000
    max_message_chars: int = 4000

    @classmethod
    def from_env(cls) -> BridgeConfig:
        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise SystemExit(f"missing required env var {name}")
            return value

        workdir = Path(os.environ.get("AGENT_WORKDIR", "/work"))
        if not workdir.exists():
            try:
                workdir.mkdir(parents=True, exist_ok=True)
            except OSError:
                workdir = Path.cwd()
        return cls(
            agent_id=required("AGENT_ID"),
            agent_name=required("AGENT_NAME"),
            peer_id=required("PEER_ID"),
            peer_name=required("PEER_NAME"),
            run_id=required("RUN_ID"),
            broker_url=required("BROKER_URL"),
            broker_token=os.environ.get("AGENT_TOKEN")
            or required("BROKER_TOKEN"),
            model=os.environ.get("AGENT_MODEL")
            or os.environ.get("ANTHROPIC_MODEL")
            or "deepseek-flash[1m]",
            workdir=workdir,
            local_port=int(os.environ.get("AGENT_LOCAL_PORT", "0") or 0),
            harness=os.environ.get("AGENT_HARNESS", "claude").strip() or "claude",
            hook_batch_max_chars=int(os.environ.get("HOOK_BATCH_MAX_CHARS", "8000")),
            max_message_chars=int(os.environ.get("MAX_MESSAGE_CHARS", "4000")),
        )


class Runtime:
    def __init__(
        self,
        config: BridgeConfig,
        *,
        broker: BrokerClient | None = None,
        harness: ClaudeHarness | None = None,
        enable_http: bool = True,
    ) -> None:
        self.config = config
        self.broker = broker or BrokerClient(
            config.broker_url, config.broker_token, config.agent_id, config.run_id
        )
        self.broker.on_event = self.handle_event
        self.harness = harness
        self.enable_http = enable_http

        self.inbox = Inbox()
        self.state = "boot"
        self.genesis: dict | None = None
        self.genesis_event = asyncio.Event()
        self.shutdown: dict | None = None
        self.shutdown_event = asyncio.Event()
        self.harness_exited = asyncio.Event()
        self.stop_requested = asyncio.Event()
        self.exit_event = asyncio.Event()
        self.ending = False
        self.world_prompt = ""
        self.registered = False
        self.local_token = secrets.token_urlsafe(24)
        self.local_url = ""
        self.started_at = time.time()
        self._http_runner: web.AppRunner | None = None
        self._wake_lock = asyncio.Lock()
        self._delivery_lock = asyncio.Lock()
        self._ack_tasks: set[asyncio.Task] = set()
        self._offline_posted = False
        self._last_error: str | None = None

    # ----------------------------------------------------------- broker events
    async def handle_event(self, kind: str, data: dict) -> None:
        if kind == "genesis":
            if self.genesis is None:
                self.genesis = data
                self.genesis_event.set()
                log.info("genesis received (%s chars)", len(data.get("prompt") or ""))
            return
        if kind == "message":
            self._receive_message(data)
            return
        if kind == "shutdown":
            if not self.shutdown_event.is_set():
                self.shutdown = data
                self.shutdown_event.set()
                log.info("shutdown received: %s", data.get("reason"))
            return
        log.debug("unknown stream event %s", kind)

    def _receive_message(self, data: dict) -> None:
        try:
            message = Message(
                id=str(data["id"]),
                seq=int(data.get("seq") or 0),
                sender=str(data.get("sender") or "?"),
                text=str(data.get("text") or ""),
                sent_at=float(data.get("sent_at") or time.time()),
                kind=str(data.get("kind") or "chat"),
                received_at=time.time(),
            )
        except (KeyError, TypeError, ValueError):
            log.warning("ignored malformed message: %r", data)
            return
        if not self.inbox.add(message):
            log.info("duplicate message %s ignored", message.id)
            return
        log.info(
            "inbox += %s from %s (%s chars, %s pending)",
            message.id,
            message.sender,
            len(message.text),
            len(self.inbox),
        )
        if self.state == "idle" and not self.exit_event.is_set():
            asyncio.create_task(self.wake_if_pending(reason="message arrived while idle"))

    # -------------------------------------------------------------- hook entry
    async def handle_hook(self, _payload: dict | None = None) -> dict:
        """Called by the PostToolBatch hook. Returns Claude Code hook JSON."""
        if self.state != "working" or self.harness is None:
            return {}
        if not self.inbox:
            return {}
        async with self._delivery_lock:
            if self.state != "working" or not self.inbox:
                return {}
            messages = self.inbox.take(self.config.hook_batch_max_chars)
            if not messages:
                return {}
            turn = self.harness.current_turn
            text = format_delivery(
                messages,
                channel="hook",
                peer_name=self.config.peer_name,
                max_chars=min(self.config.hook_batch_max_chars, 9000),
            )
            self._schedule_ack(messages, "hook", turn)
        ids = [message.id for message in messages]
        log.info("delivered %s via PostToolBatch hook (turn %s)", ids, turn)
        self.broker.trace(
            {"type": "hook_delivery", "turn": turn, "message_ids": ids, "at": time.time()}
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolBatch",
                "additionalContext": text,
            }
        }

    # --------------------------------------------------------------- tool entry
    async def agent_chat(self, recipient: str, message: str) -> dict:
        if self.ending:
            return {"ok": False, "error": "the experiment is ending; no more messages can be sent"}
        if recipient not in (self.config.peer_name, self.config.peer_id):
            return {
                "ok": False,
                "error": f"unknown recipient {recipient!r}; your peer is {self.config.peer_name!r}",
            }
        text = (message or "").strip()
        if not text:
            return {"ok": False, "error": "message is empty"}
        if len(text) > self.config.max_message_chars:
            return {
                "ok": False,
                "error": f"message too long ({len(text)} chars); limit is {self.config.max_message_chars}",
            }
        try:
            response = await self.broker.send(self.config.peer_name, text)
        except Exception as exc:
            log.warning("send failed: %s", exc)
            return {"ok": False, "error": f"supervisor unavailable: {exc}"}
        return {
            "ok": True,
            "message_id": response.get("message_id"),
            "peer": self.config.peer_name,
            "peer_state": response.get("peer_state"),
            "queued_at": response.get("queued_at"),
            "info": "queued by the supervisor; it will reach your peer at their next safe boundary",
        }

    # ------------------------------------------------------------ delivery path
    async def wake_if_pending(self, *, reason: str = "idle") -> None:
        """If idle and messages are waiting, start a new turn with them."""
        if self.harness is None:
            return
        async with self._wake_lock:
            if self.state != "idle" or self.harness.working:
                return
            if not self.inbox:
                if self.ending:
                    self.exit_event.set()
                return
            messages = self.inbox.take(self.config.hook_batch_max_chars)
            if not messages:
                return
            text = format_delivery(
                messages,
                channel="turn",
                peer_name=self.config.peer_name,
                max_chars=min(self.config.hook_batch_max_chars, 9000),
            )
            self.state = "working"
            try:
                await self.harness.send_user(text)
            except Exception:
                self.state = "idle"
                self.inbox.restore(messages)
                raise
            turn = self.harness.current_turn
            await self.broker.post_state("working", turn, reason)
            self._schedule_ack(messages, "turn", turn)
            log.info(
                "delivered %s at turn boundary (new turn %s, %s pending)",
                [message.id for message in messages],
                turn,
                len(self.inbox),
            )
            self.broker.trace(
                {
                    "type": "turn_delivery",
                    "turn": turn,
                    "message_ids": [message.id for message in messages],
                    "at": time.time(),
                }
            )

    async def _on_turn_end(self, result: dict) -> None:
        self.state = "idle"
        turn = int(result.get("turn") or (self.harness.current_turn if self.harness else 0))
        await self.broker.post_state("idle", turn, "turn ended")
        try:
            await self.wake_if_pending(reason="turn ended")
        except Exception:
            log.exception("wake after turn failed")
            self._last_error = "wake after turn failed"
        if self.ending and self.state == "idle" and not self.inbox:
            self.exit_event.set()

    def _schedule_ack(self, messages: list[Message], channel: str, turn: int) -> None:
        ids = [message.id for message in messages]
        received = {message.id: message.received_at for message in messages if message.received_at}
        task = asyncio.create_task(self.broker.ack(ids, channel, turn, received))
        self._ack_tasks.add(task)
        task.add_done_callback(self._ack_tasks.discard)

    # ------------------------------------------------------------------- http
    async def start_local_http(self) -> None:
        app = web.Application(client_max_size=1 * 1024 * 1024)
        app.router.add_post("/hook/post-tool-batch", self._h_hook)
        app.router.add_post("/internal/agent_chat", self._h_agent_chat)
        app.router.add_get("/health", self._h_health)
        self._http_runner = web.AppRunner(app, access_log=None)
        await self._http_runner.setup()
        site = web.TCPSite(self._http_runner, "127.0.0.1", self.config.local_port)
        await site.start()
        port = self.config.local_port
        server = getattr(site, "_server", None)
        if port == 0 and server is not None and server.sockets:
            port = server.sockets[0].getsockname()[1]
        self.local_url = f"http://127.0.0.1:{port}"
        log.info("local hook/tool server on %s", self.local_url)

    def _check_local_auth(self, request: web.Request) -> None:
        provided = request.headers.get("Authorization", "")
        if not secrets.compare_digest(provided, "Bearer " + self.local_token):
            raise web.HTTPUnauthorized(text="unauthorized")

    async def _h_hook(self, request: web.Request) -> web.Response:
        self._check_local_auth(request)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            payload = {}
        result = await self.handle_hook(payload)
        return web.json_response(result)

    async def _h_agent_chat(self, request: web.Request) -> web.Response:
        self._check_local_auth(request)
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"ok": False, "error": "invalid json"})
        result = await self.agent_chat(
            str(body.get("recipient") or ""), str(body.get("message") or "")
        )
        return web.json_response(result)

    async def _h_health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "agent": self.config.agent_name,
                "state": self.state,
                "inbox": len(self.inbox),
                "turn": self.harness.current_turn if self.harness else 0,
                "ending": self.ending,
            }
        )

    # -------------------------------------------------------------- lifecycle
    async def run(self) -> None:
        await self.start_local_http()
        await self.broker.start()
        await self._register_with_retry()
        stream_task = asyncio.create_task(self.broker.stream_forever())
        try:
            log.info("waiting for genesis")
            await self._wait_for(self.genesis_event, timeout=900)
            if self.stop_requested.is_set():
                return
            await self._launch_harness()
            await self._wait_for_termination(stream_task)
        finally:
            stream_task.cancel()
            await self._cleanup()

    async def _wait_for(self, event: asyncio.Event, *, timeout: float) -> None:
        event_task = asyncio.create_task(event.wait())
        stop_task = asyncio.create_task(self.stop_requested.wait())
        done, pending = await asyncio.wait(
            {event_task, stop_task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        if event_task not in done and not self.stop_requested.is_set():
            raise RuntimeError("timed out waiting for the supervisor")

    async def _register_with_retry(self) -> None:
        delay = 0.5
        last_error: Exception | None = None
        for _ in range(120):
            if self.stop_requested.is_set():
                return
            try:
                response = await self.broker.register(
                    agent_name=self.config.agent_name,
                    model=self.config.model,
                    container=os.environ.get("HOSTNAME", ""),
                )
                break
            except Exception as exc:
                last_error = exc
                log.warning("registration failed (%s); retrying in %.1fs", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)
        else:
            raise RuntimeError(f"could not register with supervisor: {last_error}")
        self.world_prompt = str(response.get("world_prompt") or "")
        peer = response.get("peer") or {}
        if peer.get("name"):
            self.config.peer_name = str(peer["name"])
            self.config.peer_id = str(peer.get("agent_id") or self.config.peer_id)
        config = response.get("config") or {}
        if config.get("hook_batch_max_chars"):
            self.config.hook_batch_max_chars = int(config["hook_batch_max_chars"])
        if config.get("max_message_chars"):
            self.config.max_message_chars = int(config["max_message_chars"])
        self.registered = True
        self.state = "starting"
        log.info(
            "registered with supervisor as %s (peer %s, run %s)",
            self.config.agent_name,
            self.config.peer_name,
            self.config.run_id,
        )

    async def _launch_harness(self) -> None:
        if self.genesis is None:
            raise RuntimeError("cannot launch without genesis")
        if self.harness is None:
            self.harness = ClaudeHarness(
                run_id=self.config.run_id,
                agent_id=self.config.agent_id,
                agent_name=self.config.agent_name,
                model=self.config.model,
                world_prompt=self.world_prompt,
                workdir=self.config.workdir,
                local_url=self.local_url,
                local_token=self.local_token,
                trace=self.broker.trace,
                on_turn_end=self._on_turn_end,
                on_exit=self._on_harness_exit,
            )
        await self.harness.start()
        await self.broker.post_state("starting", 0, "claude starting")
        prompt = str(self.genesis.get("prompt") or "")
        self.state = "working"
        # The stream-json CLI only emits its init frame after its first input,
        # so write genesis first and confirm readiness afterwards.
        await self.harness.send_initial(prompt)
        await self.harness.wait_ready()
        # The genesis turn can finish before init is observed on a very fast
        # model; report the state that actually holds now.
        state = self.state if self.state in ("working", "idle") else "working"
        await self.broker.post_state(state, self.harness.current_turn, "genesis delivered")
        self.broker.trace({"type": "genesis_delivered", "at": time.time()})
        log.info("genesis written to claude (%s chars)", len(prompt))

    async def _on_harness_exit(self, code: int) -> None:
        self._last_error = f"claude exited with code {code}"
        log.warning(self._last_error)
        self.broker.trace(
            {
                "type": "error",
                "error": self._last_error,
                "stderr_tail": self.harness.stderr_tail() if self.harness else "",
                "at": time.time(),
            }
        )
        self.harness_exited.set()
        self.shutdown_event.set()

    async def _wait_for_termination(self, stream_task: asyncio.Task) -> None:
        assert self.harness is not None
        shutdown_task = asyncio.create_task(self.shutdown_event.wait())
        exited_task = asyncio.create_task(self.harness_exited.wait())
        stop_task = asyncio.create_task(self.stop_requested.wait())
        _done, pending = await asyncio.wait(
            {shutdown_task, exited_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        if self.stop_requested.is_set():
            log.info("stop requested by signal")
            await self._ending_sequence(graceful=False)
            return
        if self.harness_exited.is_set():
            log.warning("harness exited on its own: %s", self._last_error)
            self.state = "stopped"
            return
        await self._ending_sequence(graceful=True)

    async def _ending_sequence(self, *, graceful: bool) -> None:
        self.ending = True
        assert self.harness is not None
        deadline = 90.0
        if self.shutdown:
            try:
                deadline = float(self.shutdown.get("hard_deadline_seconds") or 90.0)
            except (TypeError, ValueError):
                pass
        if graceful:
            log.info("graceful shutdown: waiting up to %.0fs for a final turn", deadline)
            try:
                if self.state == "idle" and self.inbox:
                    await self.wake_if_pending(reason="shutdown")
                elif self.state == "idle":
                    self.exit_event.set()
                await asyncio.wait_for(self.exit_event.wait(), timeout=deadline)
            except TimeoutError:
                log.warning("agents did not finish within the shutdown window")
        try:
            await self.harness.stop()
        except Exception:
            log.exception("error stopping harness")
        self.state = "stopped"

    async def _cleanup(self) -> None:
        if self.harness is not None and not self.harness.exited.is_set():
            try:
                await self.harness.stop(grace=5)
            except Exception:
                log.exception("error stopping harness")
        for task in list(self._ack_tasks):
            task.cancel()
        if self.registered and not self._offline_posted:
            self._offline_posted = True
            try:
                await self.broker.post_state("offline", self.harness.current_turn if self.harness else 0, "runtime exiting")
            except Exception:
                pass
        try:
            await self.broker.close()
        except Exception:
            log.exception("error closing broker client")
        if self._http_runner is not None:
            try:
                await self._http_runner.cleanup()
            except Exception:
                pass

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()

        def request_stop() -> None:
            log.info("signal received; stopping")
            self.stop_requested.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, request_stop)
            except (NotImplementedError, RuntimeError):
                try:
                    signal.signal(sig, lambda *_args: loop.call_soon_threadsafe(request_stop))
                except (ValueError, OSError):
                    pass


async def _amain() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        stream=sys.stdout,
    )
    config = BridgeConfig.from_env()
    log.info(
        "agent runtime starting: %s (%s) peer=%s run=%s harness=%s",
        config.agent_name,
        config.agent_id,
        config.peer_name,
        config.run_id,
        config.harness,
    )
    if config.harness != "claude":
        raise SystemExit(f"unsupported AGENT_HARNESS={config.harness!r}")
    runtime = Runtime(config)
    runtime.install_signal_handlers()
    try:
        await runtime.run()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("agent runtime failed")
        return 1
    return 0


def main() -> None:
    try:
        code = asyncio.run(_amain())
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)


if __name__ == "__main__":
    main()
