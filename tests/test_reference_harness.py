"""MCP content decoding: list tools arrive as multiple TextContent items."""

from __future__ import annotations

import json
from types import SimpleNamespace

from tsinghua_city.reference_harness import decode_tool_content


def _text(payload) -> SimpleNamespace:
    return SimpleNamespace(text=json.dumps(payload))


def test_list_tool_decodes_all_content_items():
    items = [_text({"id": "t1"}), _text({"id": "t2"})]
    assert decode_tool_content("list_tasks", items) == [{"id": "t1"}, {"id": "t2"}]
    assert decode_tool_content("get_inbox", []) == []


def test_non_list_tool_returns_single_object():
    assert decode_tool_content("check_state", [_text({"balance": 100})]) == {"balance": 100}
