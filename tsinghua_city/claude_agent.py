"""Run one Claude Code agent against the Tsinghua City MCP server.

This is the first real-agent entrypoint. It is deliberately thin: Claude Code
owns the model loop, tool execution, and session behavior. This module only
supplies the prompt, MCP config, process lifecycle, and JSONL trace.

It does not implement memory, wake, rebirth, or a cognitive lifecycle.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .claude_harness import ClaudeHarness
from .inbox_delivery import WorldInbox, format_delivery


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return slug[:48] or "agent"


class JsonlTrace:
    def __init__(self, path: Path, *, to_stdout: bool = False, metadata: dict | None = None) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a", encoding="utf-8")
        path.chmod(0o600)
        self.to_stdout = to_stdout
        self.metadata = metadata or {}

    def __call__(self, event: dict[str, Any]) -> None:
        record = {"ts": time.time(), **self.metadata, **event}
        line = json.dumps(record, ensure_ascii=False, default=str)
        self._file.write(line + "\n")
        self._file.flush()
        if self.to_stdout:
            print(line, flush=True)

    def close(self) -> None:
        self._file.close()


def build_prompt(agent_name: str, agent_id: str, world_id: str) -> str:
    return (
        f"You are {agent_name}, an autonomous agent in Tsinghua City.\n"
        f"World: {world_id}\nAgent id: {agent_id}\n\n"
        "You interact with the world only through the MCP tools supplied to you. "
        "Use them to inspect your state, check your credit balance and death deadline, "
        "read the public board, send messages, transfer credits, extend your life, "
        "publish tasks, and claim or submit work. You may also read operator messages "
        "with get_operator_messages and reply with talk_to_operator if you choose.\n\n"
        "You are alive until the server's death deadline. Death is terminal: there is "
        "no rebirth, wake, or automatic continuation after death. You may keep notes "
        "in a private workspace or on the public board if you choose, and you may also "
        "choose not to write anything. The platform does not require notes, diaries, "
        "summaries, or any particular memory strategy.\n\n"
        "If the environment exposes /work and /shared, /work is your private "
        "workspace and /shared is a public read/write workspace. You may use either, "
        "or neither. /shared is shared mutable state: use an explicit file lock "
        "(for example flock) when concurrent writes would conflict.\n\n"
        "Act autonomously on your own goals. Do not wait for a scripted operator. "
        "Use the available tools to observe the consequences of your actions."
    )


def _read_prompt(args: argparse.Namespace, name: str, agent_id: str, world_id: str) -> str:
    if args.prompt_file:
        return Path(args.prompt_file).read_text(encoding="utf-8")
    if args.prompt:
        return args.prompt
    return build_prompt(name, agent_id, world_id)


def build_launch_command(
    adapter: str,
    *,
    prompt: str | None = None,
    model: str | None = None,
    max_turns: int = 30,
) -> list[str]:
    """Build the argv used by the admin launch endpoint.

    Docker already has the package in /opt/venv, so it uses the image python;
    the local adapter must use the server interpreter.
    """
    if adapter not in {"local", "docker"}:
        raise ValueError("adapter must be local or docker")
    python = sys.executable if adapter == "local" else "python"
    command = [python, "-m", "tsinghua_city.claude_agent", "--max-turns", str(max_turns)]
    if prompt:
        command.extend(["--prompt", prompt])
    if model:
        command.extend(["--model", model])
    return command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tsinghua-city-claude-agent")
    parser.add_argument("--mcp-config", default=os.environ.get("CITY_MCP_CONFIG"))
    parser.add_argument("--model", default=os.environ.get("CITY_MODEL") or os.environ.get("ANTHROPIC_MODEL"))
    parser.add_argument("--agent-name", default=os.environ.get("CITY_AGENT_NAME"))
    parser.add_argument("--agent-id", default=os.environ.get("CITY_AGENT_ID"))
    parser.add_argument("--world-id", default=os.environ.get("CITY_WORLD_ID"))
    parser.add_argument("--run-id", default=os.environ.get("CITY_RUN_ID"))
    parser.add_argument("--experiment-id", default=None)
    parser.add_argument("--workdir", default=os.environ.get("CITY_AGENT_WORKDIR", "runs/agent-work"))
    parser.add_argument("--trace-dir", default=os.environ.get("CITY_TRACE_DIR", "runs/transcripts"))
    parser.add_argument("--claude-bin", default=os.environ.get("CITY_CLAUDE_BIN", "claude"))
    parser.add_argument("--prompt", default=None)
    parser.add_argument("--prompt-file", default=None)
    parser.add_argument("--world-prompt", default=os.environ.get("CITY_WORLD_PROMPT", ""))
    parser.add_argument("--ready-timeout", type=float, default=180.0)
    parser.add_argument("--turn-timeout", type=float, default=900.0)
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--max-budget-usd", type=float, default=None)
    parser.add_argument("--max-rounds", type=int, default=1)
    parser.add_argument("--listen-seconds", type=float, default=60.0)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--run-timeout", type=float, default=900.0)
    parser.add_argument("--resume", help="Resume an existing Claude session ID")
    parser.add_argument(
        "--trace-stdout", action="store_true", default=os.environ.get("CITY_TRACE_STDOUT") == "1"
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    if not args.mcp_config:
        print("CITY_MCP_CONFIG or --mcp-config is required", file=sys.stderr)
        return 2
    mcp_config = Path(args.mcp_config).expanduser().resolve()
    if not mcp_config.is_file():
        print(f"MCP config not found: {mcp_config}", file=sys.stderr)
        return 2
    agent_id = args.agent_id or "unknown-agent"
    agent_name = args.agent_name or agent_id
    world_id = args.world_id or "unknown-world"
    run_id = args.run_id or uuid.uuid4().hex[:12]
    workdir = Path(args.workdir).expanduser().resolve()
    trace_path = Path(args.trace_dir).expanduser().resolve() / (
        f"{_slug(agent_name)}-{_slug(run_id)}.jsonl"
    )
    limits = (args.turn_timeout, args.ready_timeout, args.run_timeout, args.poll_interval)
    if args.max_turns <= 0 or args.max_rounds <= 0 or any(
        not math.isfinite(value) or value <= 0 for value in limits
    ):
        raise ValueError("turn and timeout limits must be positive")
    if not math.isfinite(args.listen_seconds) or args.listen_seconds < 0:
        raise ValueError("listen-seconds must be finite and nonnegative")
    if args.max_budget_usd is not None and (
        not math.isfinite(args.max_budget_usd) or args.max_budget_usd <= 0
    ):
        raise ValueError("max-budget-usd must be finite and positive")
    if args.max_rounds > 1 and args.max_budget_usd is None:
        raise ValueError("multi-round runs require --max-budget-usd")
    inbox = WorldInbox(mcp_config) if args.max_rounds > 1 else None
    trace = JsonlTrace(trace_path, to_stdout=args.trace_stdout, metadata={
        "world_id": world_id, "agent_id": agent_id, "agent_name": agent_name,
        "run_id": run_id, "experiment_id": args.experiment_id,
    })
    prompt = _read_prompt(args, agent_name, agent_id, world_id)
    harness = ClaudeHarness(
        run_id=run_id,
        agent_id=agent_id,
        agent_name=agent_name,
        model=args.model or "",
        world_prompt=args.world_prompt,
        workdir=workdir,
        mcp_config=mcp_config,
        trace=trace,
        claude_bin=args.claude_bin,
        max_turns=args.max_turns,
        resume_session=args.resume,
        max_budget_usd=args.max_budget_usd,
    )
    trace(
        {
            "type": "run_started",
            "agent_name": agent_name,
            "agent_id": agent_id,
            "world_id": world_id,
            "run_id": run_id,
            "model": args.model or "",
            "mcp_config": str(mcp_config),
            "workdir": str(workdir),
            "prompt_chars": len(prompt),
            "limits": {key: getattr(args, key) for key in (
                "max_turns", "max_rounds", "max_budget_usd", "run_timeout", "listen_seconds"
            )},
        }
    )
    print(f"trace: {trace_path}", flush=True)
    status = 1
    limit_reason = None
    try:
        deadline = time.monotonic() + args.run_timeout
        try:
            async with asyncio.timeout(args.run_timeout):
                await harness.start()
                # stream-json emits its init frame only after the first input.
                await harness.send_initial(prompt)
                await harness.wait_ready(args.ready_timeout)
                delivered = []
                for round_index in range(args.max_rounds):
                    await harness.wait_for_turn(args.turn_timeout)
                    result = harness.last_result
                    if result and result.get("subtype") in {
                        "error_max_budget_usd", "error_max_turns",
                    }:
                        limit_reason = (
                            "budget" if result["subtype"] == "error_max_budget_usd" else "turns"
                        )
                        trace({"type": "run_limit", "reason": limit_reason})
                        break
                    if result is None or result["is_error"]:
                        raise RuntimeError("Claude returned an error result; inspect the trace")
                    if delivered:
                        await inbox.acknowledge(delivered)
                    if round_index + 1 >= args.max_rounds:
                        break
                    if (args.max_budget_usd and result["cost_usd"] is not None
                            and result["cost_usd"] >= args.max_budget_usd):
                        limit_reason = "budget"
                        trace({"type": "run_limit", "reason": "budget"})
                        break
                    delivered = await inbox.wait(
                        timeout=args.listen_seconds, interval=args.poll_interval
                    )
                    if not delivered:
                        break
                    await harness.send_user(format_delivery(delivered))
                    trace({"type": "inbox_delivered", "message_ids": [
                        message["id"] for message in delivered
                    ]})
        except TimeoutError:
            if time.monotonic() < deadline:
                raise
            limit_reason = "duration"
            trace({"type": "run_limit", "reason": "duration"})
        await harness.stop(grace=8.0)
        if harness.exit_code not in (None, 0) and limit_reason is None:
            raise RuntimeError(f"Claude exited with code {harness.exit_code}")
        status = 0
    except asyncio.CancelledError:
        await harness.stop(grace=1.0)
        raise
    except Exception as exc:
        trace({"type": "run_error", "error": f"{type(exc).__name__}: {exc}"})
        print(f"claude agent failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        if harness.stderr_tail():
            print(harness.stderr_tail()[-2000:], file=sys.stderr)
        await harness.stop(grace=3.0)
    finally:
        trace({"type": "run_finished", "status": status, "exit_code": harness.exit_code})
        trace.close()
    return status


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
