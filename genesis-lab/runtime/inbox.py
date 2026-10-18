"""Ordered, de-duplicating inbox for messages pushed by the supervisor.

The inbox is plain in-memory state. All methods are called from the single
asyncio event loop of the agent runtime, so no locking is needed here; the
runtime serialises taking messages with an asyncio lock.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Message:
    """One message as delivered by the supervisor's event stream."""

    id: str
    seq: int
    sender: str
    text: str
    sent_at: float
    kind: str = "chat"  # "chat" | "system"
    received_at: float = 0.0
    meta: dict = field(default_factory=dict)

    @property
    def is_system(self) -> bool:
        return self.kind == "system"


class Inbox:
    """FIFO inbox keyed by message id."""

    def __init__(self) -> None:
        self._items: list[Message] = []
        self._seen: set[str] = set()

    def add(self, message: Message) -> bool:
        """Append a message; return False if it was already seen."""
        if message.id in self._seen:
            return False
        self._seen.add(message.id)
        self._items.append(message)
        return True

    def take(self, max_chars: int) -> list[Message]:
        """Remove and return messages in order, staying under an estimated budget.

        At least one message is always returned when the inbox is non-empty, so a
        single oversized message can never starve delivery. The runtime keeps the
        remaining messages for the next safe boundary.
        """
        if not self._items:
            return []
        taken: list[Message] = []
        used = 0
        remaining: list[Message] = []
        for index, message in enumerate(self._items):
            cost = len(message.text) + 160  # per-message header estimate
            if taken and used + cost > max_chars:
                remaining.extend(self._items[index:])
                break
            taken.append(message)
            used += cost
        self._items = remaining
        return taken

    def restore(self, messages: list[Message]) -> None:
        """Put taken messages back at the front, preserving their order.

        Used only when writing to Claude's stdin fails after the messages were
        already removed from the queue.
        """
        for message in reversed(messages):
            self._items.insert(0, message)

    def pending(self) -> int:
        return len(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)
