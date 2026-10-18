"""Host-side configuration: .env loading, agent identities, run context."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path, *, override: bool = False) -> None:
    """Minimal .env loader. Existing environment variables win by default."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if raw.strip() == "":
        return default
    return int(raw)


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if raw.strip() == "":
        return default
    return float(raw)


def agent_token(run_token: str, agent_id: str) -> str:
    """Derive one container's broker token from the run token.

    The host holds the run token; each agent gets only its own derived token.
    That stops one agent process from impersonating the other on the HTTP API.
    """
    return hmac.new(
        run_token.encode("utf-8"), f"agent:{agent_id}".encode(), hashlib.sha256
    ).hexdigest()


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    name: str
    peer_id: str
    peer_name: str


@dataclass
class Settings:
    root: Path
    broker_host: str
    broker_port: int
    broker_token: str
    agents: dict[str, AgentSpec]
    max_message_chars: int
    hook_batch_max_chars: int
    run_max_messages: int
    run_max_seconds: float
    end_grace_seconds: float
    agent_image: str
    mock_llm_port: int
    world_path: Path
    mission_path: Path
    genesis_path: Path
    # Optional override for the broker URL as seen from inside the containers.
    # Empty means http://host.docker.internal:<broker_port>, which works on
    # native Linux (via compose extra_hosts) and Docker Desktop.
    broker_url: str = ""
    anthropic_env: dict[str, str] = field(default_factory=dict)

    def agent_by_name(self, name: str) -> AgentSpec | None:
        for spec in self.agents.values():
            if spec.name == name:
                return spec
        return None

    def peer(self, agent_id: str) -> AgentSpec:
        return self.agents[self.agents[agent_id].peer_id]

    @property
    def agent_ids(self) -> tuple[str, str]:
        return ("a", "b")


def load_settings(root: Path | None = None, *, env_file: Path | None = None) -> Settings:
    root = (root or PROJECT_ROOT).resolve()
    load_dotenv(env_file or root / ".env")

    name_a = os.environ.get("AGENT_A_NAME", "agent-a").strip()
    name_b = os.environ.get("AGENT_B_NAME", "agent-b").strip()
    if not name_a or not name_b or name_a == name_b:
        raise ValueError("AGENT_A_NAME and AGENT_B_NAME must be set and distinct")
    if "supervisor" in (name_a, name_b):
        raise ValueError("agent names must not be 'supervisor'")

    agents = {
        "a": AgentSpec(agent_id="a", name=name_a, peer_id="b", peer_name=name_b),
        "b": AgentSpec(agent_id="b", name=name_b, peer_id="a", peer_name=name_a),
    }

    keys = [
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
        "CLAUDE_CODE_EFFORT_LEVEL",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
    ]
    anthropic_env = {key: os.environ[key] for key in keys if os.environ.get(key)}

    world_path = root / os.environ.get("WORLD_FILE", "config/world.md")
    mission_path = root / os.environ.get("MISSION_FILE", "config/mission.md")
    genesis_path = root / os.environ.get("GENESIS_FILE", "config/genesis.md")
    for path in (world_path, mission_path, genesis_path):
        if not path.exists():
            raise FileNotFoundError(f"prompt file not found: {path}")

    return Settings(
        root=root,
        broker_host=os.environ.get("BROKER_HOST", "0.0.0.0"),
        broker_port=_int("BROKER_PORT", 8787),
        broker_token=os.environ.get("BROKER_TOKEN", "").strip(),
        agents=agents,
        max_message_chars=_int("MAX_MESSAGE_CHARS", 4000),
        hook_batch_max_chars=_int("HOOK_BATCH_MAX_CHARS", 8000),
        run_max_messages=_int("RUN_MAX_MESSAGES", 0),
        run_max_seconds=_float("RUN_MAX_SECONDS", 0.0),
        end_grace_seconds=_float("END_GRACE_SECONDS", 45.0),
        agent_image=os.environ.get("AGENT_IMAGE", "genesis-lab-agent:latest"),
        mock_llm_port=_int("MOCK_LLM_PORT", 8790),
        world_path=world_path,
        mission_path=mission_path,
        genesis_path=genesis_path,
        broker_url=os.environ.get("BROKER_URL", "").strip(),
        anthropic_env=anthropic_env,
    )


def validate_for_real_run(settings: Settings) -> list[str]:
    """Return human-readable problems that make a real (non-mock) run impossible."""
    problems: list[str] = []
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    if not token or token.startswith("sk-REPLACE"):
        problems.append("ANTHROPIC_AUTH_TOKEN is not set (copy .env.example to .env and fill it in)")
    if not os.environ.get("ANTHROPIC_BASE_URL"):
        problems.append("ANTHROPIC_BASE_URL is not set")
    if settings.max_message_chars < 1 or settings.hook_batch_max_chars < 1:
        problems.append("MAX_MESSAGE_CHARS and HOOK_BATCH_MAX_CHARS must be positive")
    if settings.hook_batch_max_chars > 9500:
        problems.append("HOOK_BATCH_MAX_CHARS must stay below Claude Code's 10000 char hook cap")
    if settings.max_message_chars > settings.hook_batch_max_chars:
        problems.append("MAX_MESSAGE_CHARS must not exceed HOOK_BATCH_MAX_CHARS")
    return problems


@dataclass
class RunContext:
    run_id: str
    run_dir: Path
    token: str
    started_at: float

    @property
    def started_iso(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.started_at))


def make_run(settings: Settings) -> RunContext:
    run_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + secrets.token_hex(2)
    run_dir = settings.root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    token = settings.broker_token or secrets.token_urlsafe(24)
    return RunContext(run_id=run_id, run_dir=run_dir, token=token, started_at=time.time())


def write_json(path: Path, data: dict, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    try:
        tmp.chmod(mode)
    except OSError:
        pass
    tmp.replace(path)


def write_current(root: Path, run: RunContext, port: int) -> None:
    write_json(
        root / "runs" / "current.json",
        {
            "run_id": run.run_id,
            "run_dir": str(run.run_dir),
            "port": port,
            "token": run.token,
            "pid": os.getpid(),
            "started_at": run.started_iso,
        },
    )


def read_current(root: Path | None = None) -> dict | None:
    root = (root or PROJECT_ROOT).resolve()
    path = root / "runs" / "current.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def redact(value: str) -> str:
    if not value:
        return value
    if len(value) <= 8:
        return "***"
    return value[:4] + "..." + value[-4:]


def redacted_env(settings: Settings) -> dict[str, str]:
    out = dict(settings.anthropic_env)
    for key in list(out):
        if "TOKEN" in key or "KEY" in key:
            out[key] = redact(out[key])
    return out
