"""Prompt files and genesis rendering."""

from __future__ import annotations

import string
from dataclasses import dataclass

from .settings import AgentSpec, Settings

GENESIS_KEYS = {"agent_name", "peer_name", "mission", "run_id", "genesis_time"}


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
    missing = GENESIS_KEYS - identifiers
    if missing:
        raise ValueError(f"{settings.genesis_path} is missing placeholders: {sorted(missing)}")
    return PromptBundle(world=world, mission=mission, genesis_template=template)


def render_genesis(
    bundle: PromptBundle,
    *,
    agent: AgentSpec,
    run_id: str,
    genesis_time: str,
) -> str:
    return string.Template(bundle.genesis_template).safe_substitute(
        agent_name=agent.name,
        peer_name=agent.peer_name,
        mission=bundle.mission,
        run_id=run_id,
        genesis_time=genesis_time,
    )
