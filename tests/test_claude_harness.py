from __future__ import annotations

import json
import stat

from tsinghua_city.claude_harness import ClaudeHarness


async def test_harness_with_fake_cli(tmp_path):
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
    events = []
    harness = ClaudeHarness(
        run_id="run-1",
        agent_id="agent-1",
        agent_name="alice",
        model="fake",
        world_prompt="",
        workdir=tmp_path / "work",
        mcp_config=mcp,
        trace=events.append,
        claude_bin=str(fake),
    )
    await harness.start()
    await harness.send_initial("hello")
    await harness.wait_ready(timeout=5)
    await harness.wait_for_turn(timeout=5)
    assert {event["type"] for event in events} >= {
        "session_initialized",
        "turn_started",
        "assistant_text",
        "turn_result",
    }
    command = harness._command()
    assert "--mcp-config" in command
    assert str(mcp) in command
    assert "--strict-mcp-config" in command
    assert "--settings" not in command
    assert "--max-turns" in command
    assert "--tools" in command
    harness.resume_session = "existing-session"
    resumed = harness._command()
    assert resumed[resumed.index("--resume") + 1] == "existing-session"
    assert "--session-id" not in resumed
    harness.max_budget_usd = 0.75
    bounded = harness._command()
    assert bounded[bounded.index("--max-budget-usd") + 1] == "0.75"
