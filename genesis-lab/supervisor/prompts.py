"""Prompt files and genesis rendering (N agents)."""

from __future__ import annotations

import string
from collections.abc import Iterable
from dataclasses import dataclass

from .settings import AgentSpec, Settings

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


@dataclass(frozen=True)
class PromptBundle:
    world: str
    mission: str
    genesis_template: str


def load_prompts(settings: Settings) -> PromptBundle:
    world = settings.world_path.read_text(encoding="utf-8").strip() + "\n"
    mission = settings.mission_path.read_text(encoding="utf-8").strip()
    template = settings.genesis_path.read_text(encoding="utf-8")

    identifiers = set(string.Template(template).get_identifiers())
    unknown = identifiers - GENESIS_KEYS
    if unknown:
        raise ValueError(f"{settings.genesis_path} uses unknown placeholders: {sorted(unknown)}")
    missing = GENESIS_REQUIRED - identifiers
    if not ({"peers", "peer_name"} & identifiers):
        missing.add("peers or peer_name")
    if missing:
        raise ValueError(f"{settings.genesis_path} is missing placeholders: {sorted(missing)}")
    return PromptBundle(world=world, mission=mission, genesis_template=template)


def _peer_names(agent: AgentSpec, peers: Iterable[AgentSpec] | None) -> list[str]:
    if peers is not None:
        return [peer.name for peer in peers]
    if agent.peer_name:
        return [agent.peer_name]
    return []


def render_genesis(
    bundle: PromptBundle,
    *,
    agent: AgentSpec,
    run_id: str,
    genesis_time: str,
    peers: Iterable[AgentSpec] | None = None,
    agent_count: int | None = None,
) -> str:
    peer_names = _peer_names(agent, peers)
    peer_text = ", ".join(peer_names)
    total = agent_count if agent_count is not None else len(peer_names) + 1
    return string.Template(bundle.genesis_template).safe_substitute(
        agent_name=agent.name,
        peer_name=peer_text,
        peers=peer_text,
        agent_count=str(total),
        mission=bundle.mission,
        run_id=run_id,
        genesis_time=genesis_time,
    )
