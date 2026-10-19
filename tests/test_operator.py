"""Generalized human-operator message channel."""

from __future__ import annotations

from conftest import agent_headers, create_agent, create_world


def test_operator_and_agent_exchange_messages(client, admin_headers):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    base = f"/v1/worlds/{world['id']}/agents/{alice['id']}/operator/messages"

    sent = client.post(base, headers=admin_headers, json={"body": "how is the task going?"})
    assert sent.status_code == 201, sent.text
    message = sent.json()
    assert message["sender"] == "operator"

    inbox = client.get("/v1/me/operator/messages", headers=agent_headers(alice))
    assert inbox.status_code == 200
    assert [item["body"] for item in inbox.json()] == ["how is the task going?"]

    read = client.post(
        "/v1/me/operator/messages/read",
        headers=agent_headers(alice),
        json={"message_ids": [message["id"]]},
    )
    assert read.status_code == 200
    assert read.json()["changed"] == 1

    reply = client.post(
        "/v1/me/operator/messages",
        headers=agent_headers(alice),
        json={"body": "task is in progress"},
    )
    assert reply.status_code == 201
    assert reply.json()["sender"] == "agent"

    history = client.get(base, headers=admin_headers)
    assert history.status_code == 200
    assert [item["sender"] for item in history.json()] == ["operator", "agent"]


def test_operator_channel_rejects_wrong_agent_and_empty_body(client, admin_headers):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    bob = create_agent(client, admin_headers, world["id"], "bob")
    base = f"/v1/worlds/{world['id']}/agents/{alice['id']}/operator/messages"
    message = client.post(base, headers=admin_headers, json={"body": "for alice"}).json()

    assert (
        client.post(
            "/v1/me/operator/messages/read",
            headers=agent_headers(bob),
            json={"message_ids": [message["id"]]},
        ).json()["changed"]
        == 0
    )
    assert (
        client.post(base, headers=admin_headers, json={"body": "   "}).status_code
        == 400
    )
    assert client.get(f"/v1/worlds/{world['id']}/agents/{bob['id']}/operator/messages").status_code == 401
