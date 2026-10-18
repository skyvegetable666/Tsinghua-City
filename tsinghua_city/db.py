"""Engine/session plumbing.

The database is the authoritative store; keep all SQLAlchemy setup in one place
so a Postgres deployment is a URL change plus migrations.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .models import Base


def _sqlite_path(url: str) -> str | None:
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return None
    database = parsed.database
    if not database or database == ":memory:":
        return ":memory:"
    return database


def make_engine(url: str) -> Engine:
    path = _sqlite_path(url)
    if path is not None:
        if path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        kwargs: dict = {"connect_args": {"check_same_thread": False}}
        if path == ":memory:":
            kwargs["poolclass"] = StaticPool
        engine = create_engine(url, pool_pre_ping=True, **kwargs)
    else:
        engine = create_engine(url, pool_pre_ping=True)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _enable_sqlite_fk(dbapi_connection, _connection_record):  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
