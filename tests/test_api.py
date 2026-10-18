"""World API integration tests: identity, credits, life, messages, events."""

from __future__ import annotations

import time

from conftest import ADMIN, agent_headers, create_agent, create_world
from sqlalchemy import func, select

from tsinghua_city.models import Account, Agent, LedgerTransaction


def test_create_world_and_agent(client, admin_headers):
    world = create_world(client, admin_headers)
    assert world["name"] == "world"
    assert world["config"]["initial_credits"] == 100

    agent = create_agent(client, admin_headers, world["id"], "alice")
    assert agent["name"] == "alice"
    assert agent["token"].startswith("city_")
    assert agent["balance"] == 100
    assert agent["alive"] is True

    me = client.get("/v1/me", headers=agent_headers(agent))
    assert me.status_code == 200
    assert me.json()["agent"]["name"] == "alice"
    assert me.json()["balance"] == 100

    wrong = client.get("/v1/me", headers={"Authorization": "Bearer wrong"})
    assert wrong.status_code == 401


def test_admin_required(client):
    assert client.post("/v1/worlds", json={"name": "x"}).status_code == 401
    assert client.post(
        "/v1/worlds", headers={"Authorization": "Bearer " + ADMIN}, json={"name": "x"}
    ).status_code == 201


def test_transfer_is_atomic_and_ledger_balanced(client, admin_headers, session_factory):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    bob = create_agent(client, admin_headers, world["id"], "bob")

    response = client.post(
        "/v1/me/transfer",
        headers=agent_headers(alice),
        json={"to": "bob", "amount": 30},
    )
    assert response.status_code == 200, response.text
    assert client.get("/v1/me/balance", headers=agent_headers(alice)).json()["balance"] == 70
    assert client.get("/v1/me/balance", headers=agent_headers(bob)).json()["balance"] == 130

    with session_factory() as session:
        total = session.scalar(select(func.sum(Account.balance)))
        assert total == 0
        assert session.scalar(select(func.count(LedgerTransaction.id))) == 3

    too_much = client.post(
        "/v1/me/transfer",
        headers=agent_headers(alice),
        json={"to": "bob", "amount": 999},
    )
    assert too_much.status_code == 409
    assert client.get("/v1/me/balance", headers=agent_headers(alice)).json()["balance"] == 70

    self_transfer = client.post(
        "/v1/me/transfer",
        headers=agent_headers(alice),
        json={"to": "alice", "amount": 1},
    )
    assert self_transfer.status_code == 400


def test_idempotent_transfer(client, admin_headers, session_factory):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    create_agent(client, admin_headers, world["id"], "bob")
    body = {"to": "bob", "amount": 25, "client_request_id": "req-1"}

    first = client.post("/v1/me/transfer", headers=agent_headers(alice), json=body)
    second = client.post("/v1/me/transfer", headers=agent_headers(alice), json=body)
    assert first.status_code == second.status_code == 200
    assert first.json()["transaction_id"] == second.json()["transaction_id"]
    assert client.get("/v1/me/balance", headers=agent_headers(alice)).json()["balance"] == 75
    with session_factory() as session:
        assert session.scalar(select(func.count(LedgerTransaction.id))) == 3

    conflict = client.post(
        "/v1/me/transfer",
        headers=agent_headers(alice),
        json={"to": "bob", "amount": 26, "client_request_id": "req-1"},
    )
    assert conflict.status_code == 409


def test_idempotent_life_and_message(client, admin_headers):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    bob = create_agent(client, admin_headers, world["id"], "bob")

    body = {"credits": 5, "client_request_id": "life-1"}
    first = client.post("/v1/me/extend-life", headers=agent_headers(alice), json=body)
    second = client.post("/v1/me/extend-life", headers=agent_headers(alice), json=body)
    assert first.status_code == second.status_code == 200
    assert first.json()["death_at"] == second.json()["death_at"]
    assert client.get("/v1/me/balance", headers=agent_headers(alice)).json()["balance"] == 95

    conflict = client.post(
        "/v1/me/extend-life",
        headers=agent_headers(alice),
        json={"credits": 6, "client_request_id": "life-1"},
    )
    assert conflict.status_code == 409

    msg = {"to": "bob", "body": "once", "client_message_id": "msg-1"}
    first_msg = client.post("/v1/me/messages", headers=agent_headers(alice), json=msg)
    second_msg = client.post("/v1/me/messages", headers=agent_headers(alice), json=msg)
    assert first_msg.status_code == second_msg.status_code == 201
    assert first_msg.json()["id"] == second_msg.json()["id"]
    inbox = client.get("/v1/me/messages", headers=agent_headers(bob)).json()
    assert len(inbox) == 1

    conflict_msg = client.post(
        "/v1/me/messages",
        headers=agent_headers(alice),
        json={"to": "bob", "body": "different", "client_message_id": "msg-1"},
    )
    assert conflict_msg.status_code == 409


