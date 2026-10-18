"""Run a small two-agent reference experiment against a running World API.

Prerequisites:
  * Tsinghua City API is running (for example `tsinghua-city serve`).
  * CITY_ADMIN_TOKEN matches the server.
  * This script and the server use the same Python environment/package.

The script creates a fresh world, starts two reference harness runtimes, waits,
prints the task board, and stops the runtimes.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + token}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("CITY_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--admin-token", default=os.environ.get("CITY_ADMIN_TOKEN"))
    parser.add_argument("--ticks", type=int, default=6)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--world", default=None)
    args = parser.parse_args()
    if not args.admin_token:
        parser.error("CITY_ADMIN_TOKEN or --admin-token is required")

    admin = _headers(args.admin_token)
    world_name = args.world or "reference-" + time.strftime("%Y%m%d-%H%M%S")
    journal_dir = (Path.cwd() / "runs" / "demo-journals" / world_name).resolve()
    journal_dir.mkdir(parents=True, exist_ok=True)

    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=30, trust_env=False) as client:
        world = client.post("/v1/worlds", headers=admin, json={"name": world_name})
        world.raise_for_status()
        world_id = world.json()["id"]
        agents = {}
        for name in ("alice", "bob"):
            response = client.post(
                f"/v1/worlds/{world_id}/agents",
                headers=admin,
                json={"name": name},
            )
            response.raise_for_status()
            agents[name] = response.json()

        command = [
            sys.executable,
            "-m",
            "tsinghua_city.reference_harness",
            "--ticks",
            str(args.ticks),
            "--interval",
            str(args.interval),
            "--journal-dir",
            str(journal_dir),
        ]
        started = {}
        for name, agent in agents.items():
            response = client.post(
                f"/v1/worlds/{world_id}/agents/{agent['id']}/runtime/start",
                headers=admin,
                json={"command": command, "restart_policy": "never"},
            )
            response.raise_for_status()
            started[name] = response.json()["id"]
            print(f"started {name}: runtime={started[name]}")

        time.sleep(max(1.0, args.ticks * (args.interval + 0.25)) + 2.0)

        tasks = client.get(f"/v1/worlds/{world_id}/tasks", headers=admin)
        tasks.raise_for_status()
        print("\ntask board:")
        for task in tasks.json():
            print(
                f"  {task['status']:9} reward={task['reward']:3} "
                f"publisher={task['publisher']:8} assignee={task['assignee'] or '-'}"
            )

        for name, agent in agents.items():
            response = client.get(
                f"/v1/worlds/{world_id}/agents/{agent['id']}/runtime",
                headers=admin,
            )
            response.raise_for_status()
            runtime = response.json()
            print(f"{name} runtime: {runtime['status']} restart_count={runtime['restart_count']}")
            if runtime["status"] in {"RUNNING", "SUSPENDED", "CRASHED"}:
                client.post(
                    f"/v1/worlds/{world_id}/agents/{agent['id']}/runtime/terminate",
                    headers=admin,
                )

        print(f"\nworld: {world_id}")
        print(f"journals: {journal_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
