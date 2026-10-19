"""Reference MCP harness for Tsinghua City.

The runtime manager starts this process with a generated CITY_MCP_CONFIG. The
harness connects to the MCP sidecar and runs a deterministic policy; replace
the policy with a model-driven loop for real experiments.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .harness_policy import run_tick

LIST_TOOLS = {"get_inbox", "list_posts", "list_tasks", "list_agents", "list_lives"}


def decode_tool_content(name: str, contents: list[Any]) -> Any:
    """FastMCP returns one TextContent per list item, not one JSON array."""
    if not contents:
        return [] if name in LIST_TOOLS else None
    values: list[Any] = []
    for content in contents:
        text = getattr(content, "text", "")
        try:
            values.append(json.loads(text))
        except (TypeError, json.JSONDecodeError):
            values.append(text)
    if name in LIST_TOOLS:
        return values
    return values[0] if len(values) == 1 else values


def _mcp_config(path: str) -> StdioServerParameters:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    server = payload["mcpServers"]["tsinghua-city"]
    return StdioServerParameters(
        command=server["command"],
        args=list(server.get("args", [])),
        env=dict(server.get("env", {})),
    )


async def _run(args: argparse.Namespace) -> int:
    config_path = os.environ.get("CITY_MCP_CONFIG")
    if not config_path:
        print("CITY_MCP_CONFIG is not set", file=sys.stderr)
        return 2
    params = _mcp_config(config_path)
    journal_dir = Path(args.journal_dir).expanduser().resolve()
    journal_dir.mkdir(parents=True, exist_ok=True)
    tick = 0
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        async def call(name: str, arguments: dict[str, Any]) -> Any:
            result = await session.call_tool(name, arguments)
            if result.isError:
                raise RuntimeError(f"MCP tool {name} failed")
            return decode_tool_content(name, list(result.content or []))

        life_id: str | None = None
        try:
            try:
                life = await call("start_life", {})
                if isinstance(life, dict):
                    life_id = life.get("id")
                if life_id:
                    await call(
                        "log_life_event",
                        {
                            "life_id": life_id,
                            "kind": "birth",
                            "body": "reference harness started",
                            "data": {"pid": os.getpid(), "ticks": args.ticks},
                        },
                    )
            except Exception as exc:
                print(
                    json.dumps(
                        {"life": "start_failed", "error": f"{type(exc).__name__}: {exc}"}
                    ),
                    flush=True,
                )

            while args.ticks == 0 or tick < args.ticks:
                started = time.time()
                try:
                    result = await run_tick(call, tick)
                except Exception as exc:
                    record = {
                        "tick": tick,
                        "ts": time.time(),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    print(json.dumps(record), flush=True)
                    if life_id:
                        try:
                            await call(
                                "log_life_event",
                                {
                                    "life_id": life_id,
                                    "kind": "error",
                                    "body": record["error"],
                                    "data": {"tick": tick},
                                },
                            )
                        except Exception:
                            pass
                    if args.ticks and tick + 1 >= args.ticks:
                        return 1
                    await asyncio.sleep(args.interval)
                    tick += 1
                    continue

                agent = result["agent"]
                journal_path = journal_dir / f"{agent}.jsonl"
                with journal_path.open("a", encoding="utf-8") as journal:
                    journal.write(json.dumps({"ts": time.time(), **result}) + "\n")
                if life_id:
                    try:
                        await call(
                            "log_life_event",
                            {
                                "life_id": life_id,
                                "kind": "tick",
                                "body": (
                                    f"tick {tick} · {len(result.get('actions', []))} actions"
                                ),
                                "data": {
                                    "agent": agent,
                                    "tick": tick,
                                    "actions": [
                                        action.get("tool")
                                        for action in result.get("actions", [])
                                    ],
                                    "elapsed": round(time.time() - started, 3),
                                },
                            },
                        )
                    except Exception:
                        pass
                print(
                    json.dumps(
                        {
                            "agent": agent,
                            "tick": tick,
                            "elapsed": round(time.time() - started, 3),
                            "actions": len(result["actions"]),
                        }
                    ),
                    flush=True,
                )
                tick += 1
                if args.ticks == 0 or tick < args.ticks:
                    await asyncio.sleep(args.interval)
        finally:
            if life_id:
                try:
                    await call(
                        "log_life_event",
                        {
                            "life_id": life_id,
                            "kind": "life-end",
                            "body": "reference harness exiting",
                        },
                    )
                except Exception:
                    pass
                try:
                    await call(
                        "end_life",
                        {"life_id": life_id, "reason": "harness_exit"},
                    )
                except Exception:
                    pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tsinghua-city-reference-harness")
    parser.add_argument("--ticks", type=int, default=0, help="0 means run forever")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between ticks")
    parser.add_argument(
        "--journal-dir",
        default=os.environ.get("CITY_HARNESS_JOURNAL_DIR", "runs/harness"),
        help="directory for per-agent JSONL journals",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
