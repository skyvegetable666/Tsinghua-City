"""Thin Docker runtime adapter for agent containers.

This is intentionally a wrapper around the Docker CLI, not a container
runtime. The archived genesis-lab Dockerfile and compose generator are the
reference; this adapter adds only the Tsinghua City-specific config, token,
workspace, and labels.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


class DockerError(RuntimeError):
    pass


@dataclass(frozen=True)
class DockerStartResult:
    pid: int
    pid_create_time: float
    log_path: str
    mcp_config_path: str


def _container_api_url(api_url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(api_url)
    host = parts.hostname or ""
    if host in {"127.0.0.1", "localhost", "0.0.0.0", "::1"}:
        netloc = "host.docker.internal"
        if parts.port is not None:
            netloc += f":{parts.port}"
        return urlunsplit((parts.scheme or "http", netloc, parts.path, parts.query, parts.fragment))
    return api_url


class DockerRuntimeAdapter:
    name = "docker"

    def __init__(
        self,
        *,
        api_url: str,
        log_dir: str,
        image: str,
        docker_bin: str = "docker",
        container_python: str = "/opt/venv/bin/python",
        volume_prefix: str = "tsinghua-city-workspace",
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.container_api_url = _container_api_url(self.api_url)
        self.log_dir = Path(log_dir).expanduser().resolve()
        self.image = image
        self.docker_bin = docker_bin
        self.container_python = container_python
        self.volume_prefix = volume_prefix
        self._log_streams: dict[str, subprocess.Popen] = {}

    def _container_name(self, runtime_id: str) -> str:
        return "tsinghua-agent-" + runtime_id

    def _workspace_volume(self, runtime_id: str) -> str:
        return f"{self.volume_prefix}-{runtime_id}"

    def _docker(
        self,
        args: list[str],
        *,
        check: bool = True,
        timeout: float = 120.0,
    ) -> subprocess.CompletedProcess:
        try:
            process = subprocess.run(
                [self.docker_bin, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise DockerError("docker CLI not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise DockerError(f"docker command timed out: {' '.join(args)}") from exc
        if check and process.returncode != 0:
            detail = (process.stderr or process.stdout or "").strip()[-2000:]
            raise DockerError(
                f"docker {' '.join(args)} failed ({process.returncode}): {detail}"
            )
        return process

    def _write_mcp_config(
        self,
        runtime_id: str,
        token: str,
        agent_id: str,
        world_id: str,
    ) -> Path:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / f"{runtime_id}.docker.mcp.json"
        payload = {
            "mcpServers": {
                "tsinghua-city": {
                    "command": self.container_python,
                    "args": ["-m", "tsinghua_city", "mcp"],
                    "env": {
                        "CITY_API_URL": self.container_api_url,
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

    def _start_log_stream(self, runtime_id: str, container_name: str, log_path: Path) -> None:
        if runtime_id in self._log_streams:
            return
        log_file = None
        try:
            log_file = log_path.open("ab")
            process = subprocess.Popen(
                [self.docker_bin, "logs", "-f", container_name],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError):
            if log_file is not None:
                log_file.close()
            return
        log_file.close()
        self._log_streams[runtime_id] = process

    def start(
        self,
        *,
        runtime_id: str,
        command: list[str],
        agent_id: str,
        world_id: str,
        token: str,
    ) -> DockerStartResult:
        if not command or any(not str(part) for part in command):
            raise ValueError("runtime command must be a non-empty argv list")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"{runtime_id}.docker.log"
        mcp_path = self._write_mcp_config(runtime_id, token, agent_id, world_id)
        container_name = self._container_name(runtime_id)
        volume = self._workspace_volume(runtime_id)
        self._docker(["rm", "-f", container_name], check=False)
        args = [
            "run",
            "-d",
            "--name",
            container_name,
            "--label",
            f"tsinghua-city.runtime_id={runtime_id}",
            "--label",
            f"tsinghua-city.agent_id={agent_id}",
            "--add-host",
            "host.docker.internal:host-gateway",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "-v",
            f"{volume}:/workspace",
            "-v",
            f"{mcp_path}:/run/tsinghua/mcp.json:ro",
            "-e",
            "CITY_MCP_CONFIG=/run/tsinghua/mcp.json",
            "-e",
            f"CITY_AGENT_ID={agent_id}",
            "-e",
            f"CITY_WORLD_ID={world_id}",
            "-e",
            "CITY_AGENT_WORKDIR=/workspace",
            "-e",
            "CITY_TRACE_DIR=/workspace/transcripts",
            "-e",
            f"CITY_API_URL={self.container_api_url}",
            self.image,
            *[str(part) for part in command],
        ]
        try:
            self._docker(args, timeout=180.0)
        except Exception:
            mcp_path.unlink(missing_ok=True)
            raise
        inspect = self._docker(
            ["inspect", "--format", "{{.State.Pid}}", container_name],
            check=False,
        )
        try:
            pid = int((inspect.stdout or "0").strip() or "0")
        except ValueError:
            pid = 0
        create_time = 0.0
        if pid > 0:
            try:
                import psutil

                create_time = psutil.Process(pid).create_time()
            except Exception:
                create_time = 0.0
        self._start_log_stream(runtime_id, container_name, log_path)
        return DockerStartResult(
            pid=pid,
            pid_create_time=create_time,
            log_path=str(log_path),
            mcp_config_path=str(mcp_path),
        )

    def is_running(self, runtime) -> bool:
        name = self._container_name(runtime.id)
        try:
            result = self._docker(
                ["inspect", "--format", "{{.State.Running}}", name],
                check=False,
                timeout=30.0,
            )
        except DockerError:
            return False
        return (result.stdout or "").strip().lower() == "true"

    def stop(self, runtime) -> int | None:
        name = self._container_name(runtime.id)
        exit_code: int | None = None
        inspect = self._docker(
            ["inspect", "--format", "{{.State.ExitCode}}", name],
            check=False,
        )
        try:
            exit_code = int((inspect.stdout or "").strip())
        except ValueError:
            exit_code = None
        self._docker(["stop", "-t", "5", name], check=False, timeout=30.0)
        self._docker(["rm", "-f", name], check=False, timeout=30.0)
        stream = self._log_streams.pop(runtime.id, None)
        if stream is not None:
            try:
                stream.terminate()
            except ProcessLookupError:
                pass
        if runtime.mcp_config_path:
            Path(runtime.mcp_config_path).unlink(missing_ok=True)
        return exit_code
