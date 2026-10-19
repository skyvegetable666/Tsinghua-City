"""Durable run lifecycle, research snapshots, and capability boundaries."""

import json
from dataclasses import replace

import pytest
from conftest import agent_headers, create_agent, create_world
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_docker_runtime import FakeDocker

from tsinghua_city import experiment_registry as registry
from tsinghua_city.api import create_app
from tsinghua_city.maintenance import host_lock
from tsinghua_city.models import Account, Agent, AgentRuntime, ExperimentRun
from tsinghua_city.scheduler import tick_once
from tsinghua_city.service import DomainError


def conditions(count=2):
    return {
        "name": "trial",
        "agents": [
            {"name": f"peer{i}", "model": "test", "prompt": "Inspect your world."}
            for i in range(count)
        ],
    }


def enqueue(client, admin, count=2, repeat=1):
    response = client.post(
        "/v1/experiments", headers=admin, json={"manifest": conditions(count), "repeat": repeat}
    )
    assert response.status_code == 202, response.text
    return response.json()


@pytest.fixture
def adapter(client):
    adapter = FakeDocker()
    client.app.state.runtime_manager._adapters["docker"] = adapter
    return adapter


def test_durable_trials_complete_once_and_freeze_metrics(client, admin_headers, adapter):
    app = client.app
    created = enqueue(client, admin_headers, repeat=2)
    assert len({row["batch_id"] for row in created}) == 1
    assert adapter.starts == 0
    tick_once(app.state.session_factory, app.state.runtime_manager)
    assert adapter.starts == 4
    rows = client.get("/v1/experiments", headers=admin_headers).json()
    assert {r["status"] for r in rows} == {"RUNNING"}
    assert len({r["world_id"] for r in rows}) == 2
    assert all(r["provenance"]["source_sha256"] for r in rows)
    adapter.exit_code = 0
    adapter.running = False
    adapter.runtimes.clear()
    tick_once(app.state.session_factory, app.state.runtime_manager)
    original = client.get("/v1/experiments/" + created[0]["id"], headers=admin_headers).json()
    assert original["status"] == "COMPLETED"
    with app.state.session_factory() as session:
        account = session.scalar(
            select(Account).where(Account.world_id == original["world_id"], Account.kind == "agent")
        )
        account.balance += 500
        session.commit()
    tick_once(app.state.session_factory, app.state.runtime_manager)
    assert adapter.starts == 4
    final = client.get("/v1/experiments/" + created[0]["id"], headers=admin_headers).json()
    assert final["metrics"] == original["metrics"]
    compare = client.get(
        "/v1/experiments/compare?ids=" + ",".join(r["id"] for r in rows), headers=admin_headers
    )
    assert compare.status_code == 200
    assert len(compare.json()) == 2
    exported = client.get(f"/v1/experiments/{created[0]['id']}/export", headers=admin_headers)
    assert exported.json()["metrics"] == original["metrics"]
    assert exported.json()["events"]
    csv = client.get(f"/v1/experiments/{created[0]['id']}/export?format=csv", headers=admin_headers)
    assert csv.text.startswith("experiment_id,metric,value")
    assert "token_hash" not in exported.text


def test_registry_cleanup_continues_and_retries_second_failure(client, admin_headers, adapter):
    app = client.app
    row = enqueue(client, admin_headers, count=3)[0]
    tick_once(app.state.session_factory, app.state.runtime_manager)
    stop = adapter.stop
    attempts = []

    def fail_second(runtime):
        attempts.append(runtime.id)
        if len(attempts) == 2:
            raise DomainError("private-key-value", 503)
        return stop(runtime)

    adapter.stop = fail_second
    path = "/v1/experiments/" + row["id"]
    assert client.post(path + "/stop", headers=admin_headers).json()["status"] == "STOPPING"
    tick_once(app.state.session_factory, app.state.runtime_manager)
    assert len(attempts) == 3
    assert client.get(path, headers=admin_headers).json()["status"] == "STOPPING"
    assert len(adapter.runtimes) == 1
    tick_once(app.state.session_factory, app.state.runtime_manager)
    assert client.get(path, headers=admin_headers).json()["status"] == "STOPPED"
    assert len(attempts) == 4
    assert not adapter.runtimes