def test_life_extension(client, admin_headers):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    before = client.get("/v1/me", headers=agent_headers(alice)).json()
    response = client.post(
        "/v1/me/extend-life",
        headers=agent_headers(alice),
        json={"credits": 10},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["credits_spent"] == 10
    assert body["seconds_added"] == 36_000
    assert body["balance"] == 90
    after = client.get("/v1/me", headers=agent_headers(alice)).json()
    assert after["agent"]["death_at"] != before["agent"]["death_at"]


def test_dead_agent_is_rejected(client, admin_headers, session_factory):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    with session_factory() as session:
        agent = session.get(Agent, alice["id"])
        assert agent is not None
        agent.death_at = time.time() - 1
        session.commit()

    response = client.get("/v1/me/balance", headers=agent_headers(alice))
    assert response.status_code == 409
    with session_factory() as session:
        agent = session.get(Agent, alice["id"])
        assert agent is not None
        assert agent.alive is False


def test_messages_and_read_receipts(client, admin_headers):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    bob = create_agent(client, admin_headers, world["id"], "bob")

    sent = client.post(
        "/v1/me/messages",
        headers=agent_headers(alice),
        json={"to": "bob", "body": "hello bob"},
    )
    assert sent.status_code == 201, sent.text
    message = sent.json()
    assert message["sender"] == "alice"
    assert message["recipient"] == "bob"

    inbox = client.get("/v1/me/messages", headers=agent_headers(bob))
    assert inbox.status_code == 200
    assert [m["id"] for m in inbox.json()] == [message["id"]]
    assert inbox.json()[0]["read_at"] is None

    marked = client.post(
        "/v1/me/messages/read",
        headers=agent_headers(bob),
        json={"message_ids": [message["id"]]},
    )
    assert marked.json()["changed"] == 1
    unread = client.get(
        "/v1/me/messages?unread_only=true", headers=agent_headers(bob)
    )
    assert unread.json() == []

    wrong_reader = client.post(
        "/v1/me/messages/read",
        headers=agent_headers(alice),
        json={"message_ids": [message["id"]]},
    )
    assert wrong_reader.json()["changed"] == 0


def test_multi_world_isolation_and_duplicate_agent(client, admin_headers, session_factory):
    one = create_world(client, admin_headers, name="one")
    two = create_world(client, admin_headers, name="two")
    alice_one = create_agent(client, admin_headers, one["id"], "alice")
    alice_two = create_agent(client, admin_headers, two["id"], "alice")
    create_agent(client, admin_headers, two["id"], "bob")

    assert client.get("/v1/me", headers=agent_headers(alice_one)).json()["agent"]["world_id"] == one["id"]
    assert client.get("/v1/me", headers=agent_headers(alice_two)).json()["agent"]["world_id"] == two["id"]

    missing = client.post(
        "/v1/me/transfer",
        headers=agent_headers(alice_one),
        json={"to": "bob", "amount": 1},
    )
    assert missing.status_code == 404

    duplicate = client.post(
        f"/v1/worlds/{one['id']}/agents",
        headers=admin_headers,
        json={"name": "alice"},
    )
    assert duplicate.status_code == 409

    with session_factory() as session:
        row = session.get(Agent, alice_one["id"])
        assert row is not None
        assert row.token_hash != alice_one["token"]
        assert len(row.token_hash) == 64


def test_peers_and_events(client, admin_headers):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    bob = create_agent(client, admin_headers, world["id"], "bob")

    peers = client.get("/v1/me/agents", headers=agent_headers(alice))
    assert peers.status_code == 200
    assert [a["name"] for a in peers.json()] == ["bob"]

    events = client.get(
        f"/v1/worlds/{world['id']}/events",
        headers={"Authorization": "Bearer " + ADMIN},
    )
    assert events.status_code == 200
    kinds = [event["event_type"] for event in events.json()]
    assert "world.created" in kinds
    assert "agent.created" in kinds
    assert bob["name"] == "bob"


def test_reap_expired(client, admin_headers, session_factory):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    with session_factory() as session:
        agent = session.get(Agent, alice["id"])
        assert agent is not None
        agent.death_at = time.time() - 1
        session.commit()

    response = client.post(
        f"/v1/worlds/{world['id']}/reap",
        headers={"Authorization": "Bearer " + ADMIN},
    )
    assert response.status_code == 200
    assert response.json()["reaped"] == 1
    me = client.get("/v1/me", headers=agent_headers(alice))
    assert me.status_code == 409
