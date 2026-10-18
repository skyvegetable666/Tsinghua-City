"""Public bulletin board: publish, list, detail, reply, world isolation."""

from __future__ import annotations

from conftest import ADMIN, agent_headers, create_agent, create_world


def test_post_and_reply_flow(client, admin_headers):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    bob = create_agent(client, admin_headers, world["id"], "bob")

    published = client.post(
        "/v1/me/posts",
        headers=agent_headers(alice),
        json={"title": "hello city", "body": "first post"},
    )
    assert published.status_code == 201, published.text
    post = published.json()
    assert post["author"] == "alice"
    assert post["title"] == "hello city"

    board = client.get("/v1/me/posts", headers=agent_headers(bob))
    assert board.status_code == 200
    assert [p["id"] for p in board.json()] == [post["id"]]

    replied = client.post(
        f"/v1/me/posts/{post['id']}/replies",
        headers=agent_headers(bob),
        json={"body": "hello back"},
    )
    assert replied.status_code == 201, replied.text
    assert replied.json()["author"] == "bob"

    detail = client.get(f"/v1/me/posts/{post['id']}", headers=agent_headers(alice))
    assert detail.status_code == 200
    assert detail.json()["replies"][0]["body"] == "hello back"

    events = client.get(
        f"/v1/worlds/{world['id']}/events",
        headers={"Authorization": "Bearer " + ADMIN},
    ).json()
    kinds = [event["event_type"] for event in events]
    assert "post.published" in kinds
    assert "post.replied" in kinds


def test_board_is_world_scoped_and_validates(client, admin_headers):
    one = create_world(client, admin_headers, name="one")
    two = create_world(client, admin_headers, name="two")
    alice = create_agent(client, admin_headers, one["id"], "alice")
    other = create_agent(client, admin_headers, two["id"], "other")

    post = client.post(
        "/v1/me/posts",
        headers=agent_headers(alice),
        json={"title": "only one", "body": "scoped"},
    ).json()
    assert client.get("/v1/me/posts", headers=agent_headers(other)).json() == []
    missing = client.get(f"/v1/me/posts/{post['id']}", headers=agent_headers(other))
    assert missing.status_code == 404

    empty = client.post(
        "/v1/me/posts",
        headers=agent_headers(alice),
        json={"title": "", "body": "x"},
    )
    assert empty.status_code == 422
