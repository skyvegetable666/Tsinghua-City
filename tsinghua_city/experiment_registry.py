"""Durable experiment intent and research measurements over existing world services."""

import hashlib
import json
import math
import subprocess
import uuid
from collections import Counter
from pathlib import Path

from sqlalchemy import select, update

from . import service
from .config import iso, utcnow
from .experiments import ExperimentManifest
from .models import Account, Agent, AgentRuntime, Event, ExperimentRun, Message, Task, World

TERMINAL = {"COMPLETED", "FAILED", "STOPPED"}


def provenance(settings):
    root = Path(__file__).resolve().parents[1]

    def command(argv):
        try:
            result = subprocess.run(
                argv, cwd=root, capture_output=True, text=True, timeout=5, check=False
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    from .source_fingerprint import source_hash

    dirty = command(["git", "status", "--porcelain"])
    return {
        "git_commit": command(["git", "rev-parse", "HEAD"]),
        "git_dirty": bool(dirty) if dirty is not None else None,
        "source_sha256": source_hash(root),
        "docker_image": settings.docker_image,
        "image_digest": command(
            ["docker", "image", "inspect", "--format", "{{.Id}}", settings.docker_image]
        ),
    }


def enqueue(session, settings, manifest: ExperimentManifest, repeat=1):
    batch = "batch_" + uuid.uuid4().hex[:20]
    fingerprint = provenance(settings)
    rows = []
    for _ in range(repeat):
        row = ExperimentRun(
            id="exp_" + uuid.uuid4().hex[:24],
            batch_id=batch,
            name=manifest.name,
            status="QUEUED",
            owner_host=settings.runtime_host_id,
            manifest=manifest.model_dump(mode="json"),
            provenance=fingerprint,
            created_at=utcnow(),
        )
        session.add(row)
        rows.append(row)
    session.commit()
    return rows


def view(row):
    return {
        "id": row.id,
        "name": row.name,
        "batch_id": row.batch_id,
        "status": row.status,
        "world_id": row.world_id,
        "owner_host": row.owner_host,
        "created_at": iso(row.created_at),
        "started_at": iso(row.started_at),
        "finished_at": iso(row.finished_at),
        "error": row.error,
        "manifest": row.manifest,
        "provenance": row.provenance,
    }


def command(spec, experiment_id):
    argv = [
        "python",
        "-m",
        "tsinghua_city.claude_agent",
        "--agent-name",
        spec.name,
        "--model",
        spec.model,
        "--prompt",
        spec.prompt,
        "--experiment-id",
        experiment_id,
        "--run-id",
        experiment_id + "-" + spec.name,
    ]
    for key, value in spec.limits.model_dump().items():
        argv.extend(["--" + key.replace("_", "-"), str(value)])
    return argv


def _cleanup(session, manager, row):
    from .model_budget import revoke

    if row.world_id is None:
        return True
    world = session.get(World, row.world_id)
    clean = True
    for runtime in session.scalars(
        select(AgentRuntime).where(
            AgentRuntime.world_id == row.world_id,
        )
    ).all():
        try:
            if runtime.status != "STOPPED":
                manager.stop(session, world, session.get(Agent, runtime.agent_id))
            revoke(session, manager.settings, runtime.id)
        except Exception:
            session.rollback()
            clean = False
    return clean


def request_stop(session, row):
    session.execute(
        update(ExperimentRun)
        .where(
            ExperimentRun.id == row.id,
            ExperimentRun.status.not_in(TERMINAL),
        )
        .values(status="STOPPING")
    )
    session.commit()
    session.refresh(row)
    return row


def _launch(session, manager, row):
    claimed = session.execute(
        update(ExperimentRun)
        .where(
            ExperimentRun.id == row.id,
            ExperimentRun.status == "QUEUED",
        )
        .values(status="STARTING", started_at=utcnow())
    ).rowcount
    session.commit()
    if not claimed:
        return
    try:
        row.provenance = provenance(manager.settings)
        session.commit()
        spec = ExperimentManifest.model_validate(row.manifest)
        world = service.create_world(session, spec.name, spec.world)
        row.world_id = world.id
        session.commit()
        peers = []
        for agent_spec in spec.agents:
            agent, _token = service.create_agent(
                session,
                world,
                agent_spec.name,
                initial_credits=agent_spec.initial_credits,
                initial_life_seconds=agent_spec.initial_life_seconds,
            )
            peers.append((agent_spec, agent))
        for agent_spec, agent in peers:
            session.refresh(row)
            if row.status != "STARTING":
                raise service.DomainError("launch stopped by operator", 409)
            # Persist the Docker identity before its first external side effect.
            # An interrupted start can then remove the container without replaying a prompt.
            runtime = AgentRuntime(
                id="rt_" + uuid.uuid4().hex[:24],
                world_id=world.id,
                agent_id=agent.id,
                adapter="docker",
                status="LOST",
                owner_host=manager.settings.runtime_host_id,
                command=command(agent_spec, row.id),
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            session.add(runtime)
            session.commit()
            manager.start(
                session,
                world,
                agent,
                adapter="docker",
                restart_policy="never",
                command=command(agent_spec, row.id),
            )
        session.execute(
            update(ExperimentRun)
            .where(
                ExperimentRun.id == row.id,
                ExperimentRun.status == "STARTING",
            )
            .values(status="RUNNING")
        )
        session.commit()
    except Exception:
        session.rollback()
        session.refresh(row)
        stopped = row.status == "STOPPING"
        row.error = (
            None if stopped else "launch failed; inspect runtime events and host diagnostics"
        )
        row.status = "STOPPING"
        session.commit()
        if _cleanup(session, manager, row):
            _finish(session, manager.settings, row, "STOPPED" if stopped else "FAILED")


def _finish(session, settings, row, status):
    row.status = status
    row.finished_at = utcnow()
    row.measurements = metrics(session, settings, row)
    session.commit()


def process_runs(factory, manager):
    """Called under the host maintenance lock; no message can enqueue a launch."""
    changed = 0
    with factory() as session:
        rows = session.scalars(
            select(ExperimentRun)
            .where(
                ExperimentRun.owner_host == manager.settings.runtime_host_id,
                ExperimentRun.status.not_in(TERMINAL),
            )
            .order_by(ExperimentRun.created_at)
        ).all()
        for row in rows:
            if row.status == "QUEUED":
                _launch(session, manager, row)
                changed += 1
                continue
            if row.status == "STARTING":
                row.error = "interrupted launch; execution is cleaned up without replaying prompts"
                row.status = "STOPPING"
                session.commit()
            if row.status == "STOPPING":
                if _cleanup(session, manager, row):
                    _finish(session, manager.settings, row, "FAILED" if row.error else "STOPPED")
                    changed += 1
                continue
            runtimes = session.scalars(
                select(AgentRuntime).where(
                    AgentRuntime.world_id == row.world_id,
                )
            ).all()
            failed = len(runtimes) != len(row.manifest["agents"]) or any(
                runtime.status in {"CRASHED", "LOST"} for runtime in runtimes
            )
            if failed:
                row.error = "one or more agent runtimes failed"
                row.status = "STOPPING"
                session.commit()
                if not _cleanup(session, manager, row):
                    continue
            if failed or all(runtime.status == "STOPPED" for runtime in runtimes):
                if not _cleanup(session, manager, row):
                    continue
                _finish(session, manager.settings, row, "FAILED" if failed else "COMPLETED")
                changed += 1
    return changed


def trace_metrics(settings, experiment_id):
    costs = {}
    calls = set()
    records = 0
    truncated = False
    for path in Path(settings.trace_dir).expanduser().glob("*.jsonl"):
        if path.is_symlink():
            continue
        try:
            with path.open() as handle:
                for line in handle:
                    records += 1
                    if records > 1_000_000:
                        truncated = True
                        break
                    try:
                        event = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(event, dict) or event.get("experiment_id") != experiment_id:
                        continue
                    run = event.get("run_id", "unknown")
                    if not isinstance(run, str):
                        continue
                    if event.get("type") == "turn_result":
                        cost = event.get("cost_usd")
                        if type(cost) in {int, float} and math.isfinite(cost) and cost >= 0:
                            # Native total_cost_usd is cumulative within a CLI session;
                            # copied traces/stdout snapshots must not multiply that total.
                            costs[run] = max(costs.get(run, 0), cost)
                    if event.get("type") == "tool_use":
                        identity = (
                            event.get("tool_use_id") or hashlib.sha256(line.encode()).hexdigest()
                        )
                        if isinstance(identity, str):
                            calls.add((run, identity))
        except OSError:
            continue
        if truncated:
            break
    return {
        "reported_cost_usd": sum(costs.values()) if costs else None,
        "cost_reporting_runs": len(costs),
        "tool_calls": len(calls),
        "trace_scan_truncated": truncated,
    }


def metrics(session, settings, row):
    if row.measurements is not None:
        return row.measurements
    agents = session.scalars(select(Agent).where(Agent.world_id == row.world_id)).all()
    balances = list(
        session.scalars(
            select(Account.balance).where(
                Account.world_id == row.world_id,
                Account.kind == "agent",
            )
        )
    )
    tasks = session.scalars(select(Task).where(Task.world_id == row.world_id)).all()
    counts = Counter(task.status for task in tasks)
    messages = session.scalars(select(Message).where(Message.world_id == row.world_id)).all()
    cutoff = row.finished_at or utcnow()
    survival = [max(0, min(agent.death_at, cutoff) - agent.created_at) for agent in agents]
    n = len(balances)
    total = sum(balances)
    gini = (
        (
            sum((2 * i - n - 1) * amount for i, amount in enumerate(sorted(balances), 1))
            / (n * total)
        )
        if n and total
        else 0
    )
    return {
        "agents": len(agents),
        "alive": sum(agent.death_at > cutoff for agent in agents),
        "tasks": len(tasks),
        "tasks_by_status": dict(counts),
        "completion_rate": counts["COMPLETED"] / len(tasks) if tasks else None,
        "messages": len(messages),
        "messages_read": sum(m.read_at is not None for m in messages),
        "credits_total": total,
        "credits_min": min(balances, default=0),
        "credits_max": max(balances, default=0),
        "credits_gini": gini,
        "mean_survival_seconds": sum(survival) / len(survival) if survival else None,
        "elapsed_seconds": max(0, cutoff - (row.started_at or cutoff)),
        **trace_metrics(settings, row.id),
    }


def export_run(session, settings, row):
    events = session.scalars(
        select(Event)
        .where(
            Event.world_id == row.world_id,
            Event.created_at <= (row.finished_at or utcnow()),
        )
        .order_by(Event.created_at, Event.id)
    ).all()
    return {
        "version": 1,
        "run": view(row),
        "metrics": metrics(session, settings, row),
        "events": [
            {
                "id": e.id,
                "type": e.event_type,
                "at": iso(e.created_at),
                "actor": e.actor_agent_id,
                "data": e.data,
            }
            for e in events
        ],
    }
