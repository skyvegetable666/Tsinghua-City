"""Command line entry points: init, serve, create-world, create-agent, mcp."""

from __future__ import annotations

import argparse
import json

from sqlalchemy import select

from . import service
from .config import load_settings
from .db import make_engine, make_session_factory, upgrade_database
from .models import World
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
    _settings, _engine, factory = _database()
    print(json.dumps(tick_once(factory), indent=2, ensure_ascii=False))
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
