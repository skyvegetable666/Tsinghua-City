"""Opt-in paid check of two Claude user turns and peer-message acknowledgement."""

import argparse
import json
import os
import secrets
import socket
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from sqlalchemy import select

from tsinghua_city.api import create_app
from tsinghua_city.config import Settings
from tsinghua_city.models import AgentRuntime, Message
from tsinghua_city.source_fingerprint import verify_image


def run(args):
    if not any(os.environ.get(name) for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")):
        raise ValueError("an Anthropic-compatible provider credential is required")
    verify_image(args.image, Path(__file__).resolve().parents[1])
    with tempfile.TemporaryDirectory(prefix="city-live-bounded-") as temporary:
        root = Path(temporary)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        settings = Settings(
            database_url="sqlite:///" + str(root / "city.db"),
            admin_token=secrets.token_urlsafe(32), scheduler_enabled=False,
            runtime_log_dir=str(root / "runtime"), trace_dir=str(root / "traces"),
            manifest_dir=str(root / "manifests"), docker_image=args.image,
            docker_api_url=f"http://host.docker.internal:{port}",
        )
        app = create_app(settings)
        server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 20
        while not server.started:
            if time.monotonic() >= deadline:
                raise RuntimeError("disposable API startup timed out")
            time.sleep(.05)
        report = {"success": False, "image": args.image, "cleanup_pending": False}
        world = None
        peers = []
        adapter = app.state.runtime_manager._adapter("docker")
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False,
                              timeout=40) as client:
                admin = {"Authorization": "Bearer " + settings.admin_token}

                def post(path, body=None, headers=admin):
                    response = client.post(path, json=body, headers=headers)
                    response.raise_for_status()
                    return response.json()

                world = post("/v1/worlds", {"name": "bounded-live-smoke"})
                peers = [post(f"/v1/worlds/{world['id']}/agents", {"name": name})
                         for name in ("alice", "bob")]
                message = post("/v1/me/messages", {
                    "to": "alice", "body": "Call check_state once, then reply SECOND_TURN_OK.",
                }, {"Authorization": "Bearer " + peers[1]["token"]})
                base = f"/v1/worlds/{world['id']}/agents/{peers[0]['id']}/runtime"
                runtime = post(base + "/start", {
                    "adapter": "docker", "restart_policy": "never", "command": [
                        "python", "-m", "tsinghua_city.claude_agent", "--model", args.model,
                        "--prompt", "Call check_state once, then reply FIRST_TURN_OK. Do nothing else.",
                        "--max-rounds", "2", "--max-turns", "6", "--max-budget-usd", str(args.budget),
                        "--run-timeout", str(args.timeout), "--turn-timeout", str(args.timeout / 2),
                        "--listen-seconds", "0",
                    ],
                })
                deadline = time.monotonic() + args.timeout + 30
                while time.monotonic() < deadline:
                    response = client.get(base, headers=admin)
                    response.raise_for_status()
                    state = response.json()
                    if state["status"] != "RUNNING":
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError("bounded live run exceeded its deadline")
                events = []
                for line in Path(runtime["log_path"]).read_text().splitlines():
                    try:
                        event = json.loads(line)
                        if isinstance(event, dict):
                            events.append(event)
                    except ValueError:
                        continue
                texts = "\n".join(str(e.get("text", "")) for e in events
                                  if e.get("type") == "assistant_text")
                with app.state.session_factory() as session:
                    read = session.get(Message, message["id"]).read_at is not None
                stop = client.post(base + "/stop", headers=admin)
                report.update({
                    "status": state["status"], "exit_code": state["exit_code"],
                    "turn_results": sum(e.get("type") == "turn_result" for e in events),
                    "peer_message_acknowledged": read,
                    "first_marker": "FIRST_TURN_OK" in texts,
                    "second_marker": "SECOND_TURN_OK" in texts,
                    "tool_results": sum(e.get("type") == "tool_result" for e in events),
                    "stop_http_status": stop.status_code,
                    "reported_cumulative_costs": [e.get("cost_usd") for e in events
                                                   if e.get("type") == "turn_result"],
                })
                report["success"] = all((state["status"] == "STOPPED", state["exit_code"] == 0,
                                         report["turn_results"] == 2, read,
                                         report["first_marker"], report["second_marker"],
                                         report["tool_results"] >= 2, stop.status_code == 200))
        except Exception as exc:
            report["error_type"] = type(exc).__name__
        finally:
            with app.state.session_factory() as session:
                for runtime in session.scalars(select(AgentRuntime)):
                    try:
                        adapter.stop(runtime)
                    except Exception:
                        report["cleanup_pending"] = True
            for peer in peers:
                try:
                    name = "city-work-" + peer["id"]
                    if adapter._owned("volume", name, world["id"] + ":" + peer["id"]):
                        adapter._run(["volume", "rm", name], missing_ok=True)
                except Exception:
                    report["cleanup_pending"] = True
            if world:
                try:
                    name = settings.public_workspace_volume + "-" + world["id"]
                    if adapter._owned("volume", name, "public:" + world["id"]):
                        adapter._run(["volume", "rm", name], missing_ok=True)
                except Exception:
                    report["cleanup_pending"] = True
            server.should_exit = True
            thread.join(timeout=10)
            app.state.engine.dispose()
        report["success"] &= not report["cleanup_pending"]
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL"), required=False)
    parser.add_argument("--image", default=os.environ.get("CITY_DOCKER_IMAGE", "tsinghua-city-agent:0.1"))
    parser.add_argument("--budget", type=float, default=.25)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not args.model or args.budget <= 0 or args.timeout <= 0:
        parser.error("select a model and positive budget/timeout")
    report = run(args)
    text = json.dumps(report, indent=2) + "\n"
    if args.report:
        args.report.write_text(text)
        args.report.chmod(0o600)
    print(text, end="")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
