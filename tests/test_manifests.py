"""Runtime manifests are secret-free reproducibility records."""

from __future__ import annotations

import sys


def test_runtime_writes_secret_free_manifest(client, admin_headers):
    world = client.post("/v1/worlds", headers=admin_headers, json={"name": "manifest"}).json()
    agent = client.post(
        f"/v1/worlds/{world['id']}/agents", headers=admin_headers, json={"name": "alice"}
    ).json()
    base = f"/v1/worlds/{world['id']}/agents/{agent['id']}/runtime"
    started = client.post(
        base + "/start",
        headers=admin_headers,
        json={"command": [sys.executable, "-c", "import time; time.sleep(5)"]},
    )
    assert started.status_code == 200, started.text

    listed = client.get(
        f"/v1/worlds/{world['id']}/agents/{agent['id']}/manifests", headers=admin_headers
    )
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    manifest = rows[0]["manifest"]
    assert manifest["runtime_id"] == started.json()["id"]
    assert manifest["adapter"] == "local"
    assert manifest["command"][-1] == "import time; time.sleep(5)"
    assert "dev-admin-token" not in str(manifest)
    assert "CITY_ADMIN_TOKEN" not in str(manifest)

    detail = client.get(f"/v1/manifests/{rows[0]['name']}", headers=admin_headers)
    assert detail.status_code == 200
    assert detail.json()["world_id"] == world["id"]

    assert client.post(base + "/stop", headers=admin_headers).status_code == 200
