"""Conservative artifact retention and SQLite's native consistent backup API."""

import json
import os
import sqlite3
from pathlib import Path

from sqlalchemy import select

from .config import utcnow
from .experiment_registry import TERMINAL
from .models import AgentRuntime, ExperimentRun, ModelGrant


def prune(session, settings, *, days=None, apply=False):
    days = settings.archive_retention_days if days is None else days
    if days <= 0:
        raise ValueError("retention days must be positive")
    cutoff = utcnow() - days * 86400
    runs = session.scalars(
        select(ExperimentRun).where(
            ExperimentRun.owner_host == settings.runtime_host_id,
            ExperimentRun.status.in_(TERMINAL),
            ExperimentRun.finished_at < cutoff,
            ExperimentRun.measurements.is_not(None),
        )
    ).all()
    worlds = {row.world_id for row in runs if row.world_id}
    runtimes = session.scalars(select(AgentRuntime).where(AgentRuntime.world_id.in_(worlds))).all()
    blocked = {
        r.world_id
        for r in runtimes
        if r.status != "STOPPED"
        or r.owner_host not in {None, settings.runtime_host_id}
        or session.get(ModelGrant, r.id) is not None
    }
    worlds -= blocked
    roots = {
        Path(p).expanduser().resolve()
        for p in (settings.trace_dir, settings.manifest_dir, settings.runtime_log_dir)
    }
    candidates = set()
    for runtime in runtimes:
        if runtime.world_id not in worlds:
            continue
        if runtime.log_path:
            candidates.add(Path(runtime.log_path).absolute())
        candidates.update(Path(settings.manifest_dir).expanduser().glob(runtime.id + "-*.json"))
    for path in Path(settings.trace_dir).expanduser().glob("*.jsonl"):
        if path.is_symlink():
            continue
        try:
            with path.open() as handle:
                for _ in range(20):
                    line = handle.readline(1_000_000)
                    if not line:
                        break
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and event.get("world_id") in worlds:
                        candidates.add(path)
                        break
        except OSError:
            continue
    removed = []
    for path in sorted(candidates):
        if path.is_symlink() or not path.is_file() or path.resolve().parent not in roots:
            continue
        if path.stat().st_mtime > cutoff:
            continue
        removed.append(str(path.resolve()))
        if apply:
            path.unlink()
    return {
        "applied": apply,
        "retention_days": days,
        "files": removed,
        "database_records_preserved": True,
        "workspace_volumes_preserved": True,
    }


def copy_sqlite(source: Path, destination: Path):
    """Read a consistent SQLite snapshot into a new file; never overwrite a database."""
    source = source.expanduser().resolve(strict=True)
    destination = destination.expanduser().absolute()
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
        if src.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("SQLite source failed integrity validation")
        tables = {
            row[0] for row in src.execute("select name from sqlite_master where type='table'")
        }
        if not {"worlds", "agents"} <= tables:
            raise ValueError("source is not a City database")
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        try:
            with sqlite3.connect(destination) as dst:
                src.backup(dst)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
    return {"path": str(destination), "backend": "sqlite", "consistent_snapshot": True}
