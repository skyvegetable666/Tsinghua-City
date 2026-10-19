"""Read-only trace listing and viewer endpoints."""

from __future__ import annotations

import json
from pathlib import Path


def test_trace_viewer_and_listing(client, admin_headers):
    root = Path(client.app.state.settings.trace_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "alice-run.jsonl").write_text(
        json.dumps({"type": "run_started", "agent_name": "alice"}) + "\n"
        + "not-json\n",
        encoding="utf-8",
    )

    page = client.get("/traces")
    assert page.status_code == 200
    assert "Tsinghua City" in page.text
    assert "/v1/traces" in page.text

    listed = client.get("/v1/traces", headers=admin_headers)
    assert listed.status_code == 200
    assert listed.json()[0]["name"] == "alice-run.jsonl"

    detail = client.get("/v1/traces/alice-run.jsonl", headers=admin_headers)
    assert detail.status_code == 200
    events = detail.json()["events"]
    assert events[0]["type"] == "run_started"
    assert events[1]["type"] == "raw"


def test_traces_require_admin_and_reject_other_files(client, admin_headers):
    assert client.get("/v1/traces").status_code == 401
    assert client.get("/v1/traces/nope.jsonl", headers=admin_headers).status_code == 404
    assert client.get("/v1/traces/not-a-trace.txt", headers=admin_headers).status_code == 404


def test_docker_trace_ingestion_filtering_and_pagination(client, admin_headers, monkeypatch):
    from tsinghua_city.docker_runtime import DockerRuntimeAdapter
    from tsinghua_city.models import AgentRuntime

    settings = client.app.state.settings
    adapter = DockerRuntimeAdapter(settings)
    runtime = AgentRuntime(id="rt_" + "a" * 24, world_id="world-a", agent_id="alice")
    logs = "trace: /work/transcripts/alice.jsonl\n" + "\n".join(
        json.dumps({"type": "assistant_text", "run_id": "run-a", "text": str(index)})
        for index in range(3)
    ) + "\nprovider stderr\n"
    monkeypatch.setattr(adapter, "_run", lambda args: logs)
    path = Path(settings.runtime_log_dir) / "launch.log"
    for _ in range(2):
        adapter._export_logs("container", path, runtime)
    assert "provider stderr" in path.read_text()
    traces = client.get("/v1/traces?world_id=world-a&agent_id=alice", headers=admin_headers).json()
    assert len(traces) == 1
    assert traces[0]["run_id"] == "run-a"
    assert client.get("/v1/traces?agent_id=bob", headers=admin_headers).json() == []
    first = client.get("/v1/traces/launch.jsonl?limit=2", headers=admin_headers).json()
    assert len(first["events"]) == 2
    assert first["events"][0]["world_id"] == "world-a"
    second = client.get(
        f"/v1/traces/launch.jsonl?offset={first['next_offset']}", headers=admin_headers,
    ).json()
    assert [item["text"] for item in second["events"]] == ["2"]
    assert second["next_offset"] is None
