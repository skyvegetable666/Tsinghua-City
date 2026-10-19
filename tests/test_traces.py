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
