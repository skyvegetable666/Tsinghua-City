from __future__ import annotations

import json
import stat

import pytest

from tsinghua_city.claude_agent import _run, build_parser


async def test_agent_entrypoint_writes_trace(tmp_path):
    fake = tmp_path / "fake-claude"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "print(json.dumps({'type':'system','subtype':'init','session_id':'s','model':'m'}), flush=True)\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'type':'assistant','message':{'role':'assistant','content':[{'type':'text','text':'ok'}]}}), flush=True)\n"
        "print(json.dumps({'type':'result','stop_reason':'end_turn','is_error':False}), flush=True)\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    mcp = tmp_path / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    trace_dir = tmp_path / "traces"
    args = build_parser().parse_args(
        [
            "--mcp-config",
            str(mcp),
            "--claude-bin",
            str(fake),
            "--trace-dir",
            str(trace_dir),
            "--workdir",
            str(tmp_path / "work"),
            "--agent-name",
            "alice",
            "--agent-id",
            "agent-1",
            "--world-id",
            "world-1",
            "--run-id",
            "run-1",
        ]
    )
    assert await _run(args) == 0
    records = [
        json.loads(line)
        for line in next(trace_dir.glob("*.jsonl")).read_text().splitlines()
    ]
    kinds = [record["type"] for record in records]
    assert "run_started" in kinds
    assert "turn_result" in kinds
    assert records[-1]["type"] == "run_finished"
    assert records[-1]["status"] == 0


@pytest.mark.parametrize("emit_error_result", [True, False])
async def test_agent_reports_provider_error_or_early_exit(tmp_path, emit_error_result):
    fake = tmp_path / "fake-claude"
    script = (
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'type':'system','subtype':'init','session_id':'s'}), flush=True)\n"
    )
    if emit_error_result:
        script += "print(json.dumps({'type':'result','is_error':True}), flush=True)\n"
    fake.write_text(script, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    mcp = tmp_path / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {}}))
    args = build_parser().parse_args([
        "--mcp-config", str(mcp), "--claude-bin", str(fake),
        "--trace-dir", str(tmp_path / "trace"), "--workdir", str(tmp_path / "work"),
        "--ready-timeout", "2", "--turn-timeout", "2",
    ])
    assert await _run(args) == 1
    records = [json.loads(line) for line in next((tmp_path / "trace").glob("*.jsonl"))
               .read_text().splitlines()]
    assert any(record["type"] == "run_error" for record in records)
    assert records[-1]["status"] == 1


@pytest.mark.parametrize("fail_reply,budget", [(False, 5), (True, 5), (False, 0.1)])
async def test_continued_turns_deliver_and_ack_messages_at_safe_boundaries(
    client, admin_headers, tmp_path, monkeypatch, fail_reply, budget,
):
    from conftest import agent_headers, create_agent, create_world

    from tsinghua_city.inbox_delivery import WorldInbox

    world = create_world(client, admin_headers)
    alice = create_agent(client, admin_headers, world["id"], "alice")
    bob = create_agent(client, admin_headers, world["id"], "bob")
    sent = client.post("/v1/me/messages", headers=agent_headers(bob), json={
        "to": "alice", "body": "Would you like to cooperate?",
    }).json()
    mcp = tmp_path / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"tsinghua-city": {"env": {
        "CITY_API_URL": "http://testserver", "CITY_AGENT_TOKEN": alice["token"],
    }}}}))

    def request(self, method, path, body=None):
        response = client.request(method, path, headers=agent_headers(alice), json=body)
        response.raise_for_status()
        return response.json()

    monkeypatch.setattr(WorldInbox, "_request", request)
    inputs = tmp_path / "inputs.jsonl"
    fake = tmp_path / "fake-claude"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "print(json.dumps({'type':'system','subtype':'init','session_id':'s'}), flush=True)\n"
        "for index, line in enumerate(sys.stdin):\n"
        f"    with open({str(inputs)!r}, 'a') as handle: handle.write(line)\n"
        "    print(json.dumps({'type':'result', 'total_cost_usd':0.2, "
        f"'is_error':bool(index and {fail_reply!r})}}), flush=True)\n",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    args = build_parser().parse_args([
        "--mcp-config", str(mcp), "--claude-bin", str(fake),
        "--trace-dir", str(tmp_path / "trace"), "--workdir", str(tmp_path / "work"),
        "--max-rounds", "3", "--listen-seconds", "0", "--max-budget-usd", str(budget),
    ])
    assert await _run(args) == int(fail_reply and budget > 0.2)
    delivered = [json.loads(line) for line in inputs.read_text().splitlines()]
    assert len(delivered) == (1 if budget < 0.2 else 2)
    messages = client.get("/v1/me/messages", headers=agent_headers(alice)).json()
    assert messages[0]["id"] == sent["id"]
    assert (messages[0]["read_at"] is not None) == (not fail_reply and budget > 0.2)
    if len(delivered) == 2:
        assert "Would you like to cooperate?" in delivered[1]["message"]["content"]
    records = [json.loads(line) for line in next((tmp_path / "trace").glob("*.jsonl"))
               .read_text().splitlines()]
    kinds = [record["type"] for record in records]
    if "inbox_delivered" in kinds:
        assert kinds.index("turn_result") < kinds.index("inbox_delivered")


async def test_run_duration_limit_stops_cli(tmp_path):
    fake = tmp_path / "fake-claude"
    fake.write_text(
        "#!/usr/bin/env python3\nimport json, sys\nsys.stdin.readline()\n"
        "print(json.dumps({'type':'system','subtype':'init','session_id':'s'}), flush=True)\n"
        "sys.stdin.read()\n",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    mcp = tmp_path / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {}}))
    args = build_parser().parse_args([
        "--mcp-config", str(mcp), "--claude-bin", str(fake),
        "--trace-dir", str(tmp_path / "trace"), "--workdir", str(tmp_path / "work"),
        "--run-timeout", "0.5",
    ])
    assert await _run(args) == 0
    events = [json.loads(line) for line in next((tmp_path / "trace").glob("*.jsonl"))
              .read_text().splitlines()]
    assert any(event.get("reason") == "duration" for event in events)
    assert events[-1]["status"] == 0


@pytest.mark.parametrize("subtype", ["error_max_budget_usd", "error_max_turns"])
async def test_native_cli_limits_finish_without_failure(tmp_path, subtype):
    fake = tmp_path / "fake-claude"
    fake.write_text(
        "#!/usr/bin/env python3\nimport json, sys\nsys.stdin.readline()\n"
        "print(json.dumps({'type':'system','subtype':'init','session_id':'s'}), flush=True)\n"
        f"print(json.dumps({{'type':'result','is_error':True,'subtype':{subtype!r}}}), flush=True)\n"
        "sys.stdin.read()\n",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    mcp = tmp_path / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {}}))
    args = build_parser().parse_args([
        "--mcp-config", str(mcp), "--claude-bin", str(fake),
        "--trace-dir", str(tmp_path / "trace"), "--workdir", str(tmp_path / "work"),
        "--max-budget-usd", "0.1",
    ])
    assert await _run(args) == 0
    events = [json.loads(line) for line in next((tmp_path / "trace").glob("*.jsonl"))
              .read_text().splitlines()]
    assert any(event["type"] == "run_limit" for event in events)
    assert not any(event["type"] == "run_error" for event in events)