def test_interrupted_start_is_cleaned_without_prompt_replay(client, admin_headers, adapter):
    app = client.app
    row = enqueue(client, admin_headers)[0]
    tick_once(app.state.session_factory, app.state.runtime_manager)
    with app.state.session_factory() as session:
        session.get(ExperimentRun, row["id"]).status = "STARTING"
        session.commit()
    tick_once(app.state.session_factory, app.state.runtime_manager)
    result = client.get("/v1/experiments/" + row["id"], headers=admin_headers).json()
    assert result["status"] == "FAILED"
    assert adapter.starts == 2
    assert not adapter.runtimes


def test_failed_launch_cleanup_and_queued_stop(client, admin_headers, adapter):
    app = client.app
    rows = enqueue(client, admin_headers, repeat=2)
    path = "/v1/experiments/" + rows[1]["id"]
    client.post(path + "/stop", headers=admin_headers).raise_for_status()
    start = adapter.start

    def fail_second(**kw):
        if adapter.starts:
            raise RuntimeError("private-error-text")
        return start(**kw)

    adapter.start = fail_second
    tick_once(app.state.session_factory, app.state.runtime_manager)
    failed = client.get("/v1/experiments/" + rows[0]["id"], headers=admin_headers).json()
    assert failed["status"] == "FAILED"
    assert "private-error-text" not in json.dumps(failed)
    assert not adapter.runtimes
    assert adapter.starts == 1
    assert client.get(path, headers=admin_headers).json()["status"] == "STOPPED"


def test_host_ownership_and_maintenance_exclusion(client, admin_headers, adapter):
    app = client.app
    row = enqueue(client, admin_headers)[0]
    tick_once(app.state.session_factory, app.state.runtime_manager)
    with app.state.session_factory() as session:
        run = session.get(ExperimentRun, row["id"])
        runtime = session.scalar(select(AgentRuntime).where(AgentRuntime.world_id == run.world_id))
        runtime.owner_host = "other-host"
        run.owner_host = "other-host"
        base = f"/v1/worlds/{run.world_id}/agents/{runtime.agent_id}/runtime"
        session.get(Agent, runtime.agent_id).death_at = 0
        session.commit()
    assert client.post(base + "/stop", headers=admin_headers).status_code == 409
    assert (
        tick_once(app.state.session_factory, app.state.runtime_manager)["stopped_dead_runtimes"]
        == 0
    )
    with host_lock(app.state.session_factory, app.state.settings) as locked:
        assert locked
        assert tick_once(app.state.session_factory, app.state.runtime_manager) == {
            "maintenance_busy": 1
        }
        assert client.post(base + "/stop", headers=admin_headers).status_code == 409


def test_role_boundaries_and_batch_validation(engine, settings):
    settings = replace(settings, viewer_token="view-access", operator_token="operate-access")
    app = create_app(settings, engine=engine)
    with TestClient(app) as client:
        view = {"Authorization": "Bearer view-access"}
        operate = {"Authorization": "Bearer operate-access"}
        assert client.get("/v1/experiments").status_code == 401
        assert client.get("/v1/experiments", headers=view).status_code == 200
        assert (
            client.post(
                "/v1/experiments", headers=view, json={"manifest": conditions()}
            ).status_code
            == 403
        )
        assert client.post("/v1/worlds", headers=operate, json={"name": "bad"}).status_code == 403
        assert len(enqueue(client, operate)) == 1
        assert (
            client.post(
                "/v1/experiments", headers=operate, json={"manifest": conditions(2), "repeat": 51}
            ).status_code
            == 422
        )
        assert client.get("/experiments").status_code == 200
        assert client.get("/v1/experiments/compare?ids=missing", headers=view).status_code == 404


