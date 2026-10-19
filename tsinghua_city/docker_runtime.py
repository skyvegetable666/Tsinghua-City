"""Thin Docker CLI adapter; world state and restart policy stay in RuntimeManager."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .config import Settings
from .models import AgentRuntime
from .runtime import StartResult
from .service import DomainError

LABEL = "org.tsinghua-city"
PROVIDER_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
)


class DockerRuntimeAdapter:
    name = "docker"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.log_dir = Path(settings.runtime_log_dir).expanduser().resolve()
        self.trace_dir = Path(settings.trace_dir).expanduser().resolve()
        if settings.docker_cpus <= 0 or settings.docker_pids_limit <= 0:
            raise ValueError("Docker CPU and PID limits must be positive")
        try:
            self.volume_options = json.loads(settings.docker_volume_options)
        except ValueError:
            raise ValueError("CITY_DOCKER_VOLUME_OPTIONS must be a JSON object") from None
        if not isinstance(self.volume_options, dict) or any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in self.volume_options.items()
        ):
            raise ValueError("Docker volume options must map strings to strings")
        if settings.docker_storage_quota_required and not (
            settings.docker_volume_driver == "local"
            and self.volume_options.get("type") == "tmpfs"
            and self.volume_options.get("device") == "tmpfs"
            and re.search(r"(?:^|,)size=[1-9][0-9]*[kKmMgG]?(?:,|$)",
                          self.volume_options.get("o", ""))
        ):
            raise DomainError("required storage quota needs local tmpfs volume options with size; "
                              "ordinary named volumes have no enforced size limit", 503)

    @staticmethod
    def container_name(runtime_id: str) -> str:
        if not re.fullmatch(r"rt_[a-f0-9]{24}", runtime_id):
            raise ValueError("invalid runtime id")
        return "city-" + runtime_id

    def _run(self, args: list[str], *, timeout: int = 30, missing_ok=False) -> str | None:
        try:
            result = subprocess.run(
                ["docker", *args], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT if args[0] == "logs" else subprocess.PIPE,
                text=True, timeout=timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DomainError("Docker CLI unavailable or timed out", 503) from exc
        if result.returncode:
            if missing_ok and any(
                marker in (result.stderr or "").lower()
                for marker in (
                    "no such container", "no such network", "no such volume",
                    "no such file", "could not find the file",
                    f"network {args[-1]} not found",
                )
            ):
                return None
            # Commands never contain credentials; do not echo daemon output or environment.
            raise DomainError(f"Docker {args[0]} failed; check daemon/image configuration", 503)
        return result.stdout.strip()

    def _inspect(self, kind: str, name: str) -> dict | None:
        templates = {
            "container": '{"labels":{{json .Config.Labels}},"state":{{json .State}}}',
            "volume": '{"labels":{{json .Labels}},"driver":{{json .Driver}},"options":{{json .Options}}}',
            "network": '{"labels":{{json .Labels}},"internal":{{json .Internal}}}',
        }
        template = templates[kind]
        value = self._run([kind, "inspect", "--format", template, name], missing_ok=True)
        return json.loads(value) if value is not None else None

    def _owned(self, kind: str, name: str, identity: str) -> dict | None:
        info = self._inspect(kind, name)
        if info is not None and (info["labels"] or {}).get(LABEL + ".owner") != identity:
            raise DomainError("refusing to manage a foreign Docker resource", 409)
        return info

    def _export_logs(self, name: str, path: Path, runtime: AgentRuntime) -> None:
        logs = self._run(["logs", name])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(logs or "", encoding="utf-8")
        path.chmod(0o600)
        records = []
        for line in (logs or "").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and isinstance(event.get("type"), str):
                records.append(json.dumps({
                    **event, "world_id": runtime.world_id, "agent_id": runtime.agent_id,
                    "runtime_id": runtime.id,
                }, ensure_ascii=False))
        if records:
            root = Path(self.settings.trace_dir).expanduser().resolve()
            root.mkdir(parents=True, exist_ok=True)
            # Polling must never expose a partially rewritten transcript to the viewer.
            descriptor, temporary = tempfile.mkstemp(dir=root)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    output.write("\n".join(records) + "\n")
                os.replace(temporary, root / (path.stem + ".jsonl"))
            finally:
                Path(temporary).unlink(missing_ok=True)

    def _export_traces(self, name: str) -> None:
        """Copy the container's agent trace directory into the host trace dir.

        Trace export is best-effort: a missing directory must not make stopping
        a runtime fail. The container is still removed afterwards.
        """
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.trace_dir.chmod(0o700)
        except OSError:
            pass
        try:
            self._run(
                ["cp", f"{name}:/work/transcripts/.", str(self.trace_dir)],
                timeout=60,
                missing_ok=True,
            )
        except DomainError:
            return
        for path in self.trace_dir.glob("*.jsonl"):
            try:
                path.chmod(0o600)
            except OSError:
                pass

    def _volume(self, name, owner):
        info = self._owned("volume", name, owner)
        if info is not None:
            if (info["driver"] != self.settings.docker_volume_driver
                    or (info["options"] or {}) != self.volume_options):
                raise DomainError("existing workspace volume has a different storage policy", 409)
            return
        args = ["volume", "create", "--driver", self.settings.docker_volume_driver,
                "--label", LABEL + ".owner=" + owner]
        for key, value in sorted(self.volume_options.items()):
            args.extend(["--opt", key + "=" + value])
        self._run([*args, name])

    def start(self, *, runtime_id, command, agent_id, world_id, token,
              provider_env=None) -> StartResult:
        if not command or any(not isinstance(part, str) or not part for part in command):
            raise ValueError("runtime command must be a non-empty argv list")
        name = self.container_name(runtime_id)
        owner = world_id + ":" + agent_id
        volume = "city-work-" + agent_id
        public_volume = f"{self.settings.public_workspace_volume}-{world_id}"
        public_owner = "public:" + world_id
        env = dict(provider_env) if provider_env is not None else {
            key: os.environ[key] for key in PROVIDER_ENV if os.environ.get(key)
        }
        if self.settings.docker_egress_proxy:
            env.update({"HTTP_PROXY": self.settings.docker_egress_proxy,
                        "HTTPS_PROXY": self.settings.docker_egress_proxy,
                        "NO_PROXY": urlsplit(self.settings.docker_api_url).hostname or ""})
        env.update({
            "CITY_API_URL": self.settings.docker_api_url,
            "CITY_AGENT_TOKEN": token, "CITY_AGENT_ID": agent_id, "CITY_WORLD_ID": world_id,
        })
        if any("\n" in value or "\r" in value for value in env.values()):
            raise ValueError("multiline environment values are not supported")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"{runtime_id}-{uuid.uuid4().hex[:12]}.log"
        if self._owned("container", name, runtime_id) is not None:
            raise DomainError("runtime container already exists", 409)
        self._volume(volume, owner)
        self._volume(public_volume, public_owner)
        network = self._owned("network", name, runtime_id)
        if network is None:
            self._run(["network", "create", *(["--internal"] if self.settings.docker_network_internal else []),
                       "--label", LABEL + ".owner=" + runtime_id, name])
        elif network["internal"] != self.settings.docker_network_internal:
            raise DomainError("existing runtime network has a different egress policy", 409)
        created = False
        try:
            # A private temporary env file keeps credentials out of process argv.
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as env_file:
                env_file.write("".join(f"{key}={value}\n" for key, value in env.items()))
                env_file.flush()
                self._run([
                    "create", "--pull", "never", "--name", name,
                    "--label", LABEL + ".owner=" + runtime_id,
                    "--init", "--user", "1000:1000", "--read-only", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges:true", "--pids-limit",
                    str(self.settings.docker_pids_limit), "--memory", self.settings.docker_memory,
                    "--memory-swap", self.settings.docker_memory, "--cpus",
                    str(self.settings.docker_cpus), "--network", name,
                    "--add-host", "host.docker.internal:host-gateway",
                    "--mount", f"type=volume,src={volume},dst=/work",
                    "--mount", f"type=volume,src={public_volume},dst=/shared",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
                    "--log-driver", "json-file", "--log-opt", "max-size=10m",
                    "--log-opt", "max-file=3", "--env-file", env_file.name,
                    self.settings.docker_image, *command,
                ])
                created = True
            self._run(["start", name])
        except Exception:
            if created:
                self._run(["rm", "--force", name], missing_ok=True)
            self._run(["network", "rm", name], missing_ok=True)
            raise
        return StartResult(None, None, str(log_path), None)

    def is_running(self, runtime: AgentRuntime) -> bool:
        name = self.container_name(runtime.id)
        info = self._owned("container", name, runtime.id)
        if info is None:
            return False
        if runtime.log_path:
            self._export_logs(name, Path(runtime.log_path), runtime)
        return bool(info["state"]["Running"])

    def stop(self, runtime: AgentRuntime) -> int | None:
        name = self.container_name(runtime.id)
        info = self._owned("container", name, runtime.id)
        exit_code = None
        if info is not None:
            if info["state"]["Running"]:
                self._run(["stop", "--time", "5", name])
                info = self._owned("container", name, runtime.id)
            if runtime.log_path:
                self._export_logs(name, Path(runtime.log_path), runtime)
            self._export_traces(name)
            exit_code = info["state"]["ExitCode"]
            self._run(["rm", name])
        if self._owned("network", name, runtime.id) is not None:
            self._run(["network", "rm", name])
        return exit_code
