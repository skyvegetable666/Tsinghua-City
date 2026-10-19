"""The Alembic baseline must create the current schema and be re-runnable."""

from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from tsinghua_city.db import upgrade_database
from tsinghua_city.models import Base


def test_upgrade_creates_schema_and_records_version(tmp_path):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    upgrade_database(url)
    upgrade_database(url)

    engine = create_engine(url)
    try:
        assert {
            "worlds",
            "agents",
            "accounts",
            "tasks",
            "messages",
            "posts",
            "post_replies",
            "agent_runtimes",
            "operator_messages",
            "experiment_runs",
            "employers",
            "conversations",
            "conversation_members",
            "conversation_messages",
            "model_grants",
            "events",
            "alembic_version",
        } <= set(inspect(engine).get_table_names())
        with engine.connect() as connection:
            version = connection.execute(text("select version_num from alembic_version")).scalar()
        assert version == "0007_experiment_operations"
    finally:
        engine.dispose()


def test_migration_roundtrip_preserves_existing_world(tmp_path):
    url = f"sqlite:///{tmp_path / 'upgrade.db'}"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "0006_operator_messages")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("insert into worlds values ('existing', 'history', 'active', '{}', 1)"))
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0006_operator_messages")
    command.upgrade(cfg, "head")
    with engine.connect() as connection:
        assert connection.execute(text("select name from worlds")).scalar() == "history"
    engine.dispose()


@pytest.mark.skipif(not os.environ.get("CITY_TEST_DATABASE_URL"), reason="requires disposable Postgres")
def test_postgres_migrations(engine):
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", os.environ["CITY_TEST_DATABASE_URL"])
    command.upgrade(cfg, "head")
    assert "model_grants" in inspect(engine).get_table_names()
    command.downgrade(cfg, "0006_operator_messages")
    command.upgrade(cfg, "head")
    assert "employer_id" in {c["name"] for c in inspect(engine).get_columns("tasks")}
