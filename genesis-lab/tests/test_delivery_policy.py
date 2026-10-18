"""The non-interrupting delivery policy, without Docker or a real model."""

from __future__ import annotations

import asyncio
import time

import pytest

from runtime.bridge import BridgeConfig, Runtime


class FakeBroker:
    def __init__(self) -> None:
        self.on_event = None
        self.sent: list[tuple[str, str]] = []
        self.states: list[tuple[str, int, str]] = []
        self.acks: list[dict] = []
        self.traces: list[dict] = []
        self.closed = False

    async def send(self, recipient: str, text: str) -> dict:
        self.sent.append((recipient, text))
        return {"ok": True, "message_id": "m000001", "peer_state": "idle", "queued_at": "now"}

    async def post_state(self, state: str, turn: int, detail: str = "") -> None:
        self.states.append((state, turn, detail))

    async def ack(self, ids, channel, turn, received_at) -> None:
        self.acks.append(
            {"ids": list(ids), "channel": channel, "turn": turn, "received_at": received_at}
        )

    def trace(self, event: dict) -> None:
        self.traces.append(event)

    async def close(self) -> None:
        self.closed = True


class FakeHarness:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.working = False
        self.current_turn = 0
        self.exited = asyncio.Event()

    async def send_user(self, text: str) -> None:
        assert not self.working, "runtime wrote to stdin during an active turn"
        self.working = True
        self.current_turn += 1
        self.sent.append(text)

    def finish_turn(self) -> None:
        self.working = False

    async def stop(self, grace: float = 0) -> None:
        self.exited.set()


def message(message_id: str, text: str, seq: int = 1, kind: str = "chat") -> dict:
    return {
        "id": message_id,
        "seq": seq,
        "sender": "agent-b" if kind == "chat" else "supervisor",
        "recipient": "agent-a",
        "kind": kind,
        "text": text,
        "sent_at": time.time(),
    }


@pytest.fixture
def runtime(tmp_path):
    config = BridgeConfig(
        agent_id="a",
        agent_name="agent-a",
        peer_id="b",
        peer_name="agent-b",
        run_id="run-test",
        broker_url="http://127.0.0.1:1",
        broker_token="token",
        model="mock",
        workdir=tmp_path,
        local_port=0,
    )
    broker = FakeBroker()
    harness = FakeHarness()
    runtime = Runtime(config, broker=broker, harness=harness, enable_http=False)
    runtime.state = "working"
    harness.working = True
    return runtime


async def drain() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def test_hook_delivers_without_touching_stdin(runtime):
    harness = runtime.harness
    broker = runtime.broker
    await runtime.handle_event("message", message("m1", "hello from peer"))
    assert len(runtime.inbox) == 1

    result = await runtime.handle_hook({})
    assert result["hookSpecificOutput"]["hookEventName"] == "PostToolBatch"
    assert "hello from peer" in result["hookSpecificOutput"]["additionalContext"]
    assert harness.sent == []
    assert len(runtime.inbox) == 0
    await drain()
    assert broker.acks[0]["channel"] == "hook"
    assert broker.acks[0]["ids"] == ["m1"]


async def test_idle_message_starts_a_new_turn(runtime):
    runtime.state = "idle"
    runtime.harness.working = False
    await runtime.handle_event("message", message("m2", "are you there?"))
    await drain()
    await asyncio.sleep(0.05)
    assert len(runtime.harness.sent) == 1
    assert "are you there?" in runtime.harness.sent[0]
    assert runtime.state == "working"
    assert runtime.broker.acks[-1]["channel"] == "turn"


async def test_working_agent_is_not_woken(runtime):
    await runtime.handle_event("message", message("m1", "first"))
    assert len(runtime.harness.sent) == 0
    runtime.state = "working"
    await runtime.wake_if_pending()
    assert len(runtime.harness.sent) == 0
    assert len(runtime.inbox) == 1


async def test_batch_order_and_dedupe(runtime):
    for index in range(3):
        await runtime.handle_event("message", message(f"m{index}", f"message {index}", seq=index))
    await runtime.handle_event("message", message("m1", "duplicate", seq=9))
    assert len(runtime.inbox) == 3
    result = await runtime.handle_hook({})
    context = result["hookSpecificOutput"]["additionalContext"]
    assert context.index("message 0") < context.index("message 1") < context.index("message 2")
    assert runtime.inbox.pending() == 0
    await drain()
    assert runtime.broker.acks[-1]["ids"] == ["m0", "m1", "m2"]


async def test_turn_end_delivers_pending(runtime):
    await runtime.handle_event("message", message("m1", "pending"))
    runtime.harness.working = False
    await runtime._on_turn_end({"turn": 1, "stop_reason": "end_turn"})
    assert runtime.state == "working"
    assert len(runtime.harness.sent) == 1
    assert "pending" in runtime.harness.sent[0]
    await drain()
    assert runtime.broker.acks[-1]["channel"] == "turn"


async def test_hook_does_nothing_when_idle(runtime):
    runtime.state = "idle"
    runtime.harness.working = False
    await runtime.handle_event("message", message("m1", "queued"))
    assert await runtime.handle_hook({}) == {}
    assert len(runtime.inbox) == 1


async def test_agent_chat_validation(runtime):
    runtime.state = "working"
    runtime.harness.working = True
    ok = await runtime.agent_chat("agent-b", "hi")
    assert ok["ok"] is True
    assert runtime.broker.sent == [("agent-b", "hi")]

    wrong = await runtime.agent_chat("agent-a", "self")
    assert wrong["ok"] is False
    empty = await runtime.agent_chat("agent-b", "   ")
    assert empty["ok"] is False
    runtime.config.max_message_chars = 5
    too_long = await runtime.agent_chat("agent-b", "123456")
    assert too_long["ok"] is False

    runtime.ending = True
    blocked = await runtime.agent_chat("agent-b", "hi")
    assert blocked["ok"] is False


async def test_shutdown_ending_waits_for_final_turn(runtime):
    runtime.state = "idle"
    runtime.harness.working = False
    runtime.ending = True
    await runtime.handle_event("message", message("s1", "the experiment is over", kind="system"))
    # The runtime should deliver the final notice as one turn, then exit after its result.
    await drain()
    assert len(runtime.harness.sent) == 1
    runtime.harness.finish_turn()
    await runtime._on_turn_end({"turn": 1, "stop_reason": "end_turn"})
    assert runtime.exit_event.is_set()
