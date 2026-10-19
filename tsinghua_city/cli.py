"""Command line entry points: init, serve, create-world, create-agent, mcp."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

from sqlalchemy import select

from . import service
from .config import load_settings
from .db import make_engine, make_session_factory, upgrade_database
from .models import World
from .runtime import RuntimeManager
from .scheduler import tick_once
from .schemas import WorldConfig


def _database():
    settings = load_settings()
    upgrade_database(settings.database_url)
    engine = make_engine(settings.database_url)
    return settings, engine, make_session_factory(engine)


def cmd_init(_args: argparse.Namespace) -> int:
    settings, _engine, _factory = _database()
    print(f"database: {settings.database_url}")
    if settings.uses_default_admin_token:
        print("admin token: dev-admin-token (set CITY_ADMIN_TOKEN for a real run)")
    else:
        print("admin token: from CITY_ADMIN_TOKEN")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = load_settings()
    upgrade_database(settings.database_url)
    host = args.host or settings.host
    port = args.port or settings.port
    uvicorn.run(
        "tsinghua_city.api:app",
        host=host,
        port=port,
        log_level=settings.log_level,
    )
    return 0


def cmd_upgrade(_args: argparse.Namespace) -> int:
    settings = load_settings()
    upgrade_database(settings.database_url)
    print(f"database upgraded: {settings.database_url}")
    return 0


def cmd_tick(_args: argparse.Namespace) -> int:
    settings, _engine, factory = _database()
    manager = RuntimeManager(settings, factory)
    print(json.dumps(tick_once(factory, manager), indent=2, ensure_ascii=False))
    return 0


def cmd_create_world(args: argparse.Namespace) -> int:
    _settings, _engine, factory = _database()
    session = factory()
    try:
        world = service.create_world(session, args.name, WorldConfig())
        print(json.dumps(service.world_view(world), indent=2, ensure_ascii=False))
    finally:
        session.close()
    return 0


def cmd_create_agent(args: argparse.Namespace) -> int:
    _settings, _engine, factory = _database()
    session = factory()
    try:
        world = session.get(World, args.world)
        if world is None:
            world = session.scalars(select(World).where(World.name == args.world)).one_or_none()
        if world is None:
            raise SystemExit(f"world not found: {args.world}")
        agent, token = service.create_agent(
            session,
            world,
            args.name,
            initial_credits=args.credits,
            initial_life_seconds=args.life,
        )
        print(
            json.dumps(
                {
                    "id": agent.id,
                    "world_id": agent.world_id,
                    "name": agent.name,
                    "death_at": service.iso(agent.death_at),
                    "token": token,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    finally:
        session.close()
    return 0


def cmd_mcp(_args: argparse.Namespace) -> int:
    from .mcp_server import main as mcp_main

    mcp_main()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check whether this host can run the first product end to end."""
    settings = load_settings()
    checks: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    try:
        upgrade_database(settings.database_url)
        add("database", "ok", settings.database_url)
    except Exception as exc:  # doctor reports setup failures
        add("database", "fail", f"{type(exc).__name__}: {exc}")

    for label, raw in (
        ("runtime logs", settings.runtime_log_dir),
        ("traces", settings.trace_dir),
        ("manifests", settings.manifest_dir),
    ):
        path = Path(raw).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".doctor-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            add(label, "ok", str(path.resolve()))
        except OSError as exc:
            add(label, "fail", f"{raw}: {exc}")

    if settings.uses_default_admin_token:
        add("admin token", "warn", "using dev-admin-token; set CITY_ADMIN_TOKEN")
    else:
        add("admin token", "ok", "CITY_ADMIN_TOKEN is set")

    docker = shutil.which("docker")
    if docker is None:
        detail = "docker CLI not found"
        if "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower():
            detail += "; enable Docker Desktop WSL integration for this distro"
        add("docker CLI", "fail", detail)
    else:
        add("docker CLI", "ok", docker)
        try:
            result = subprocess.run(
                [docker, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if result.returncode == 0:
                add("docker daemon", "ok", result.stdout.strip())
                image = subprocess.run(
                    [docker, "image", "inspect", settings.docker_image],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                if image.returncode == 0:
                    add("agent image", "ok", settings.docker_image)
                else:
                    add("agent image", "fail", f"build {settings.docker_image} first")
            else:
                add("docker daemon", "fail", "docker info failed")
        except (OSError, subprocess.TimeoutExpired) as exc:
            add("docker daemon", "fail", str(exc))

    claude = os.environ.get("CITY_CLAUDE_BIN") or shutil.which("claude")
    if claude:
        add("claude CLI", "ok", claude)
    else:
        add("claude CLI", "fail", "install Claude Code or set CITY_CLAUDE_BIN")

    credential_names = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    if any(os.environ.get(name) for name in credential_names):
        add("model credentials", "ok", "ANTHROPIC_* environment is set")
    elif (Path.home() / ".claude" / "config.json").exists():
        add("model credentials", "warn", "only local Claude config found; containers need explicit env")
    else:
        add("model credentials", "warn", "set ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN")

    if args.json:
        print(json.dumps({"checks": checks}, indent=2, ensure_ascii=False))
    else:
        for check in checks:
            print(f"[{check['status']}] {check['name']}: {check['detail']}")
        failures = sum(1 for check in checks if check["status"] == "fail")
        print(f"\n{len(checks) - failures}/{len(checks)} checks passed")
    return 1 if any(check["status"] == "fail" for check in checks) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tsinghua-city")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="apply database migrations")
    init.set_defaults(func=cmd_init)

    upgrade = sub.add_parser("upgrade", help="apply database migrations")
    upgrade.set_defaults(func=cmd_upgrade)

    serve = sub.add_parser("serve", help="run the world API")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.set_defaults(func=cmd_serve)

    tick = sub.add_parser("tick", help="run one deadline/death maintenance pass")
    tick.set_defaults(func=cmd_tick)

    world = sub.add_parser("create-world", help="create a world")
    world.add_argument("name")
    world.set_defaults(func=cmd_create_world)

    agent = sub.add_parser("create-agent", help="create an agent; prints its token once")
    agent.add_argument("world", help="world id or name")
    agent.add_argument("name", help="agent name")
    agent.add_argument("--credits", type=int, default=None)
    agent.add_argument("--life", type=int, default=None, help="initial life in seconds")
    agent.set_defaults(func=cmd_create_agent)

    mcp = sub.add_parser("mcp", help="run the MCP server (stdio) for one agent")
    mcp.set_defaults(func=cmd_mcp)

    doctor = sub.add_parser("doctor", help="check host prerequisites for real runs")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
