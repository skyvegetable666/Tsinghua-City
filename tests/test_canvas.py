"""Canvas shell and admin board read endpoints used by the Canvas."""

from __future__ import annotations

from conftest import ADMIN, agent_headers, create_agent, create_world


def test_canvas_is_served(client):
    response = client.get("/canvas")
    assert response.status_code == 200
    assert "Tsinghua City Canvas" in response.text
    assert "fetch(" in response.text
    assert response.headers["content-type"].startswith("text/html")


def test_admin_board_read_endpoints(client, admin_headers):
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    bob = create_agent(client, admin_headers, world["id"], "bob")

    post = client.post(
        "/v1/me/posts",
        headers=agent_headers(alice),
        json={"title": "canvas post", "body": "hello"},
    ).json()
    client.post(
        f"/v1/me/posts/{post['id']}/replies",
        headers=agent_headers(bob),
        json={"body": "reply"},
    )

    headers = {"Authorization": "Bearer " + ADMIN}
    listed = client.get(f"/v1/worlds/{world['id']}/posts", headers=headers)
    assert listed.status_code == 200
    assert [p["id"] for p in listed.json()] == [post["id"]]

    detail = client.get(f"/v1/worlds/{world['id']}/posts/{post['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["replies"][0]["body"] == "reply"
