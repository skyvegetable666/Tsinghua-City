"""Generate a docker compose file for the current agent roster.

The static docker-compose.yml describes the original two-agent baseline.  The
CLI writes this generated file into the run directory so N agents can share one
build image while each service gets its own network, name, peers, and token.
"""

from __future__ import annotations

import json
from pathlib import Path

from .settings import Settings


def _q(value: str) -> str:
    """A JSON string is a valid YAML double-quoted scalar."""
    return json.dumps(value, ensure_ascii=False)


def _common_env() -> list[tuple[str, str]]:
    return [
        ("RUN_ID", "${RUN_ID}"),
        ("BROKER_URL", "${BROKER_URL}"),
        ("AGENT_HARNESS", "${AGENT_HARNESS:-claude}"),
        ("AGENT_LOCAL_PORT", "8765"),
        ("AGENT_WORKDIR", "/work"),
        ("HOOK_BATCH_MAX_CHARS", "${HOOK_BATCH_MAX_CHARS:-8000}"),
        ("MAX_MESSAGE_CHARS", "${MAX_MESSAGE_CHARS:-4000}"),
        ("ANTHROPIC_BASE_URL", "${ANTHROPIC_BASE_URL:-https://api.deepseek.com/anthropic}"),
        ("ANTHROPIC_AUTH_TOKEN", "${ANTHROPIC_AUTH_TOKEN:-}"),
        ("ANTHROPIC_MODEL", "${ANTHROPIC_MODEL:-deepseek-flash[1m]}"),
        (
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "${ANTHROPIC_DEFAULT_OPUS_MODEL:-deepseek-flash[1m]}",
        ),
        (
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "${ANTHROPIC_DEFAULT_SONNET_MODEL:-deepseek-flash[1m]}",
        ),
        ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "${ANTHROPIC_DEFAULT_HAIKU_MODEL:-deepseek-flash}"),
        ("CLAUDE_CODE_SUBAGENT_MODEL", "${CLAUDE_CODE_SUBAGENT_MODEL:-deepseek-flash}"),
        ("CLAUDE_CODE_EFFORT_LEVEL", "${CLAUDE_CODE_EFFORT_LEVEL:-max}"),
        (
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
            "${CLAUDE_CODE_AUTO_COMPACT_WINDOW:-786432}",
        ),
        (
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
            "${CLAUDE_CODE_MAX_CONTEXT_TOKENS:-1000000}",
        ),
        ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1"),
        ("DISABLE_TELEMETRY", "1"),
        ("DISABLE_ERROR_REPORTING", "1"),
        ("NO_COLOR", "1"),
    ]


def render_compose(settings: Settings) -> str:
    root = settings.root.resolve()
    lines: list[str] = [
        'name: "genesis-lab"',
        "",
        "services:",
    ]
    for spec in settings.agents.values():
        peers = settings.peers_of(spec.agent_id)
        peer_ids = [peer.agent_id for peer in peers]
        peer_names = [peer.name for peer in peers]
        lines.extend(
            [
                f"  agent-{spec.agent_id}:",
                "    build:",
                f"      context: {_q(str(root))}",
                "      dockerfile: image/Dockerfile",
                f"    image: {_q(settings.agent_image)}",
                "    init: true",
                "    stop_grace_period: 30s",
                "    extra_hosts:",
                '      - "host.docker.internal:host-gateway"',
                "    environment:",
            ]
        )
        for key, value in _common_env():
            lines.append(f"      {key}: {_q(value)}")
        per_agent = [
            ("AGENT_ID", spec.agent_id),
            ("AGENT_NAME", spec.name),
            ("PEER_ID", peer_ids[0] if peer_ids else ""),
            ("PEER_NAME", peer_names[0] if peer_names else ""),
            ("PEER_IDS", json.dumps(peer_ids)),
            ("PEER_NAMES", json.dumps(peer_names)),
            ("AGENT_TOKEN", "${AGENT_TOKEN_" + spec.agent_id.upper() + "}"),
        ]
        for key, value in per_agent:
            lines.append(f"      {key}: {_q(value)}")
        lines.extend(
            [
                "    networks:",
                f"      - net-{spec.agent_id}",
                "",
            ]
        )
    lines.append("networks:")
    for spec in settings.agents.values():
        lines.append(f"  net-{spec.agent_id}:")
    lines.append("")
    return "\n".join(lines)


def write_compose(path: Path, settings: Settings) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_compose(settings), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path
