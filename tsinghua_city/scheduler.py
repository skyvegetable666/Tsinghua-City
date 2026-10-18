"""Small in-process maintenance loop.

This is deliberately simple: it sweeps task deadlines and agent death deadlines.
A deployed experiment should replace it with Temporal/Postgres-backed durable
timers; the sweep is idempotent, so losing/duplicating a tick is safe.
"""

from __future__ import annotations

import logging
import threading

from sqlalchemy import select

from . import service
from .models import World

logger = logging.getLogger(__name__)


def tick_once(session_factory) -> dict[str, int]:
    expired = 0
    reaped = 0
    with session_factory() as session:
        worlds = session.scalars(select(World).where(World.status == "active")).all()
        for world in worlds:
            expired += service.expire_tasks(session, world)
            reaped += service.reap_expired(session, world)
    return {"expired_tasks": expired, "reaped_agents": reaped}


class Scheduler:
    def __init__(self, session_factory, interval_seconds: float = 5.0) -> None:
        self._session_factory = session_factory
        self._interval = max(0.5, float(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="city-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                result = tick_once(self._session_factory)
                if result["expired_tasks"] or result["reaped_agents"]:
                    logger.info("maintenance tick: %s", result)
            except Exception:
                logger.exception("maintenance tick failed")
