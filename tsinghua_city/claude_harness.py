"""Thin Claude Code CLI process harness over stream-json.

This is a port of the archived genesis-lab harness, trimmed to the first
product: one Claude Code process, one or more user turns, and structured trace
events. It does not implement memory, wake, rebirth, or a custom agent loop.

The harness is the only writer to Claude's stdin. It refuses to write while a
turn is active. In the first product the process runs in a Docker sandbox; the
local adapter is only for development.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path

log = logging.getLogger("tsinghua_city.claude_harness")

DISALLOWED_TOOLS = "Task,WebFetch,WebSearch,SendMessage,ListAgents,Workflow,EnterWorktree,ExitWorktree"
WORKSPACE_TOOLS = "Bash,Read,Write,Edit,Glob,Grep,mcp__tsinghua-city__*"


def _session_uuid(run_id: str, agent_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"tsinghua-city:{run_id}:{agent_id}"))


class ClaudeHarness:
    def __init__(
        self,
        *,
        run_id: str,
        agent_id: str,
        agent_name: str,
        model: str,
        world_prompt: str,
        workdir: Path,
        mcp_config: Path,
        trace: Callable[[dict], None],
        on_turn_end: Callable[[dict], Awaitable[None]] | None = None,
        on_exit: Callable[[int], Awaitable[None]] | None = None,
        claude_bin: str = "claude",
        max_turns: int = 30,
        resume_session: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.agent_id = agent_id
        self.agent_name = agent_name
        self.model = model
        self.world_prompt = world_prompt
        self.workdir = Path(workdir)
        self.mcp_config = Path(mcp_config)
        self.trace = trace
        self.on_turn_end = on_turn_end
        self.on_exit = on_exit
        self.claude_bin = claude_bin
        self.max_turns = max_turns
        self.resume_session = resume_session
        self.last_result: dict | None = None

        self.process: asyncio.subprocess.Process | None = None
        self.ready = asyncio.Event()
        self.exited = asyncio.Event()
        self.turn_ended = asyncio.Event()
        self.exit_code: int | None = None
        self.session_id: str | None = None
        self.current_turn = 0
        self.working = False
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._stopping = False

    # -------------------------------------------------------------- lifecycle
    def _command(self) -> list[str]:
        command = [
            self.claude_bin,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--setting-sources",
            "",
            "--mcp-config",
            str(self.mcp_config),
            "--strict-mcp-config",
            "--permission-mode",
            "bypassPermissions",
            "--dangerously-skip-permissions",
            "--disallowedTools",
            DISALLOWED_TOOLS,
            "--tools",
            WORKSPACE_TOOLS,
            "--max-turns",
            str(self.max_turns),
            "--name",
            self.agent_name,
        ]
        if self.resume_session:
            command.extend(["--resume", self.resume_session])
        else:
            command.extend(["--session-id", _session_uuid(self.run_id, self.agent_id)])
        if self.model:
            command.extend(["--model", self.model])
        if self.world_prompt.strip():
            command.extend(["--append-system-prompt", self.world_prompt])
        return command

    async def start(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        # The Claude process and its shell tools do not need service secrets.
        # The MCP subprocess receives its token through the generated config.
        for secret_name in (
            "CITY_ADMIN_TOKEN",
            "CITY_DATABASE_URL",
            "CITY_AGENT_TOKEN",
            "CITY_MCP_CONFIG",
        ):
            env.pop(secret_name, None)
        env.update(
            {
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_TELEMETRY": "1",
                "DISABLE_ERROR_REPORTING": "1",
                "DISABLE_AUTOUPDATER": "1",
                "NO_COLOR": "1",
                "TERM": "dumb",
            }
        )
        command = self._command()
        try:
            self.process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workdir),
                env=env,
                # Tool results can be large; the default 64 KiB StreamReader
                # limit would abort a legitimate stream-json frame.
                limit=16 * 1024 * 1024,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"claude CLI not found: {self.claude_bin}") from exc
        log.info("claude started pid=%s turn=%s", self.process.pid, self.current_turn)
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def wait_ready(self, timeout: float = 180.0) -> None:
        ready_task = asyncio.create_task(self.ready.wait())
        exit_task = asyncio.create_task(self.exited.wait())
        try:
            await asyncio.wait(
                {ready_task, exit_task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (ready_task, exit_task):
                if not task.done():
                    task.cancel()
        if self.ready.is_set():
            return
        if self.exited.is_set():
            raise RuntimeError(
                f"claude exited before session init (code {self.exit_code}); stderr: "
                f"{self.stderr_tail()[-1000:]}"
            )
        raise RuntimeError("claude did not report session init in time")

    async def send_initial(self, text: str) -> None:
        """Write the first user turn.

        The stream-json CLI does not emit its init frame until it receives its
        first input, so the first message is allowed before the ready event. It
        still refuses to write during an active turn.
        """
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("claude process is not running")
        await self._write_user(text)

    async def send_user(self, text: str) -> None:
        """Write one user message to Claude. Only legal when no turn is active."""
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("claude process is not running")
        if not self.ready.is_set():
            raise RuntimeError("claude is not ready")
        await self._write_user(text)

    async def wait_for_turn(self, timeout: float = 300.0) -> int:
        turn_task = asyncio.create_task(self.turn_ended.wait())
        exit_task = asyncio.create_task(self.exited.wait())
        try:
            await asyncio.wait(
                {turn_task, exit_task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (turn_task, exit_task):
                if not task.done():
                    task.cancel()
        if not self.turn_ended.is_set():
            if self.exited.is_set():
                raise RuntimeError(f"claude exited before a turn result (code {self.exit_code})")
            raise TimeoutError("claude turn timed out")
        return self.current_turn

    async def wait_exit(self, timeout: float | None = None) -> int | None:
        if timeout is None:
            await self.exited.wait()
        else:
            await asyncio.wait_for(self.exited.wait(), timeout=timeout)
        return self.exit_code

    async def _write_user(self, text: str) -> None:
        if self.working:
            raise RuntimeError("refusing to write stdin while a turn is active")
        message = {
            "type": "user",
            "message": {"role": "user", "content": text},
            "parent_tool_use_id": None,
            "session_id": "default",
        }
        async with self._write_lock:
            if self.working:
                raise RuntimeError("turn became active while waiting for stdin lock")
            self.current_turn += 1
            self.working = True
            self.turn_ended.clear()
            self.last_result = None
            self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
            await self.process.stdin.drain()
        log.info("wrote user turn %s (%s chars)", self.current_turn, len(text))
        self.trace(
            {
                "type": "turn_started",
                "turn": self.current_turn,
                "chars": len(text),
                "at": time.time(),
            }
        )

    async def stop(self, grace: float = 8.0) -> None:
        self._stopping = True
        process = self.process
        if process is None:
            return
        try:
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=grace)
        except TimeoutError:
            log.warning("claude did not exit in %.1fs; terminating", grace)
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        if self._reader_task:
            self._reader_task.cancel()
        if self._stderr_task:
            self._stderr_task.cancel()

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail)

    # ------------------------------------------------------------------ io
    async def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        stream = self.process.stdout
        try:
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("non-json stdout: %s", line[:300])
                    continue
                await self._handle_event(event)
        except asyncio.CancelledError:
            return
        except Exception:  # pragma: no cover - defensive
            log.exception("stdout reader failed")
        finally:
            code = await self.process.wait()
            self.exit_code = code
            self.exited.set()
            log.info("claude exited code=%s", code)
            if not self._stopping and self.on_exit is not None:
                try:
                    await self.on_exit(code)
                except Exception:
                    log.exception("on_exit callback failed")

    async def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        try:
            while True:
                raw = await self.process.stderr.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self._stderr_tail.append(line)
                    log.info("claude: %s", line[:500])
        except asyncio.CancelledError:
            return
        except Exception:  # pragma: no cover - defensive
            return

    async def _handle_event(self, event: dict) -> None:
        kind = event.get("type")
        subtype = event.get("subtype")
        if kind == "system" and subtype == "init":
            self.session_id = str(event.get("session_id") or "")
            self.ready.set()
            self.trace(
                {
                    "type": "session_initialized",
                    "session_id": self.session_id,
                    "model": event.get("model"),
                    "tools": event.get("tools"),
                    "permission_mode": event.get("permissionMode"),
                }
            )
            return
        if kind == "assistant":
            content = (event.get("message") or {}).get("content") or []
            texts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    texts.append(str(block["text"]))
                elif block.get("type") == "tool_use":
                    self.trace(
                        {
                            "type": "tool_use",
                            "turn": event.get("turn", self.current_turn),
                            "name": block.get("name"),
                            "tool_use_id": block.get("id"),
                            "input_preview": json.dumps(block.get("input"), ensure_ascii=False)[:1000],
                        }
                    )
                    log.info("tool_use %s", block.get("name"))
            if texts:
                text = "\n".join(texts)
                self.trace(
                    {
                        "type": "assistant_text",
                        "turn": event.get("turn", self.current_turn),
                        "text": text[:8000],
                    }
                )
                log.info("assistant: %s", text[:300].replace("\n", " "))
            return
        if kind == "user":
            content = (event.get("message") or {}).get("content") or []
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                raw = block.get("content")
                body = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
                self.trace(
                    {
                        "type": "tool_result",
                        "turn": event.get("turn", self.current_turn),
                        "tool_use_id": block.get("tool_use_id"),
                        "is_error": bool(block.get("is_error")),
                        "preview": body[:2000],
                    }
                )
            return
        if kind == "result":
            self.working = False
            result = {
                "type": "turn_result",
                "turn": self.current_turn,
                "stop_reason": event.get("stop_reason"),
                "is_error": bool(event.get("is_error")),
                "duration_ms": event.get("duration_ms"),
                "duration_api_ms": event.get("duration_api_ms"),
                "usage": event.get("usage") or {},
                "cost_usd": event.get("total_cost_usd") or 0,
                "num_turns": event.get("num_turns"),
            }
            self.last_result = result
            self.trace(result)
            self.turn_ended.set()
            log.info(
                "turn %s ended stop=%s cost=%s",
                self.current_turn,
                result["stop_reason"],
                result["cost_usd"],
            )
            if self.on_turn_end is not None:
                try:
                    await self.on_turn_end(result)
                except Exception:
                    log.exception("turn-end callback failed")
            return
        if kind == "system" and subtype == "api_retry":
            log.warning("api retry %s/%s", event.get("attempt"), event.get("max_retries"))
            return
        if kind == "system" and subtype == "hook_response" and event.get("exit_code") not in (None, 0):
            log.warning("hook failed: %s", str(event.get("stderr"))[:300])
