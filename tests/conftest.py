"""Shared API fixtures.

By default tests use an in-memory SQLite database. To run the same suite against
Postgres, install the postgres extra and set:

    CITY_TEST_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/city_test

The test database is dropped/recreated by the fixture; never point this at a
database whose contents matter.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from tsinghua_city.api import create_app
from tsinghua_city.config import Settings
from tsinghua_city.db import init_db, make_engine, make_session_factory
from tsinghua_city.models import Base

ADMIN = "admin-token"


@pytest.fixture
def engine():
    url = os.environ.get("CITY_TEST_DATABASE_URL", "").strip()
    if url:
        engine = make_engine(url)
        Base.metadata.drop_all(engine)
        init_db(engine)
    else:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url="sqlite://",
        admin_token=ADMIN,
        scheduler_enabled=False,
        runtime_log_dir=str(tmp_path / "runtime"),
    )


@pytest.fixture
def app(engine, settings):
    return create_app(settings, engine=engine)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def session_factory(engine):
    return make_session_factory(engine)


@pytest.fixture
def admin_headers():
    return {"Authorization": "Bearer " + ADMIN}


def create_world(client: TestClient, admin_headers: dict, *, name: str = "world") -> dict:
    response = client.post(
        "/v1/worlds",
        headers=admin_headers,
        json={"name": name},
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_agent(
    client: TestClient,
    admin_headers: dict,
    world_id: str,
    name: str,
    **kwargs,
) -> dict:
    response = client.post(
        f"/v1/worlds/{world_id}/agents",
        headers=admin_headers,
        json={"name": name, **kwargs},
    )
    assert response.status_code == 201, response.text
    return response.json()


def agent_headers(agent: dict) -> dict:
    return {"Authorization": "Bearer " + agent["token"]}
