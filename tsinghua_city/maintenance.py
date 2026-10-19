"""Coordinate host maintenance with database advisory locks or FileLock."""

import hashlib
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout
from sqlalchemy import text


@contextmanager
def host_lock(factory, settings):
    engine = factory.kw["bind"]
    if engine.dialect.name == "postgresql":
        key = int.from_bytes(
            hashlib.sha256(settings.runtime_host_id.encode()).digest()[:8], "big", signed=True
        )
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            locked = connection.scalar(text("select pg_try_advisory_lock(:key)"), {"key": key})
            try:
                yield bool(locked)
            finally:
                if locked:
                    connection.execute(text("select pg_advisory_unlock(:key)"), {"key": key})
    else:
        directory = Path(settings.runtime_log_dir).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        lock = FileLock(directory / ".maintenance.lock", thread_local=False)
        try:
            lock.acquire(timeout=0)
        except Timeout:
            yield False
        else:
            try:
                yield True
            finally:
                lock.release()