def test_trace_measurements_deduplicate_and_ignore_malformed_records(settings, tmp_path):
    path = tmp_path / "trace.jsonl"
    events = [
        {"type": "turn_result", "cost_usd": 0.1},
        {"type": "turn_result", "cost_usd": 0.25},
        {"type": "tool_use", "tool_use_id": "tool1"},
        {"type": "tool_use", "tool_use_id": ["malformed"]},
        {"type": "turn_result", "cost_usd": True},
        {"type": "turn_result", "cost_usd": 100, "run_id": []},
    ]
    lines = [json.dumps({"experiment_id": "exp-test", "run_id": "run1", **e}) for e in events]
    path.write_text("\n".join(lines) + "\nnot-json\n[]\n")
    (tmp_path / "duplicate.jsonl").write_text(path.read_text())
    result = registry.trace_metrics(replace(settings, trace_dir=str(tmp_path)), "exp-test")
    assert result["reported_cost_usd"] == 0.25
    assert result["tool_calls"] == 1


def test_employer_escrow_refund_idempotency_and_group_membership(client, admin_headers):
    world = create_world(client, admin_headers)
    alice, bob, carol = [
        create_agent(client, admin_headers, world["id"], name) for name in ("alice", "bob", "carol")
    ]
    base = f"/v1/worlds/{world['id']}"
    employer = client.post(
        base + "/employers", headers=admin_headers, json={"name": "lab", "budget": 20}
    ).json()
    url = base + f"/employers/{employer['id']}/tasks"
    task = {
        "title": "job",
        "description": "work",
        "acceptance_criteria": "done",
        "reward": 15,
        "client_task_id": "unique",
    }
    first = client.post(url, headers=admin_headers, json=task)
    assert first.status_code == 201, first.text
    assert client.post(url, headers=admin_headers, json=task).json()["id"] == first.json()["id"]
    assert (
        client.post(
            url, headers=admin_headers, json={**task, "client_task_id": "overspend"}
        ).status_code
        == 409
    )
    task_url = base + "/tasks/" + first.json()["id"]
    assert client.post(task_url + "/cancel", headers=admin_headers).status_code == 200
    assert client.get(base + "/employers", headers=admin_headers).json()[0]["balance"] == 20
    ah = agent_headers(alice)
    group = client.post(
        "/v1/me/conversations", headers=ah, json={"name": "team", "members": ["bob"]}
    ).json()
    path = f"/v1/me/conversations/{group['id']}/messages"
    body = {"body": "hello", "client_message_id": "m1"}
    sent = client.post(path, headers=ah, json=body)
    assert sent.status_code == 201
    assert client.post(path, headers=ah, json=body).json() == sent.json()
    assert client.post(path, headers=ah, json={**body, "body": "changed"}).status_code == 409
    assert len(client.get(path, headers=agent_headers(bob)).json()) == 1
    assert client.get(path, headers=agent_headers(carol)).status_code == 403
    other = create_world(client, admin_headers, name="other")
    outsider = create_agent(client, admin_headers, other["id"], "outsider")
    assert client.get(path, headers=agent_headers(outsider)).status_code == 404
    assert client.get("/v1/me/conversations", headers=agent_headers(carol)).json() == []


def test_employer_settlement_and_expiry_refund(client, admin_headers):
    from tsinghua_city.models import Task

    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    base = f"/v1/worlds/{world['id']}"
    employer = client.post(base + "/employers", headers=admin_headers,
                           json={"name": "lab", "budget": 20}).json()
    fields = {"title": "work", "description": "work", "acceptance_criteria": "done", "reward": 12}
    path = base + f"/employers/{employer['id']}/tasks"
    task = client.post(path, headers=admin_headers, json=fields).json()
    me = "/v1/me/tasks/" + task["id"]
    ah = agent_headers(alice)
    client.post(me + "/claim", headers=ah).raise_for_status()
    client.post(me + "/submit", headers=ah, json={"submission": "finished"}).raise_for_status()
    client.post(base + f"/tasks/{task['id']}/accept", headers=admin_headers).raise_for_status()
    assert client.get("/v1/me/balance", headers=ah).json()["balance"] == 112
    assert client.get(base + "/employers", headers=admin_headers).json()[0]["balance"] == 8
    expiring = client.post(path, headers=admin_headers,
                           json={**fields, "reward": 8, "deadline_seconds": 60}).json()
    with client.app.state.session_factory() as session:
        session.get(Task, expiring["id"]).deadline_at = 0
        session.commit()
    tick_once(client.app.state.session_factory, client.app.state.runtime_manager)
    assert client.get(base + "/employers", headers=admin_headers).json()[0]["balance"] == 8
