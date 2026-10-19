"""Agent lives and transcript events."""

from __future__ import annotations

from conftest import ADMIN, agent_headers, create_agent, create_world


def test_lifecycle_transcript_and_supersede(client, admin_headers):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    headers = agent_headers(alice)

    first = client.post(
        "/v1/me/lives",
        headers=headers,
        json={"session_id": "session-1", "wake": "remember the garden"},
    )
    assert first.status_code == 201, first.text
    first_life = first.json()
    assert first_life["status"] == "ACTIVE"
    assert first_life["session_id"] == "session-1"

    event = client.post(
        f"/v1/me/lives/{first_life['id']}/events",
        headers=headers,
        json={"kind": "tick", "body": "looked around", "data": {"n": 1}},
    )
    assert event.status_code == 201, event.text
    assert event.json()["kind"] == "tick"

    second = client.post("/v1/me/lives", headers=headers, json={})
    assert second.status_code == 201, second.text
    second_life = second.json()

    listed = client.get("/v1/me/lives", headers=headers)
    assert listed.status_code == 200
    lives = listed.json()
    assert [life["id"] for life in lives] == [second_life["id"], first_life["id"]]
    assert lives[1]["status"] == "ENDED"
    assert lives[1]["end_reason"] == "superseded"

    detail = client.get(f"/v1/me/lives/{first_life['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["wake"] == "remember the garden"
    assert [e["body"] for e in detail.json()["events"]] == ["looked around"]

    ended = client.post(
        f"/v1/me/lives/{second_life['id']}/end",
        headers=headers,
        json={"reason": "done", "diary": "today was useful"},
    )
    assert ended.status_code == 200, ended.text
    assert ended.json()["status"] == "ENDED"

    admin_detail = client.get(
        f"/v1/worlds/{world['id']}/agents/{alice['id']}/lives/{second_life['id']}",
        headers={"Authorization": "Bearer " + ADMIN},
    )
    assert admin_detail.status_code == 200
    assert admin_detail.json()["diary"] == "today was useful"


def test_lives_are_agent_scoped(client, admin_headers):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    bob = create_agent(client, admin_headers, world["id"], "bob")

    life = client.post("/v1/me/lives", headers=agent_headers(alice), json={}).json()

    assert (
        client.post(
            f"/v1/me/lives/{life['id']}/events",
            headers=agent_headers(bob),
            json={"kind": "tick"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/v1/me/lives/{life['id']}/end",
            headers=agent_headers(bob),
            json={},
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/v1/worlds/{world['id']}/agents/{alice['id']}/lives",
            headers={"Authorization": "Bearer wrong"},
        ).status_code
        == 401
    )
