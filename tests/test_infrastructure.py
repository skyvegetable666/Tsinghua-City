"""Virtual-key lifecycle, fail-closed policy settings, retention, and restore."""

import json
import os
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from conftest import create_agent, create_world
from sqlalchemy import select
from test_docker_runtime import FakeDocker

from tsinghua_city import model_budget
from tsinghua_city.archives import copy_sqlite, prune
from tsinghua_city.auth import token_hash
from tsinghua_city.config import load_settings, utcnow
from tsinghua_city.db import upgrade_database
from tsinghua_city.docker_runtime import DockerRuntimeAdapter
from tsinghua_city.models import ExperimentRun, ModelGrant
from tsinghua_city.service import DomainError
from tsinghua_city.source_fingerprint import source_hash, verify_image


def bounded_command():
    return [
        "python",
        "-m",
        "tsinghua_city.claude_agent",
        "--model",
        "city-model",
        "--max-budget-usd",
        "0.5",
        "--run-timeout",
        "60",
    ]


def test_virtual_key_grant_revoke_and_retry(client, admin_headers, monkeypatch):
    manager = client.app.state.runtime_manager
    manager.settings = replace(
        manager.settings,
        model_gateway_url="http://gateway.test",
        model_gateway_agent_url="http://agent-gateway.test",
        model_gateway_admin_key="master-private",
        require_gateway_budget=True,
    )
    adapter = FakeDocker()
    start = adapter.start
    environments = []

    def capture(**kw):
        environments.append(kw["provider_env"])
        return start(**kw)

    adapter.start = capture
    manager._adapters["docker"] = adapter
    calls = []
    fail_revoke = False

    def respond(request):
        nonlocal fail_revoke
        assert request.headers["Authorization"] == "Bearer master-private"
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path.endswith("generate"):
            return httpx.Response(200, json={"key": "virtual-private"})
        return httpx.Response(503 if fail_revoke else 200, json={"deleted_keys": body["keys"]})

    original = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: original(
            **kw,
            transport=httpx.MockTransport(respond),
        ),
    )
    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    path = f"/v1/worlds/{world['id']}/agents/{alice['id']}/runtime"
    launched = client.post(
        path + "/start",
        headers=admin_headers,
        json={"adapter": "docker", "command": bounded_command()},
    )
    assert launched.status_code == 200, launched.text
    assert calls[0][1]["max_budget"] == 0.5
    assert calls[0][1]["models"] == ["city-model"]
    assert calls[0][1]["duration"] == "120s"
    assert environments == [
        {
            "ANTHROPIC_AUTH_TOKEN": "virtual-private",
            "ANTHROPIC_BASE_URL": "http://agent-gateway.test",
        }
    ]
    with client.app.state.session_factory() as session:
        grant = session.get(ModelGrant, launched.json()["id"])
        assert grant.token_hash == token_hash("virtual-private")
        assert grant.owner_host == manager.settings.runtime_host_id
    fail_revoke = True
    assert client.post(path + "/stop", headers=admin_headers).status_code == 200
    assert model_budget.cleanup(client.app.state.session_factory, manager.settings) == 1
    fail_revoke = False
    assert model_budget.cleanup(client.app.state.session_factory, manager.settings) == 0
    with client.app.state.session_factory() as session:
        assert session.scalars(select(ModelGrant)).all() == []
    assert calls[-1][1] == {"keys": [token_hash("virtual-private")]}
    assert "virtual-private" not in launched.text
    assert "master-private" not in launched.text


def test_required_gateway_and_storage_fail_closed(settings, session_factory):
    with session_factory() as session, pytest.raises(DomainError, match="not configured"):
        model_budget.issue(
            session, replace(settings, require_gateway_budget=True), "rt-test", bounded_command()
        )
    with pytest.raises(DomainError, match="storage quota"):
        DockerRuntimeAdapter(replace(settings, docker_storage_quota_required=True))
    bounded = replace(
        settings,
        docker_storage_quota_required=True,
        docker_volume_options=json.dumps(
            {"type": "tmpfs", "device": "tmpfs", "o": "size=64m,uid=1000,gid=1000"}
        ),
    )
    assert DockerRuntimeAdapter(bounded).volume_options["type"] == "tmpfs"


def test_source_hash_detects_dirty_changes_and_stale_images(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("project")
    (tmp_path / "README.md").write_text("readme")
    (tmp_path / "tsinghua_city").mkdir()
    code = tmp_path / "tsinghua_city" / "agent.py"
    code.write_text("old")
    old = source_hash(tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            [],
            0,
            stdout=old + "\n",
            stderr="",
        ),
    )
    assert verify_image("image-test", tmp_path) == old
    code.write_text("new-uncommitted")
    with pytest.raises(ValueError, match="built from the current working source"):
        verify_image("image-test", tmp_path)


def test_sqlite_backup_restore_preserves_data_without_overwrite(tmp_path):
    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    restored = tmp_path / "restored.db"
    upgrade_database("sqlite:///" + str(source))
    with sqlite3.connect(source) as conn:
        conn.execute("insert into worlds values ('world', 'history', 'active', '{}', 1)")
    copy_sqlite(source, backup)
    copy_sqlite(backup, restored)
    with sqlite3.connect(restored) as conn:
        assert conn.execute("select name from worlds").fetchone() == ("history",)
    assert restored.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        copy_sqlite(backup, restored)


def test_retention_preserves_history_active_files_and_symlinks(client, admin_headers, tmp_path):
    settings = client.app.state.settings
    root = Path(settings.trace_dir)
    root.mkdir(parents=True)
    world = create_world(client, admin_headers)
    old = utcnow() - 100 * 86400
    expired = root / "expired.jsonl"
    recent = root / "recent.jsonl"
    foreign = tmp_path / "foreign.jsonl"
    payload = json.dumps({"world_id": world["id"], "type": "run_started"})
    for path in (expired, recent, foreign):
        path.write_text(payload + "\n")
    os.utime(expired, (old, old))
    link = root / "symlink.jsonl"
    link.symlink_to(foreign)
    with client.app.state.session_factory() as session:
        session.add(
            ExperimentRun(
                id="exp-old",
                name="old",
                batch_id="batch-old",
                status="COMPLETED",
                owner_host=settings.runtime_host_id,
                world_id=world["id"],
                manifest={},
                provenance={},
                measurements={"agents": 0},
                created_at=old,
                finished_at=old,
            )
        )
        session.commit()
        result = prune(session, settings)
        assert result["files"] == [str(expired)]
        assert expired.exists()
        prune(session, settings, apply=True)
        assert session.get(ExperimentRun, "exp-old").measurements == {"agents": 0}
    assert not expired.exists()
    assert recent.exists() and foreign.exists() and link.is_symlink()


def test_secret_file_settings(monkeypatch, tmp_path):
    secret = tmp_path / "admin"
    secret.write_text("file-private\n")
    monkeypatch.setenv("CITY_ADMIN_TOKEN_FILE", str(secret))
    assert load_settings().admin_token == "file-private"


def test_metrics_require_access_and_avoid_identity_labels(client, admin_headers):
    assert client.get("/metrics").status_code == 401
    response = client.get("/metrics", headers=admin_headers)
    assert response.status_code == 200
    assert 'city_experiments_by_status{status="QUEUED"}' in response.text
    assert "token" not in response.text and "agent_id=" not in response.text
