"""Thin control-plane SDK for a Genesis world.

This module does not implement message routing or an agent harness.  It is a
convenience layer over the existing supervisor process:

    Python script
        -> World.start()
        -> python -m supervisor up (background child)
        -> broker + Docker containers (existing core)

The SDK owns process lifecycle, environment/prompt preparation, status queries,
event tailing, and graceful shutdown.  Agent-to-agent traffic still goes through
the broker and the MCP tool, never through this API.
"""

from __future__ import annotations

import json
import os
import socket
import string
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from .spec import (
    MIN_AGENTS,
    AgentSpec,
    WorldSpec,
    coerce_agent,
    coerce_agents,
)

SDK_ROOT = Path(__file__).resolve().parent.parent
GENESIS_KEYS = {
    "agent_name",
    "peer_name",
    "peers",
    "agent_count",
    "mission",
    "run_id",
    "genesis_time",
}
GENESIS_REQUIRED = {"agent_name", "mission", "run_id", "genesis_time"}


class WorldError(RuntimeError):
    """Raised when a world cannot be prepared, started, queried, or stopped."""


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorldError(f"cannot read {path}: {exc}") from exc


def _tail(path: Path, limit: int = 4000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", "replace")


def _free_port() -> int:
    """Reserve a local ephemeral port, then release it for the child broker."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class World:
    """A not-yet-started or running world.

    Construction is side-effect free.  start() launches the existing supervisor
    in a child process; stop() (or the context manager) shuts it down.  The
    object is intentionally synchronous: the supervisor already owns the async
    event loop inside the child, so the SDK stays simple and usable from
    ordinary scripts and notebooks.
    """

    def __init__(self, spec: WorldSpec | None = None) -> None:
        self.spec: WorldSpec = spec or WorldSpec()
        self.spec.agents = coerce_agents(self.spec.agents)
        self._proc: subprocess.Popen[bytes] | None = None
        self._log_handle: Any = None
        self._log_path: Path | None = None
        self._spec_dir: Path | None = None
        self._env: dict[str, str] | None = None
        self._root: Path | None = None
        self._current: dict[str, Any] | None = None
        self._token: str | None = None
        self._base_url: str | None = None
        self._run_dir: Path | None = None
        self._started = False
        self._stopped = False

    # ---------------------------------------------------------------- facade
    @classmethod
    def create(
        cls,
        agents: Sequence[str | AgentSpec] | None = None,
        **spec_kwargs: Any,
    ) -> World:
        """Create a world description without starting anything.

        Examples:
            World.create(agents=["agent-a", "agent-b"], mock=True)
            World.create(mission="...", max_seconds=60)
        """
        spec = WorldSpec(agents=coerce_agents(agents), **spec_kwargs)
        return cls(spec)

    @property
    def agents(self) -> list[AgentSpec]:
        return list(self.spec.agents)

    @property
    def started(self) -> bool:
        return self._started

    @property
    def running(self) -> bool:
        return bool(self._proc is not None and self._proc.poll() is None)

    @property
    def run_id(self) -> str | None:
        return None if self._current is None else str(self._current.get("run_id"))

    @property
    def run_dir(self) -> Path | None:
        return self._run_dir

    @property
    def base_url(self) -> str | None:
        return self._base_url

    @property
    def token(self) -> str | None:
        return self._token

    @property
    def log_path(self) -> Path | None:
        return self._log_path

    @property
    def process(self) -> subprocess.Popen[bytes] | None:
        return self._proc

    def add_agent(self, agent: str | AgentSpec) -> World:
        """Register an agent before start.  Worlds support N >= 2 agents."""
        if self._started:
            raise WorldError("cannot add an agent after start()")
        spec = coerce_agent(agent)
        if any(existing.name == spec.name for existing in self.spec.agents):
            raise WorldError(f"duplicate agent name: {spec.name}")
        self.spec.agents.append(spec)
        return self

    def register_agent(self, agent: str | AgentSpec) -> World:
        """Alias for add_agent(), for experiment scripts that use 'register'."""
        return self.add_agent(agent)

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def __repr__(self) -> str:
        names = ", ".join(agent.name for agent in self.spec.agents)
        state = "running" if self.running else ("started" if self._started else "new")
        return f"World(state={state}, agents=[{names}], run_id={self.run_id!r})"

    # --------------------------------------------------------------- prepare
    def _project_root(self) -> Path:
        root = Path(self.spec.project_root or SDK_ROOT).expanduser().resolve()
        if not (root / "supervisor").is_dir() or not (root / "docker-compose.yml").is_file():
            raise WorldError(
                f"{root} does not look like a Genesis Lab checkout "
                "(expected supervisor/ and docker-compose.yml)"
            )
        return root

    def _validate_start(self) -> None:
        if self._started:
            raise WorldError("world is already started")
        if len(self.spec.agents) < MIN_AGENTS:
            raise WorldError(
                f"a world needs at least {MIN_AGENTS} agents, "
                f"got {len(self.spec.agents)}; call add_agent() before start()"
            )
        names = [agent.name for agent in self.spec.agents]
        if len(set(names)) != len(names):
            raise WorldError(f"duplicate agent names: {names}")

    def _resolve_prompts(self, root: Path) -> tuple[str, str, str]:
        world_rules = (
            self.spec.world_rules
            if self.spec.world_rules is not None
            else _read_text(root / "config" / "world.md")
        )
        mission = (
            self.spec.mission
            if self.spec.mission is not None
            else _read_text(root / "config" / "mission.md")
        )
        genesis = (
            self.spec.genesis_template
            if self.spec.genesis_template is not None
            else _read_text(root / "config" / "genesis.md")
        )
        world_rules = world_rules.strip() + "\n"
        mission = mission.strip()
        identifiers = set(string.Template(genesis).get_identifiers())
        unknown = identifiers - GENESIS_KEYS
        if unknown:
            raise WorldError(f"genesis_template uses unknown placeholders: {sorted(unknown)}")
        missing = GENESIS_REQUIRED - identifiers
        if not ({"peers", "peer_name"} & identifiers):
            missing.add("peers or peer_name")
        if missing:
            raise WorldError(f"genesis_template is missing placeholders: {sorted(missing)}")
        return world_rules, mission, genesis

    def _write_prompts(self, prompts: tuple[str, str, str], directory: Path) -> None:
        world_rules, mission, genesis = prompts
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "world.md").write_text(world_rules, encoding="utf-8")
        (directory / "mission.md").write_text(mission, encoding="utf-8")
        (directory / "genesis.md").write_text(genesis, encoding="utf-8")

    def _resolve_port(self) -> int:
        if self.spec.broker_port not in (None, 0):
            return int(self.spec.broker_port)
        return _free_port()

    def _build_env(self, root: Path, spec_dir: Path, port: int) -> dict[str, str]:
        env = dict(os.environ)
        # A user-level BROKER_URL would override the host-side container URL;
        # the child must derive it from BROKER_PORT for this world.
        env.pop("BROKER_URL", None)
        env.update(
            {
                "WORLD_FILE": str(spec_dir / "world.md"),
                "MISSION_FILE": str(spec_dir / "mission.md"),
                "GENESIS_FILE": str(spec_dir / "genesis.md"),
                "AGENTS": json.dumps(
                    [agent.name for agent in self.spec.agents],
                    ensure_ascii=False,
                ),
                "AGENT_A_NAME": self.spec.agents[0].name,
                "AGENT_B_NAME": self.spec.agents[1].name,
                "BROKER_HOST": "0.0.0.0",
                "BROKER_PORT": str(port),
                "PYTHONUNBUFFERED": "1",
            }
        )
        if self.spec.model:
            env["ANTHROPIC_MODEL"] = self.spec.model
        if self.spec.image:
            env["AGENT_IMAGE"] = self.spec.image
        if self.spec.max_seconds is not None:
            env["RUN_MAX_SECONDS"] = str(self.spec.max_seconds)
        if self.spec.max_messages is not None:
            env["RUN_MAX_MESSAGES"] = str(self.spec.max_messages)
        if self.spec.end_grace_seconds is not None:
            env["END_GRACE_SECONDS"] = str(self.spec.end_grace_seconds)
        return env

    def _build_command(self) -> list[str]:
        command = [sys.executable, "-m", "supervisor", "up"]
        if self.spec.mock:
            command.append("--mock")
        if self.spec.no_build:
            command.append("--no-build")
        if self.spec.max_seconds is not None:
            command.extend(["--max-seconds", str(self.spec.max_seconds)])
        if self.spec.max_messages is not None:
            command.extend(["--max-messages", str(self.spec.max_messages)])
        return command

    def _log_destination(self, root: Path) -> Path:
        if self.spec.log_path is not None:
            path = Path(self.spec.log_path).expanduser().resolve()
        else:
            path = root / "runs" / f"sdk-supervisor-{os.getpid()}-{uuid.uuid4().hex[:8]}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    # ---------------------------------------------------------------- lifecycle
    def start(self, *, timeout: float | None = None) -> World:
        """Start the supervisor and wait for genesis/RUNNING."""
        self._validate_start()
        root = self._project_root()
        self._root = root
        try:
            runs_dir = root / "runs"
            runs_dir.mkdir(parents=True, exist_ok=True)
            self._spec_dir = Path(tempfile.mkdtemp(prefix="sdk-spec-", dir=runs_dir))
            prompts = self._resolve_prompts(root)
            self._write_prompts(prompts, self._spec_dir)
            port = self._resolve_port()
            self._env = self._build_env(root, self._spec_dir, port)
            command = self._build_command()
            self._log_path = self._log_destination(root)
            self._log_handle = self._log_path.open("ab")
            self._proc = subprocess.Popen(
                command,
                cwd=str(root),
                env=self._env,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
            )
            startup_timeout = float(timeout if timeout is not None else self.spec.startup_timeout)
            self._current = self._wait_for_current(root, startup_timeout, port)
            self._token = str(self._current.get("token") or "")
            self._run_dir = Path(str(self._current.get("run_dir") or ""))
            self._base_url = f"http://127.0.0.1:{int(self._current.get('port'))}"
            self._wait_until_running(startup_timeout)
            self._write_sdk_spec()
        except Exception as exc:
            self._abort_start()
            if isinstance(exc, WorldError):
                raise
            raise WorldError(str(exc)) from exc
        self._started = True
        self._stopped = False
        return self

    def _wait_for_current(self, root: Path, timeout: float, port: int) -> dict[str, Any]:
        path = root / "runs" / "current.json"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise WorldError(self._startup_failure("supervisor exited before publishing current.json"))
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = None
            # Match on the broker port we assigned, not only on the child pid:
            # on Windows a venv python.exe is a launcher that re-spawns the real
            # interpreter, so Popen.pid never equals the supervisor's os.getpid().
            matched = data is not None and int(data.get("port") or -1) == int(port)
            if not matched and data and self._proc is not None:
                matched = int(data.get("pid") or -1) == self._proc.pid
            if matched:
                return data
            time.sleep(0.2)
        raise WorldError(self._startup_failure(f"timed out after {timeout:.0f}s waiting for supervisor"))

    def _wait_until_running(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise WorldError(self._startup_failure("supervisor exited before genesis"))
            try:
                status = self._status()
            except WorldError:
                time.sleep(0.2)
                continue
            phase = status.get("phase")
            if phase == "running":
                return
            if phase in ("ending", "ended"):
                raise WorldError(self._startup_failure(f"world ended during startup (phase={phase})"))
            time.sleep(0.2)
        raise WorldError(self._startup_failure("timed out waiting for genesis/RUNNING"))

    def _startup_failure(self, message: str) -> str:
        tail = _tail(self._log_path) if self._log_path else ""
        if tail:
            return f"{message}\n--- supervisor log tail ---\n{tail}"
        return message

    def _write_sdk_spec(self) -> None:
        if self._run_dir is None:
            return
        try:
            self._run_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "sdk_version": 1,
                "agents": [agent.name for agent in self.spec.agents],
                "spec": self.spec.to_dict(),
            }
            (self._run_dir / "sdk-spec.json").write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    def _abort_start(self) -> None:
        self._started = False
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        self._force_down()
        self._close_log()
        self._remove_spec_dir()
        self._proc = None
        self._current = None
        self._token = None
        self._base_url = None
        self._run_dir = None

    def _close_log(self) -> None:
        handle = self._log_handle
        self._log_handle = None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def _remove_spec_dir(self) -> None:
        directory = self._spec_dir
        self._spec_dir = None
        if directory is None:
            return
        for child in directory.glob("*"):
            try:
                if child.is_dir():
                    for nested in child.rglob("*"):
                        if nested.is_file():
                            nested.unlink()
                    child.rmdir()
                else:
                    child.unlink()
            except OSError:
                pass
        try:
            directory.rmdir()
        except OSError:
            pass

    def _force_down(self) -> None:
        if self._env is None or self._current is None or self._root is None:
            return
        try:
            subprocess.run(
                ["docker", "compose", "down", "--remove-orphans"],
                cwd=str(self._root),
                env=self._env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            current = self._root / "runs" / "current.json"
            if current.exists():
                data = json.loads(current.read_text(encoding="utf-8"))
                if self._proc is not None and int(data.get("pid") or -1) == self._proc.pid:
                    current.unlink()
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    # -------------------------------------------------------------------- http
    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._base_url or not self._token:
            raise WorldError("world is not started")
        body = None
        headers = {"Authorization": f"Bearer {self._token}"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self._base_url + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:1000]
            raise WorldError(f"broker HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise WorldError(f"broker request failed: {exc}") from exc

    def _status(self) -> dict[str, Any]:
        return self._request("GET", "/v1/status")

    # --------------------------------------------------------------- public API
    def status(self) -> dict[str, Any]:
        """Snapshot from GET /v1/status."""
        if not self._started:
            raise WorldError("world is not started")
        return self._status()

    def send_system(self, text: str, *, to: str = "all") -> dict[str, Any]:
        """Inject an operator/system message (not an agent tool call).

        This uses the existing host-only endpoint; it does not let the SDK
        impersonate agent A or B on the agent-to-agent channel.
        """
        if not self._started:
            raise WorldError("world is not started")
        return self._request("POST", "/v1/system/messages", {"to": to, "text": text})

    def events(self, *, follow: bool = False) -> Iterator[dict[str, Any]]:
        """Yield parsed events from runs/<id>/events.jsonl.

        follow=True keeps tailing until the supervisor child exits (or stop()
        is called).  This is a deliberately simple file-tailing API; a typed
        event stream can replace it later without changing the world lifecycle.
        """
        if self._run_dir is None:
            raise WorldError("world is not started")
        path = self._run_dir / "events.jsonl"
        position = 0
        while True:
            if path.exists():
                with path.open("r", encoding="utf-8") as handle:
                    handle.seek(position)
                    while True:
                        line = handle.readline()
                        if not line:
                            break
                        position = handle.tell()
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            continue
            if not follow:
                return
            if self._proc is None or self._proc.poll() is not None:
                return
            time.sleep(0.2)

    def wait(self, timeout: float | None = None) -> int:
        """Wait for the supervisor child to exit; return its exit code."""
        if self._proc is None:
            raise WorldError("world has not been started")
        return int(self._proc.wait(timeout=timeout))

    def report(self) -> dict[str, Any]:
        """Write transcript.md/metrics.json/messages.jsonl and return metrics."""
        if self._run_dir is None:
            raise WorldError("world has not produced a run directory yet")
        from supervisor.report import build_report

        return build_report(self._run_dir)

    def stop(self, *, timeout: float | None = None) -> None:
        """Ask the supervisor to end, then wait / terminate it."""
        if self._proc is None:
            self._started = False
            return
        stop_timeout = float(timeout if timeout is not None else self.spec.stop_timeout)
        if self._started and self._proc.poll() is None:
            try:
                self._request("POST", "/v1/end", {"reason": "sdk stop"})
            except WorldError:
                pass
        try:
            self._proc.wait(timeout=stop_timeout)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=10)
            self._force_down()
        if not self._stopped and self._proc.returncode not in (0, None):
            self._force_down()
        self._close_log()
        self._remove_spec_dir()
        self._started = False
        self._stopped = True
