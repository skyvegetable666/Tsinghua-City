"""Maintenance sweep: deadline expiry, refunds, and agent reaping."""

from __future__ import annotations

from conftest import agent_headers, create_agent, create_world
from sqlalchemy import select

from tsinghua_city.models import Agent, Task
from tsinghua_city.scheduler import tick_once


def test_tick_expires_tasks_and_reaps_agents(client, admin_headers, session_factory):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    bob = create_agent(client, admin_headers, world["id"], "bob")

    task = client.post(
        "/v1/me/tasks",
        headers=agent_headers(bob),
        json={
            "title": "expires",
            "description": "do not do this",
            "acceptance_criteria": "none",
            "reward": 10,
            "deadline_seconds": 60,
        },
    ).json()
    with session_factory() as session:
        task_row = session.get(Task, task["id"])
        alice_row = session.get(Agent, alice["id"])
        assert task_row is not None and alice_row is not None
        task_row.deadline_at = 0
        alice_row.death_at = 0
        session.commit()

    result = tick_once(session_factory)
    assert result["expired_tasks"] >= 1
    assert result["reaped_agents"] >= 1

    with session_factory() as session:
        task_row = session.get(Task, task["id"])
        assert task_row is not None
        assert task_row.status == "CANCELLED"
        bob_row = session.scalar(select(Agent).where(Agent.id == bob["id"]))
        assert bob_row is not None

    assert client.get("/v1/me/balance", headers=agent_headers(bob)).json()["balance"] == 100
    assert client.get("/v1/me", headers=agent_headers(alice)).status_code == 409
