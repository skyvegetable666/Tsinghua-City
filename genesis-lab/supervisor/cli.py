"""Command line interface for the host supervisor."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import signal
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import dockerctl
from .broker import Broker
from .compose import write_compose
from .mock_llm import MockLLM
from .prompts import load_prompts
from .report import build_report
from .settings import (
    PROJECT_ROOT,
    agent_token,
    load_settings,
    make_run,
    read_current,
    redacted_env,
    validate_for_real_run,
    write_current,
)
from .store import RunStore


# --------------------------------------------------------------------- utils
def _post(port: int, token: str, path: str, body: dict, timeout: float = 5.0) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise SystemExit(f"supervisor rejected the request ({exc.code}): {detail}") from exc
    except Exception as exc:
        raise SystemExit(f"cannot reach the running supervisor: {exc}") from exc


def _get(port: int, token: str, path: str, timeout: float = 5.0) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"Authorization": "Bearer " + token},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"supervisor rejected the request ({exc.code})") from exc
    except Exception as exc:
        raise SystemExit(f"cannot reach the running supervisor: {exc}") from exc


def _current_or_die(root: Path) -> dict:
    current = read_current(root)
    if not current:
        raise SystemExit("no run found; start one with: python -m supervisor up")
    return current


def _latest_run_dir(root: Path) -> Path:
    current = read_current(root)
    if current and Path(current["run_dir"]).exists():
        return Path(current["run_dir"])
    runs = sorted((root / "runs").glob("20*"), key=lambda path: path.stat().st_mtime, reverse=True)
    runs = [path for path in runs if path.is_dir()]
    if not runs:
        raise SystemExit("no runs found")
    return runs[0]


def _compose_file_for_down(root: Path) -> Path | None:
    current = read_current(root)
    if current and current.get("compose_file"):
        path = Path(current["compose_file"])
        if path.exists():
            return path
    runs = sorted(
        (root / "runs").glob("20*"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for run_dir in runs:
        candidate = run_dir / "compose.yml"
        if candidate.is_file():
            return candidate
    return None


def _container_host_base(settings) -> str:
    """Scheme + host that containers use to reach host services."""
    if settings.broker_url:
        parsed = urllib.parse.urlsplit(settings.broker_url)
        if parsed.scheme and parsed.hostname:
            host = parsed.hostname
            if ":" in host:
                host = f"[{host}]"
            return f"{parsed.scheme}://{host}"
    return "http://host.docker.internal"


def _broker_url_for_containers(settings) -> str:
    if settings.broker_url:
        return settings.broker_url
    return f"http://host.docker.internal:{settings.broker_port}"


def _compose_env(settings, run, mock: MockLLM | None) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "COMPOSE_PROJECT_NAME": "genesis-lab",
            "RUN_ID": run.run_id,
            "BROKER_TOKEN": run.token,
            "BROKER_PORT": str(settings.broker_port),
            "BROKER_URL": _broker_url_for_containers(settings),
            "AGENT_IMAGE": settings.agent_image,
            "HOOK_BATCH_MAX_CHARS": str(settings.hook_batch_max_chars),
            "MAX_MESSAGE_CHARS": str(settings.max_message_chars),
        }
    )
    for agent_id in settings.agents:
        env[f"AGENT_TOKEN_{agent_id.upper()}"] = agent_token(run.token, agent_id)
    if mock is not None:
        base = f"{_container_host_base(settings)}:{settings.mock_llm_port}"
        env.update(
            {
                "ANTHROPIC_BASE_URL": base,
                "ANTHROPIC_AUTH_TOKEN": "mock-key",
                "ANTHROPIC_MODEL": "mock-model[1m]",
                "ANTHROPIC_DEFAULT_OPUS_MODEL": "mock-model[1m]",
                "ANTHROPIC_DEFAULT_SONNET_MODEL": "mock-model[1m]",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": "mock-model",
                "CLAUDE_CODE_SUBAGENT_MODEL": "mock-model",
                "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000",
            }
        )
    return env


def _port_free(host: str, port: int) -> bool:
    family = socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
        return True
    except OSError:
        return False


# ------------------------------------------------------------------ commands
def cmd_init(args: argparse.Namespace) -> int:
    root = PROJECT_ROOT
    env_path = root / ".env"
    example = root / ".env.example"
    if env_path.exists():
        print(f".env already exists: {env_path}")
    else:
        shutil.copyfile(example, env_path)
        print(f"created {env_path}")
    (root / "runs").mkdir(exist_ok=True)
    print()
    print("Next steps:")
    print("  1. edit .env and set ANTHROPIC_AUTH_TOKEN (the DeepSeek key)")
    print("  2. dry run without a key: python -m supervisor up --mock")
    print("  3. real run:            python -m supervisor up")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    root = PROJECT_ROOT
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"[FAIL] configuration: {exc}")
        return 1
    failures: list[str] = []
    print(f"[INFO] platform: {platform.platform()}")
    print(f"[INFO] containers reach broker at: {_broker_url_for_containers(settings)}")
    print(f"[INFO] agents: {len(settings.agents)} (ids: {', '.join(settings.agent_ids)})")

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}", flush=True)
        if not ok:
            failures.append(name)

    check("python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0])
    check(".env exists", (root / ".env").exists(), str(root / ".env"))
    check(
        "BROKER_HOST reachable from containers",
        settings.broker_host not in {"127.0.0.1", "localhost", "::1"},
        settings.broker_host,
    )
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    if not token:
        token_detail = "missing (edit .env)"
    elif token.startswith("sk-REPLACE"):
        token_detail = "placeholder (edit .env)"
    else:
        token_detail = "set"
    if args.mock:
        check("ANTHROPIC_AUTH_TOKEN set", True, "skipped for --mock")
    else:
        check(
            "ANTHROPIC_AUTH_TOKEN set",
            bool(token) and not token.startswith("sk-REPLACE"),
            token_detail,
        )
    for label, path in (
        ("world prompt", settings.world_path),
        ("mission prompt", settings.mission_path),
        ("genesis template", settings.genesis_path),
    ):
        check(label, path.exists(), str(path))

    version = dockerctl.compose_version()
    check(
        "docker compose v2",
        version is not None,
        version or "install Docker Engine + compose plugin (Linux) or Docker Desktop (Windows)",
    )
    ok, detail = dockerctl.daemon_ok()
    if not ok and "permission denied" in detail.lower():
        detail += " (add your user to the docker group: sudo usermod -aG docker $USER)"
    check("docker daemon", ok, detail)

    current = read_current(root)
    if current:
        try:
            health = _get(int(current["port"]), current["token"], "/v1/status", timeout=2)
            check("current run reachable", True, f"{health.get('run_id')} ({health.get('phase')})")
        except SystemExit:
            check("current run reachable", False, "current.json exists but the broker is not answering")
    else:
        free = _port_free(settings.broker_host, settings.broker_port)
        check(f"broker port {settings.broker_port}", free, "free" if free else "already in use")
        if args.mock:
            mock_free = _port_free("0.0.0.0", settings.mock_llm_port)
            check(
                f"mock LLM port {settings.mock_llm_port}",
                mock_free,
                "free" if mock_free else "already in use",
            )

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    root = PROJECT_ROOT
    settings = load_settings()
    if not args.mock:
        problems = validate_for_real_run(settings)
        if problems:
            print("cannot start a real run:", file=sys.stderr)
            for problem in problems:
                print("  - " + problem, file=sys.stderr)
            print("\nTip: use --mock for a plumbing dry run without an API key.", file=sys.stderr)
            return 2
    if args.max_seconds is not None:
        settings.run_max_seconds = args.max_seconds
    if args.max_messages is not None:
        settings.run_max_messages = args.max_messages
    prompts = load_prompts(settings)
    run = make_run(settings)
    compose_path = write_compose(run.run_dir / "compose.yml", settings)
    store = RunStore(run.run_dir, run.run_id)
    store.write_json(
        "run.json",
        {
            "run_id": run.run_id,
            "started_at": run.started_iso,
            "mode": "mock" if args.mock else "real",
            "broker": {
                "host": settings.broker_host,
                "port": settings.broker_port,
                "container_url": _broker_url_for_containers(settings),
            },
            "agents": {
                spec.agent_id: {
                    "name": spec.name,
                    "peers": [peer.name for peer in settings.peers_of(spec.agent_id)],
                }
                for spec in settings.agents.values()
            },
            "compose_file": str(compose_path),
            "limits": {
                "max_message_chars": settings.max_message_chars,
                "hook_batch_max_chars": settings.hook_batch_max_chars,
                "max_messages": settings.run_max_messages,
                "max_seconds": settings.run_max_seconds,
                "end_grace_seconds": settings.end_grace_seconds,
            },
            "anthropic": redacted_env(settings),
            "world_prompt": prompts.world,
            "mission": prompts.mission,
            "genesis_template": prompts.genesis_template,
            "image": settings.agent_image,
        },
    )
    store.append(
        "run_started",
        mode="mock" if args.mock else "real",
        run_dir=str(run.run_dir),
        broker_port=settings.broker_port,
        broker_url=_broker_url_for_containers(settings),
        image=settings.agent_image,
        model="mock-model[1m]" if args.mock else os.environ.get("ANTHROPIC_MODEL", ""),
        limits={
            "max_messages": settings.run_max_messages,
            "max_seconds": settings.run_max_seconds,
        },
        agent_count=len(settings.agents),
        compose_file=str(compose_path),
    )

    env = _compose_env(settings, run, None)
    if not args.no_build:
        print("[supervisor] building the agent image (first run takes a few minutes)...", flush=True)
        try:
            dockerctl.build(root, env, compose_file=compose_path)
        except dockerctl.DockerError as exc:
            print(f"[supervisor] build failed:\n{exc}", file=sys.stderr)
            store.append("error", where="build", error=str(exc))
            store.close()
            return 1

    async def _run() -> int:
        broker = Broker(settings, run, prompts, store)
        mock: MockLLM | None = None
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        def request_stop(*_args) -> None:
            loop.call_soon_threadsafe(stop.set)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, request_stop)
            except (ValueError, OSError):
                pass

        started = False
        auto_end_task: asyncio.Task | None = None
        try:
            if args.mock:
                mock = MockLLM(
                    port=settings.mock_llm_port,
                    host="0.0.0.0",
                    mode="demo",
                    log_path=run.run_dir / "mock_llm_requests.jsonl",
                    roster={
                        spec.name: [peer.name for peer in settings.peers_of(spec.agent_id)]
                        for spec in settings.agents.values()
                    },
                )
                try:
                    mock.start()
                except OSError as exc:
                    print(
                        f"[supervisor] mock LLM could not bind port {settings.mock_llm_port}: {exc}",
                        file=sys.stderr,
                    )
                    return 1
                print(f"[supervisor] mock LLM listening on 0.0.0.0:{settings.mock_llm_port}", flush=True)
            try:
                await broker.start()
            except OSError as exc:
                print(
                    f"[supervisor] broker could not bind {settings.broker_host}:{settings.broker_port}: {exc}",
                    file=sys.stderr,
                )
                print("[supervisor] is another run already active? try: python -m supervisor down", file=sys.stderr)
                return 1
            started = True
            write_current(root, run, settings.broker_port, compose_file=compose_path)
            env_run = _compose_env(settings, run, mock)
            print(
                f"[supervisor] starting {len(settings.agents)} containers...",
                flush=True,
            )
            await asyncio.to_thread(dockerctl.up, root, env_run, compose_file=compose_path)
            print(flush=True)
            print(f"  run id:  {run.run_id}", flush=True)
            print(f"  run dir: {run.run_dir}", flush=True)
            print(f"  broker:  http://{settings.broker_host}:{settings.broker_port}", flush=True)
            print(f"  mode:    {'MOCK (no API calls)' if args.mock else 'REAL'}", flush=True)
            print("  Ctrl+C ends the run gracefully.", flush=True)
            print(flush=True)
            if settings.run_max_seconds and settings.run_max_seconds > 0:
                auto_end_task = asyncio.create_task(
                    _auto_end(broker, settings.run_max_seconds)
                )
            await _console_loop(broker, settings, stop)
            return 0
        finally:
            if auto_end_task:
                auto_end_task.cancel()
            print("[supervisor] stopping containers...", flush=True)
            try:
                await asyncio.to_thread(
                    dockerctl.down,
                    root,
                    _compose_env(settings, run, mock),
                    compose_file=compose_path,
                )
            except Exception as exc:
                print(f"[supervisor] warning: could not stop containers: {exc}", file=sys.stderr)
            if started:
                try:
                    final_reason = broker.end_reason or (
                        "completed" if broker.genesis_at else "startup failed"
                    )
                    await broker.finalize(final_reason)
                except Exception as exc:
                    print(f"[supervisor] warning: finalize failed: {exc}", file=sys.stderr)
                try:
                    await broker.stop()
                except Exception:
                    pass
            else:
                store.close()
            if mock is not None:
                mock.stop()
            current_path = root / "runs" / "current.json"
            current = read_current(root)
            if current and current.get("run_id") == run.run_id:
                # Drop the pointer: keeping it would make `doctor` treat the
                # finished run as an unreachable live broker.
                current_path.unlink(missing_ok=True)
            print(f"[supervisor] run directory: {run.run_dir}", flush=True)

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 130
    except dockerctl.DockerError as exc:
        print(f"[supervisor] docker error: {exc}", file=sys.stderr)
        return 1


async def _auto_end(broker: Broker, seconds: float) -> None:
    await asyncio.sleep(seconds)
    if broker.phase == "running":
        await broker.request_end(f"time limit ({seconds:.0f}s) reached")


async def _console_loop(broker: Broker, settings, stop: asyncio.Event) -> None:
    started = time.monotonic()
    end_deadline: float | None = None
    both_offline_since: float | None = None
    while not broker.ended_event.is_set():
        if stop.is_set():
            stop.clear()
            if broker.phase == "running":
                print("\n[supervisor] operator interrupted; ending the run...", flush=True)
                await broker.request_end("operator interrupted")
            else:
                print("\n[supervisor] stopping now", flush=True)
                break

        if broker.phase == "ending" and end_deadline is None:
            end_deadline = time.monotonic() + settings.end_grace_seconds + 150.0
        if end_deadline is not None and time.monotonic() > end_deadline:
            print("[supervisor] end deadline reached; stopping containers", flush=True)
            break

        if broker.phase == "waiting" and time.monotonic() - started > 180.0:
            print("[supervisor] genesis did not happen within 180s; ending the run", flush=True)
            await broker.request_end("genesis timeout")
            end_deadline = time.monotonic() + 30.0

        if broker.genesis_at and broker.phase == "running":
            if all(not channel.connected for channel in broker.channels.values()):
                if both_offline_since is None:
                    both_offline_since = time.monotonic()
                elif time.monotonic() - both_offline_since > 20.0:
                    print("[supervisor] all agents disconnected; ending the run", flush=True)
                    await broker.request_end("all agents disconnected")
            else:
                both_offline_since = None

        try:
            line = await asyncio.wait_for(broker.log_queue.get(), timeout=0.5)
            print("  " + line, flush=True)
        except TimeoutError:
            pass

        if (
            broker.phase == "ending"
            and broker.shutdown_sent_at is not None
            and all(not channel.connected for channel in broker.channels.values())
        ):
            await asyncio.sleep(1.5)
            break
    await asyncio.sleep(0.2)


def cmd_down(args: argparse.Namespace) -> int:
    root = PROJECT_ROOT
    current = read_current(root)
    if current:
        try:
            _post(int(current["port"]), current["token"], "/v1/end", {"reason": "operator interrupted (down)"}, timeout=2)
            print("asked the supervisor to end the run")
        except SystemExit:
            pass
    try:
        compose_file = _compose_file_for_down(root)
        dockerctl.down(root, dict(os.environ), check=False, compose_file=compose_file)
    except dockerctl.DockerError as exc:
        print(f"docker compose down failed: {exc}", file=sys.stderr)
        return 1
    print("containers stopped")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    current = _current_or_die(PROJECT_ROOT)
    status = _get(int(current["port"]), current["token"], "/v1/status")
    print(json.dumps(status, indent=2, ensure_ascii=False))
    return 0


def cmd_say(args: argparse.Namespace) -> int:
    current = _current_or_die(PROJECT_ROOT)
    text = " ".join(args.text).strip()
    if not text:
        raise SystemExit("empty message")
    result = _post(
        int(current["port"]),
        current["token"],
        "/v1/system/messages",
        {"to": args.to, "text": text},
    )
    print(f"injected system message(s): {result.get('message_ids')}")
    return 0


def cmd_end(args: argparse.Namespace) -> int:
    current = _current_or_die(PROJECT_ROOT)
    result = _post(
        int(current["port"]),
        current["token"],
        "/v1/end",
        {"reason": args.reason},
    )
    print(f"ending: {result}")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir) if args.run_dir else _latest_run_dir(PROJECT_ROOT)
    path = run_dir / "events.jsonl"
    if not path.exists():
        raise SystemExit(f"no events.jsonl in {run_dir}")
    offset = 0
    if args.follow:
        with path.open("r", encoding="utf-8") as handle:
            handle.seek(0, os.SEEK_END)
            offset = handle.tell()
    with path.open("r", encoding="utf-8") as handle:
        if not args.follow:
            lines = handle.readlines()
            for line in lines[-args.tail :]:
                _print_event(line)
            return 0
        while True:
            where = handle.tell()
            line = handle.readline()
            if line:
                _print_event(line)
                offset = handle.tell()
            else:
                handle.seek(where)
                time.sleep(0.5)
                if args.once and handle.tell() == offset:
                    return 0


def _print_event(line: str) -> None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return
    kind = event.get("event")
    stamp = str(event.get("ts_iso") or "")[11:19]
    if kind == "message_sent":
        message = event.get("message") or {}
        text = str(message.get("text") or "").replace("\n", " ")
        print(f"{stamp} {message.get('id')} {message.get('sender')} -> {message.get('recipient')}: {text[:120]}")
    elif kind == "message_delivered":
        print(
            f"{stamp} delivered {event.get('message_id')} via {event.get('channel')} "
            f"(+{event.get('latency_s')}s)"
        )
    elif kind == "agent_trace":
        trace = event.get("trace") or {}
        name = trace.get("type")
        if name == "assistant_text":
            print(f"{stamp} [{event.get('agent_id')}] assistant: {str(trace.get('text'))[:200]}")
        elif name == "tool_use":
            print(f"{stamp} [{event.get('agent_id')}] tool: {trace.get('name')} {str(trace.get('input_preview'))[:140]}")
        elif name == "turn_result":
            print(
                f"{stamp} [{event.get('agent_id')}] turn {trace.get('turn')} ended "
                f"{trace.get('stop_reason')} cost={trace.get('cost_usd')}"
            )
    elif kind == "genesis":
        peers = event.get("peers") or ([event.get("peer")] if event.get("peer") else [])
        print(f"{stamp} GENESIS {event.get('name')} (peers {', '.join(map(str, peers))})")
    elif kind == "agent_state":
        print(f"{stamp} {event.get('name')} state={event.get('state')} turn={event.get('turn')}")
    else:
        print(f"{stamp} {kind} {json.dumps({k: v for k, v in event.items() if k not in ('ts', 'ts_iso', 'run_id', 'event')}, ensure_ascii=False)[:220]}")


def cmd_report(args: argparse.Namespace) -> int:
    if args.run_dir:
        run_dir = Path(args.run_dir)
    else:
        run_dir = _latest_run_dir(PROJECT_ROOT)
    metrics = build_report(run_dir)
    print(json.dumps(metrics, indent=2, ensure_ascii=False))
    print(f"\ntranscript: {run_dir / 'transcript.md'}")
    print(f"metrics:    {run_dir / 'metrics.json'}")
    print(f"messages:   {run_dir / 'messages.jsonl'}")
    return 0


# --------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m supervisor",
        description="Genesis Lab: a Python supervisor for two Claude Code agents in two containers.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create .env and the runs directory").set_defaults(func=cmd_init)

    doctor = sub.add_parser("doctor", help="check the local prerequisites")
    doctor.add_argument("--mock", action="store_true", help="skip the API key check (for a mock dry run)")
    doctor.set_defaults(func=cmd_doctor)

    up = sub.add_parser("up", help="build, start the containers and run the world in the foreground")
    up.add_argument("--mock", action="store_true", help="use the scripted mock LLM instead of DeepSeek (no API key)")
    up.add_argument("--no-build", action="store_true", help="skip docker compose build")
    up.add_argument("--max-seconds", type=float, default=None, help="end the run after N seconds")
    up.add_argument("--max-messages", type=int, default=None, help="end the run after N chat messages")
    up.set_defaults(func=cmd_up)

    down = sub.add_parser("down", help="stop the containers")
    down.set_defaults(func=cmd_down)

    status = sub.add_parser("status", help="print the supervisor status as JSON")
    status.set_defaults(func=cmd_status)

    say = sub.add_parser("say", help="inject a message from the supervisor into the world")
    say.add_argument("text", nargs="+")
    say.add_argument("--to", default="all", help="all (default), a, b, or an agent name")
    say.set_defaults(func=cmd_say)

    end = sub.add_parser("end", help="ask the supervisor to end the run")
    end.add_argument("--reason", default="operator requested end")
    end.set_defaults(func=cmd_end)

    logs = sub.add_parser("logs", help="print the run event log")
    logs.add_argument("--run-dir", default=None)
    logs.add_argument("--tail", type=int, default=80)
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--once", action="store_true")
    logs.set_defaults(func=cmd_logs)

    report = sub.add_parser("report", help="write and print transcript.md + metrics.json")
    report.add_argument("--run-dir", default=None)
    report.set_defaults(func=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
