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
