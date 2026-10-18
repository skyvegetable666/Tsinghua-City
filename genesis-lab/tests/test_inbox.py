from runtime.formatting import format_delivery
from runtime.inbox import Inbox, Message


def make(msg_id: str, text: str, seq: int = 1, kind: str = "chat") -> Message:
    return Message(
        id=msg_id,
        seq=seq,
        sender="agent-b" if kind == "chat" else "supervisor",
        text=text,
        sent_at=1_700_000_000.0,
        kind=kind,
    )


def test_dedupes_by_id():
    inbox = Inbox()
    assert inbox.add(make("m1", "hello")) is True
    assert inbox.add(make("m1", "hello again")) is False
    assert inbox.pending() == 1


def test_fifo_order_and_take():
    inbox = Inbox()
    for i in range(3):
        inbox.add(make(f"m{i}", f"message {i}", seq=i))
    taken = inbox.take(10_000)
    assert [m.id for m in taken] == ["m0", "m1", "m2"]
    assert inbox.pending() == 0


def test_take_respects_budget_but_keeps_order():
    inbox = Inbox()
    inbox.add(make("m0", "x" * 100))
    inbox.add(make("m1", "y" * 100))
    inbox.add(make("m2", "z" * 100))
    taken = inbox.take(300)
    assert [m.id for m in taken] == ["m0"]
    assert inbox.pending() == 2
    rest = inbox.take(10_000)
    assert [m.id for m in rest] == ["m1", "m2"]


def test_restore_preserves_order():
    inbox = Inbox()
    inbox.add(make("m0", "first"))
    inbox.add(make("m1", "second"))
    taken = inbox.take(10_000)
    inbox.add(make("m2", "newer"))
    inbox.restore(taken)
    assert [m.id for m in inbox.take(10_000)] == ["m0", "m1", "m2"]


def test_take_always_returns_one_message():
    inbox = Inbox()
    inbox.add(make("m0", "x" * 5000))
    taken = inbox.take(10)
    assert [m.id for m in taken] == ["m0"]
    assert inbox.pending() == 0


def test_format_contains_headers_and_content():
    msgs = [make("m1", "first"), make("m2", "second")]
    text = format_delivery(msgs, channel="hook", peer_name="agent-b")
    assert "id=m1" in text
    assert "id=m2" in text
    assert "first" in text and "second" in text
    assert "between your tool calls" in text


def test_format_system_notice():
    text = format_delivery([make("s1", "world ending", kind="system")], channel="turn", peer_name="agent-b")
    assert "SYSTEM NOTICE" in text
    assert "world ending" in text


def test_format_hard_cap():
    text = format_delivery([make("m1", "x" * 20_000)], channel="hook", peer_name="agent-b", max_chars=1000)
    assert len(text) <= 1000
    assert "truncated by supervisor" in text
