"""The Alembic baseline must create the current schema and be re-runnable."""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from tsinghua_city.db import upgrade_database


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
            "events",
            "alembic_version",
        } <= set(inspect(engine).get_table_names())
        with engine.connect() as connection:
            version = connection.execute(text("select version_num from alembic_version")).scalar()
        assert version == "0001_initial"
    finally:
        engine.dispose()
