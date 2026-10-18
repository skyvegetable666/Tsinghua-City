"""Declarative specifications for the Genesis SDK.

The SDK is a thin control plane: it launches the existing supervisor + Docker
runtime and observes the run.  These dataclasses are deliberately data-only so
they can be serialized, compared, and later extended with per-agent prompt,
skill, harness, and model fields without changing the lifecycle API.

Current backend limit: exactly two agents.  That limit lives in the broker,
runtime, and compose file, not in the spec, so the spec is ready for N agents
once those layers are generalized.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MAX_AGENTS = 2
"""Backend limit for the current two-agent broker/compose implementation."""

_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_RESERVED_AGENT_NAMES = {"system", "supervisor", "host", "all", "none"}


@dataclass(frozen=True)
class AgentSpec:
    """One agent in the world.

    Only name is wired today.  Future versions will add per-agent prompt,
    skills, model, and harness fields; the SDK API should stay source
    compatible by adding optional fields here rather than changing World.
    """

    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise TypeError("agent name must be a string")
        name = self.name.strip()
        if not _AGENT_NAME_RE.match(name):
            raise ValueError(
                "agent name must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}: "
                f"{self.name!r}"
            )
        if name.lower() in _RESERVED_AGENT_NAMES:
            raise ValueError(f"agent name is reserved: {name!r}")
        object.__setattr__(self, "name", name)


@dataclass
class WorldSpec:
    """Everything needed to describe (not start) a world.

    mission/world_rules/genesis_template override the repository's config/*.md
    files when set.  None means "use the repository default", which keeps the
    no-code path and the SDK path consistent.
    """

    agents: list[AgentSpec] = field(default_factory=list)
    mission: str | None = None
    world_rules: str | None = None
    genesis_template: str | None = None
    model: str | None = None
    mock: bool = False
    no_build: bool = False
    broker_port: int | None = None
    max_seconds: float | None = None
    max_messages: int | None = None
    end_grace_seconds: int | None = None
    image: str | None = None
    project_root: Path | str | None = None
    startup_timeout: float = 900.0
    stop_timeout: float = 180.0
    log_path: Path | str | None = None

    def __post_init__(self) -> None:
        if self.broker_port is not None and not 0 <= int(self.broker_port) <= 65535:
            raise ValueError("broker_port must be between 0 and 65535")
        if self.max_seconds is not None and self.max_seconds < 0:
            raise ValueError("max_seconds must be >= 0")
        if self.max_messages is not None and self.max_messages < 0:
            raise ValueError("max_messages must be >= 0")
        if self.end_grace_seconds is not None and self.end_grace_seconds < 0:
            raise ValueError("end_grace_seconds must be >= 0")
        if self.startup_timeout <= 0:
            raise ValueError("startup_timeout must be > 0")
        if self.stop_timeout <= 0:
            raise ValueError("stop_timeout must be > 0")

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form, usable in a run manifest / notebook."""
        data = asdict(self)
        for key in ("project_root", "log_path"):
            if data.get(key) is not None:
                data[key] = str(data[key])
        return data


def coerce_agent(value: str | AgentSpec) -> AgentSpec:
    if isinstance(value, AgentSpec):
        return value
    if isinstance(value, str):
        return AgentSpec(value)
    raise TypeError(f"expected str or AgentSpec, got {type(value).__name__}")


def coerce_agents(values: Sequence[str | AgentSpec] | None) -> list[AgentSpec]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)):
        raise TypeError("agents must be a sequence, not a single string")
    return [coerce_agent(value) for value in values]
