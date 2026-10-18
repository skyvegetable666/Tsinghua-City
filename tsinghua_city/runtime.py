"""Agent runtime manager and local process adapter.

The logical agent is a database identity; a runtime is a replaceable execution
process. This module owns process lifecycle only, never world state. The local
adapter is useful for harness development and tests; a sandbox/container adapter
implements the same interface in a deployed experiment.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import service
from .config import Settings, iso, utcnow
from .models import Agent, AgentRuntime, Message, World

RUNNING = "RUNNING"
SUSPENDED = "SUSPENDED"
STOPPED = "STOPPED"
CRASHED = "CRASHED"
LOST = "LOST"
RESTART_NEVER = "never"
RESTART_ON_FAILURE = "on_failure"
ACTIVE = {RUNNING, SUSPENDED}


@dataclass
class StartResult:
    pid: int
    pid_create_time: float
    log_path: str
    mcp_config_path: str


class LocalRuntimeAdapter:
    name = "local"

    def __init__(self, *, api_url: str, log_dir: str) -> None:
        self.api_url = api_url.rstrip("/")
        self.log_dir = Path(log_dir).expanduser().resolve()

    def _write_mcp_config(self, runtime_id: str, token: str, agent_id: str, world_id: str) -> Path:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / f"{runtime_id}.mcp.json"
        payload = {
            "mcpServers": {
                "tsinghua-city": {
                    "command": sys.executable,
                    "args": ["-m", "tsinghua_city", "mcp"],
                    "env": {
                        "CITY_API_URL": self.api_url,
                        "CITY_AGENT_TOKEN": token,
                        "CITY_AGENT_ID": agent_id,
                        "CITY_WORLD_ID": world_id,
                    },
                }
            }
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return path

    def start(
        self,
        *,
        runtime_id: str,
        command: list[str],
        agent_id: str,
        world_id: str,
        token: str,
    ) -> StartResult:
        if not command or any(not str(part) for part in command):
            raise ValueError("runtime command must be a non-empty argv list")
        argv = [str(part) for part in command]
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"{runtime_id}.log"
        mcp_path = self._write_mcp_config(runtime_id, token, agent_id, world_id)
        # Do not leak the server environment (admin token, database URL, etc.)
        # into an agent process. Only runtime-safe variables are inherited.
        safe_names = (
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "TEMP",
            "TMP",
            "SYSTEMROOT",
            "COMSPEC",
            "PATHEXT",
            "VIRTUAL_ENV",
            "PYTHONPATH",
        )
        env = {name: os.environ[name] for name in safe_names if name in os.environ}
        env.update(
            {
                "CITY_API_URL": self.api_url,
                "CITY_AGENT_TOKEN": token,
                "CITY_AGENT_ID": agent_id,
                "CITY_WORLD_ID": world_id,
                "CITY_MCP_CONFIG": str(mcp_path),
            }
        )
        log_file = log_path.open("ab")
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(Path.cwd()),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception:
            log_file.close()
            mcp_path.unlink(missing_ok=True)
            raise
        finally:
            log_file.close()
        try:
            create_time = psutil.Process(process.pid).create_time()
        except Exception:
            process.terminate()
            mcp_path.unlink(missing_ok=True)
            raise
        return StartResult(
            pid=process.pid,
            pid_create_time=create_time,
            log_path=str(log_path),
            mcp_config_path=str(mcp_path),
        )

    def is_running(self, runtime: AgentRuntime) -> bool:
        if runtime.pid is None:
            return False
        try:
            process = psutil.Process(runtime.pid)
            if (
                runtime.pid_create_time is not None
                and abs(process.create_time() - runtime.pid_create_time) > 0.01
            ):
                return False
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return False

    def stop(self, runtime: AgentRuntime) -> int | None:
        exit_code: int | None = None
        if runtime.pid is not None:
            try:
                process = psutil.Process(runtime.pid)
                if (
                    runtime.pid_create_time is not None
                    and abs(process.create_time() - runtime.pid_create_time) > 0.01
                ):
                    raise RuntimeError("refusing to stop a reused pid")
                if process.is_running():
                    process.terminate()
                    try:
                        exit_code = process.wait(timeout=5)
                    except psutil.TimeoutExpired:
                        process.kill()
                        exit_code = process.wait(timeout=5)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
        if runtime.mcp_config_path:
            Path(runtime.mcp_config_path).unlink(missing_ok=True)
        return exit_code


class RuntimeManager:
    def __init__(self, settings: Settings, session_factory) -> None:
        self.settings = settings
        self._session_factory = session_factory
        self.adapter = LocalRuntimeAdapter(
            api_url=settings.api_url,
            log_dir=settings.runtime_log_dir,
        )

    def get_runtime(self, session: Session, world: World, agent: Agent) -> AgentRuntime | None:
        return session.scalars(
            select(AgentRuntime).where(
                AgentRuntime.world_id == world.id,
                AgentRuntime.agent_id == agent.id,
            )
        ).one_or_none()

    def list_world(self, session: Session, world: World) -> list[AgentRuntime]:
        return list(
            session.scalars(
                select(AgentRuntime)
                .where(AgentRuntime.world_id == world.id)
                .order_by(AgentRuntime.created_at)
            ).all()
        )

    def _event(
        self,
        session: Session,
        runtime: AgentRuntime,
        event_type: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        service.record_event(
            session,
            runtime.world_id,
            event_type,
            actor_agent_id=runtime.agent_id,
            data={"runtime_id": runtime.id, **(data or {})},
        )

    def _start(
        self,
        session: Session,
        world: World,
        agent: Agent,
        *,
        command: list[str],
        runtime: AgentRuntime | None,
        restart_policy: str | None = None,
        max_restarts: int | None = None,
        is_restart: bool = False,
    ) -> AgentRuntime:
        service.require_alive(session, agent, world)
        if runtime is not None and runtime.status == RUNNING:
            raise service.DomainError("agent runtime is already running", 409)
        if restart_policy is not None and restart_policy not in {
            RESTART_NEVER,
            RESTART_ON_FAILURE,
        }:
            raise service.DomainError("restart_policy must be never or on_failure", 400)
        if max_restarts is not None and max_restarts < 0:
            raise service.DomainError("max_restarts must be >= 0", 400)
        token = service.rotate_agent_token(session, agent)
        now = utcnow()
        runtime_id = runtime.id if runtime else "rt_" + uuid.uuid4().hex[:24]
        try:
            started = self.adapter.start(
                runtime_id=runtime_id,
                command=command,
                agent_id=agent.id,
                world_id=world.id,
                token=token,
            )
        except Exception:
            session.rollback()
            raise
        if runtime is None:
            runtime = AgentRuntime(
                id=runtime_id,
                world_id=world.id,
                agent_id=agent.id,
                created_at=now,
                updated_at=now,
                command=command,
                adapter=self.adapter.name,
                status=RUNNING,
                restart_policy=restart_policy or RESTART_NEVER,
                max_restarts=max_restarts if max_restarts is not None else 3,
                restart_count=0,
            )
            session.add(runtime)
        runtime.adapter = self.adapter.name
        runtime.command = list(command)
        if restart_policy is not None:
            runtime.restart_policy = restart_policy
        if max_restarts is not None:
            runtime.max_restarts = max_restarts
        if is_restart:
            runtime.restart_count += 1
            runtime.last_restart_at = now
        else:
            runtime.restart_count = 0
            runtime.next_restart_at = None
        runtime.status = RUNNING
        runtime.pid = started.pid
        runtime.pid_create_time = started.pid_create_time
        runtime.log_path = started.log_path
        runtime.mcp_config_path = started.mcp_config_path
        runtime.exit_code = None
        runtime.last_error = None
        runtime.started_at = now
        runtime.stopped_at = None
        runtime.next_restart_at = None
        runtime.updated_at = now
        self._event(
            session,
            runtime,
            "runtime.restarted" if is_restart else "runtime.started",
            data={
                "pid": started.pid,
                "command": command,
                "restart_count": runtime.restart_count,
            },
        )
        try:
            session.commit()
        except Exception:
            session.rollback()
            self.adapter.stop(runtime)
            raise
        session.refresh(runtime)
        return runtime

    def start(
        self,
        session: Session,
        world: World,
        agent: Agent,
        *,
        command: list[str],
        adapter: str = "local",
        restart_policy: str = RESTART_NEVER,
        max_restarts: int = 3,
    ) -> AgentRuntime:
        if adapter != self.adapter.name:
            raise service.DomainError(f"unsupported runtime adapter: {adapter}", 400)
        runtime = self.get_runtime(session, world, agent)
        return self._start(
            session,
            world,
            agent,
            command=command,
            runtime=runtime,
            restart_policy=restart_policy,
            max_restarts=max_restarts,
        )

    def _stop(
        self,
        session: Session,
        world: World,
        agent: Agent,
        *,
        status: str,
        event_type: str,
    ) -> AgentRuntime:
        runtime = self.get_runtime(session, world, agent)
        if runtime is None:
            raise service.DomainError("runtime not found", 404)
        if runtime.status not in {RUNNING, SUSPENDED, CRASHED, LOST}:
            raise service.DomainError(f"runtime is not stoppable ({runtime.status})", 409)
        exit_code = self.adapter.stop(runtime)
        service.rotate_agent_token(session, agent)
        runtime.next_restart_at = None
        runtime.status = status
        runtime.exit_code = exit_code
        runtime.pid = None
        runtime.pid_create_time = None
        runtime.mcp_config_path = None
        runtime.stopped_at = utcnow()
        runtime.updated_at = runtime.stopped_at
        self._event(session, runtime, event_type, data={"exit_code": exit_code})
        session.commit()
        session.refresh(runtime)
        return runtime

    def stop(self, session: Session, world: World, agent: Agent) -> AgentRuntime:
        return self._stop(
            session,
            world,
            agent,
            status=STOPPED,
            event_type="runtime.stopped",
        )

    def terminate(self, session: Session, world: World, agent: Agent) -> AgentRuntime:
        return self._stop(
            session,
            world,
            agent,
            status=STOPPED,
            event_type="runtime.stopped",
        )

    def suspend(self, session: Session, world: World, agent: Agent) -> AgentRuntime:
        return self._stop(
            session,
            world,
            agent,
            status=SUSPENDED,
            event_type="runtime.suspended",
        )

    def resume(self, session: Session, world: World, agent: Agent) -> AgentRuntime:
        runtime = self.get_runtime(session, world, agent)
        if runtime is None:
            raise service.DomainError("runtime not found", 404)
        if runtime.status == RUNNING:
            raise service.DomainError("agent runtime is already running", 409)
        return self._start(
            session,
            world,
            agent,
            command=list(runtime.command),
            runtime=runtime,
        )

    def _mark_crashed(self, session: Session, runtime: AgentRuntime, reason: str) -> None:
        if runtime.mcp_config_path:
            Path(runtime.mcp_config_path).unlink(missing_ok=True)
            runtime.mcp_config_path = None
        agent = session.get(Agent, runtime.agent_id)
        if agent is not None:
            # The process may have held a valid token; invalidate it.
            service.rotate_agent_token(session, agent)
        now = utcnow()
        runtime.status = CRASHED
        runtime.pid = None
        runtime.pid_create_time = None
        runtime.last_error = reason
        runtime.updated_at = now
        if (
            runtime.restart_policy == RESTART_ON_FAILURE
            and runtime.restart_count < runtime.max_restarts
        ):
            delay = min(60, 2 ** min(runtime.restart_count, 6))
            runtime.next_restart_at = now + delay
        else:
            runtime.next_restart_at = None
        self._event(session, runtime, "runtime.crashed", data={"reason": reason})

    def status(self, session: Session, world: World, agent: Agent) -> AgentRuntime:
        runtime = self.get_runtime(session, world, agent)
        if runtime is None:
            raise service.DomainError("runtime not found", 404)
        if runtime.status == RUNNING and not self.adapter.is_running(runtime):
            self._mark_crashed(session, runtime, "process is not running")
            session.commit()
            session.refresh(runtime)
        return runtime

    def wake_suspended(self, session: Session, world: World) -> int:
        """Resume suspended runtimes that have unread inbox messages.

        The process can then read the durable inbox through MCP. This is a wake
        policy, not a forced injection into an active model call.
        """
        woken = 0
        suspended = session.scalars(
            select(AgentRuntime).where(
                AgentRuntime.world_id == world.id,
                AgentRuntime.status == SUSPENDED,
            )
        ).all()
        for runtime in suspended:
            has_unread = session.scalar(
                select(Message.id)
                .where(
                    Message.recipient_agent_id == runtime.agent_id,
                    Message.read_at.is_(None),
                )
                .limit(1)
            )
            if has_unread is None:
                continue
            agent = session.get(Agent, runtime.agent_id)
            if agent is None:
                continue
            try:
                self.resume(session, world, agent)
                woken += 1
            except service.DomainError:
                session.rollback()
                continue
        return woken

    def poll_running(self, session: Session, world: World) -> int:
        changed = 0
        runtimes = session.scalars(
            select(AgentRuntime).where(
                AgentRuntime.world_id == world.id,
                AgentRuntime.status == RUNNING,
            )
        ).all()
        for runtime in runtimes:
            if not self.adapter.is_running(runtime):
                self._mark_crashed(session, runtime, "process is not running")
                changed += 1
        if changed:
            session.commit()
        return changed

    def restart_due(self, session: Session, world: World) -> int:
        now = utcnow()
        runtimes = session.scalars(
            select(AgentRuntime).where(
                AgentRuntime.world_id == world.id,
                AgentRuntime.status == CRASHED,
                AgentRuntime.restart_policy == RESTART_ON_FAILURE,
                AgentRuntime.next_restart_at.is_not(None),
                AgentRuntime.next_restart_at <= now,
                AgentRuntime.restart_count < AgentRuntime.max_restarts,
            )
        ).all()
        restarted = 0
        for runtime in runtimes:
            agent = session.get(Agent, runtime.agent_id)
            if agent is None:
                continue
            try:
                self._start(
                    session,
                    world,
                    agent,
                    command=list(runtime.command),
                    runtime=runtime,
                    restart_policy=runtime.restart_policy,
                    max_restarts=runtime.max_restarts,
                    is_restart=True,
                )
                restarted += 1
            except service.DomainError:
                session.rollback()
                current = self.get_runtime(session, world, agent)
                if current is not None:
                    current.next_restart_at = None
                    session.commit()
                continue
        return restarted

    def reconcile(self) -> int:
        changed = 0
        with self._session_factory() as session:
            runtimes = session.scalars(
                select(AgentRuntime).where(AgentRuntime.status == RUNNING)
            ).all()
            for runtime in runtimes:
                if not self.adapter.is_running(runtime):
                    self._mark_crashed(session, runtime, "lost on manager restart")
                    changed += 1
            if changed:
                session.commit()
        return changed

    def view(self, runtime: AgentRuntime) -> dict[str, Any]:
        return {
            "id": runtime.id,
            "world_id": runtime.world_id,
            "agent_id": runtime.agent_id,
            "adapter": runtime.adapter,
            "status": runtime.status,
            "restart_policy": runtime.restart_policy,
            "max_restarts": runtime.max_restarts,
            "restart_count": runtime.restart_count,
            "next_restart_at": iso(runtime.next_restart_at),
            "last_restart_at": iso(runtime.last_restart_at),
            "command": runtime.command,
            "pid": runtime.pid,
            "log_path": runtime.log_path,
            "mcp_config_path": runtime.mcp_config_path,
            "exit_code": runtime.exit_code,
            "last_error": runtime.last_error,
            "created_at": iso(runtime.created_at),
            "updated_at": iso(runtime.updated_at),
            "started_at": iso(runtime.started_at),
            "stopped_at": iso(runtime.stopped_at),
        }
