"""Shared SQLite/FastAPI fixtures for the world service."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from tsinghua_city.api import create_app
from tsinghua_city.config import Settings
from tsinghua_city.db import init_db, make_session_factory

ADMIN = "admin-token"


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def settings() -> Settings:
    return Settings(database_url="sqlite://", admin_token=ADMIN)


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
