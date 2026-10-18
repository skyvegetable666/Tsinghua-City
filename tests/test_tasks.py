"""Task-board lifecycle: escrow, claim, submit, accept/reject/cancel, external tasks."""

from __future__ import annotations

from conftest import ADMIN, agent_headers, create_agent, create_world
from sqlalchemy import func, select

from tsinghua_city.models import Account, Task


def _publish(client, agent, *, reward: int = 30, **kwargs):
    payload = {
        "title": "write a report",
        "description": "produce a short report",
        "acceptance_criteria": "report exists and is coherent",
        "reward": reward,
    }
    payload.update(kwargs)
    response = client.post("/v1/me/tasks", headers=agent_headers(agent), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_agent_task_happy_path(client, admin_headers, session_factory):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    bob = create_agent(client, admin_headers, world["id"], "bob")

    task = _publish(client, alice, reward=30)
    assert task["status"] == "OPEN"
    assert task["publisher"] == "alice"
    assert task["reward"] == 30
    assert client.get("/v1/me/balance", headers=agent_headers(alice)).json()["balance"] == 70

    board = client.get("/v1/me/tasks", headers=agent_headers(bob))
    assert board.status_code == 200
    assert board.json()[0]["id"] == task["id"]

    claimed = client.post(
        f"/v1/me/tasks/{task['id']}/claim",
        headers=agent_headers(bob),
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["status"] == "CLAIMED"
    assert claimed.json()["assignee"] == "bob"

    submitted = client.post(
        f"/v1/me/tasks/{task['id']}/submit",
        headers=agent_headers(bob),
        json={"submission": "report v1"},
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "SUBMITTED"

    accepted = client.post(
        f"/v1/me/tasks/{task['id']}/accept",
        headers=agent_headers(alice),
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "COMPLETED"
    assert client.get("/v1/me/balance", headers=agent_headers(bob)).json()["balance"] == 130

    with session_factory() as session:
        assert session.scalar(select(func.sum(Account.balance))) == 0
        assert session.scalar(select(func.count(Task.id))) == 1


def test_task_authorization_reject_and_cancel(client, admin_headers):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    bob = create_agent(client, admin_headers, world["id"], "bob")

    task = _publish(client, alice, reward=20)
    self_claim = client.post(
        f"/v1/me/tasks/{task['id']}/claim",
        headers=agent_headers(alice),
    )
    assert self_claim.status_code == 409

    client.post(f"/v1/me/tasks/{task['id']}/claim", headers=agent_headers(bob))
    wrong_accept = client.post(
        f"/v1/me/tasks/{task['id']}/accept",
        headers=agent_headers(bob),
    )
    assert wrong_accept.status_code == 403

    submitted = client.post(
        f"/v1/me/tasks/{task['id']}/submit",
        headers=agent_headers(bob),
        json={"submission": "not quite"},
    )
    assert submitted.status_code == 200

    reopened = client.post(
        f"/v1/me/tasks/{task['id']}/reject",
        headers=agent_headers(alice),
        json={"reason": "needs revision"},
    )
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "OPEN"
    assert reopened.json()["assignee"] is None

    cancelled = client.post(
        f"/v1/me/tasks/{task['id']}/cancel",
        headers=agent_headers(alice),
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    assert client.get("/v1/me/balance", headers=agent_headers(alice)).json()["balance"] == 100


def test_external_task_and_expiry(client, admin_headers, session_factory):
    world = create_world(client, admin_headers)
    bob = create_agent(client, admin_headers, world["id"], "bob")

    external = client.post(
        f"/v1/worlds/{world['id']}/tasks",
        headers={"Authorization": "Bearer " + ADMIN},
        json={
            "title": "external job",
            "description": "do external work",
            "acceptance_criteria": "work is done",
            "reward": 40,
        },
    )
    assert external.status_code == 201, external.text
    task = external.json()
    assert task["source"] == "external"
    assert task["publisher"] == "external"

    assert client.post(
        f"/v1/me/tasks/{task['id']}/claim", headers=agent_headers(bob)
    ).status_code == 200
    assert client.post(
        f"/v1/me/tasks/{task['id']}/submit",
        headers=agent_headers(bob),
        json={"submission": "done"},
    ).status_code == 200
    accepted = client.post(
        f"/v1/worlds/{world['id']}/tasks/{task['id']}/accept",
        headers={"Authorization": "Bearer " + ADMIN},
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "COMPLETED"
    assert client.get("/v1/me/balance", headers=agent_headers(bob)).json()["balance"] == 140

    expiring = _publish(client, bob, reward=10, deadline_seconds=60)
    with session_factory() as session:
        row = session.get(Task, expiring["id"])
        assert row is not None
        row.deadline_at = 0
        session.commit()
    expired = client.post(
        f"/v1/worlds/{world['id']}/tasks/expire",
        headers={"Authorization": "Bearer " + ADMIN},
    )
    assert expired.status_code == 200
    assert expired.json()["expired"] == 1
    assert client.get(f"/v1/me/tasks/{expiring['id']}", headers=agent_headers(bob)).json()["status"] == "CANCELLED"
