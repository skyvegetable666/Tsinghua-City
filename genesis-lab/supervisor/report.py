"""Offline analysis: transcript.md and metrics.json from events.jsonl."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _iso(ts: float | None) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%H:%M:%S")


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    index = (len(ordered) - 1) * q
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    fraction = index - low
    return round(ordered[low] * (1 - fraction) + ordered[high] * fraction, 3)


def load_events(run_dir: Path) -> list[dict[str, Any]]:
    path = Path(run_dir) / "events.jsonl"
    events: list[dict[str, Any]] = []
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def build_report(run_dir: Path) -> dict[str, Any]:
    run_dir = Path(run_dir)
    events = load_events(run_dir)
    messages: dict[str, dict] = {}
    deliveries: dict[str, dict] = {}
    turns: dict[str, list[dict]] = defaultdict(list)
    agents: dict[str, str] = {}
    started = events[0]["ts"] if events else None
    ended = events[-1]["ts"] if events else None
    run_id = str((events[0].get("run_id") if events else None) or run_dir.name)

    for event in events:
        kind = event.get("event")
        if kind == "agent_registered":
            agents[event.get("agent_id", "?")] = event.get("name", "?")
        elif kind == "message_sent":
            message = event.get("message") or {}
            if message.get("id"):
                messages[message["id"]] = message
        elif kind == "message_delivered":
            deliveries[event.get("message_id")] = event
        elif kind == "agent_trace":
            trace = event.get("trace") or {}
            if trace.get("type") == "turn_result":
                turns[event.get("agent_id", "?")].append(trace)
        elif kind == "run_ended":
            ended = event.get("ts", ended)

    chat = [m for m in messages.values() if m.get("kind") == "chat"]
    system = [m for m in messages.values() if m.get("kind") == "system"]
    delivered = [m for m in messages.values() if m["id"] in deliveries]
    latencies = [float(deliveries[m["id"]].get("latency_s") or 0) for m in delivered]
    network = [
        float(deliveries[m["id"]]["network_s"])
        for m in delivered
        if deliveries[m["id"]].get("network_s") is not None
    ]
    channels = Counter(deliveries[m["id"]].get("channel") or "unknown" for m in delivered)
    by_sender = Counter(m.get("sender") or "?" for m in messages.values() if m.get("kind") == "chat")

    agent_metrics: dict[str, dict] = {}
    for agent_id, name in agents.items():
        records = turns.get(agent_id, [])
        usage_input = sum(int((r.get("usage") or {}).get("input_tokens") or 0) for r in records)
        usage_output = sum(int((r.get("usage") or {}).get("output_tokens") or 0) for r in records)
        cost = sum(float(r.get("cost_usd") or 0) for r in records)
        agent_metrics[agent_id] = {
            "name": name,
            "turns": len(records),
            "input_tokens": usage_input,
            "output_tokens": usage_output,
            "cost_usd": round(cost, 6),
        }

    duration = round((ended - started), 3) if started and ended else None
    metrics = {
        "run_id": run_id,
        "started_at": started,
        "ended_at": ended,
        "duration_s": duration,
        "messages": {
            "chat": len(chat),
            "system": len(system),
            "delivered": len(delivered),
            "pending": len(messages) - len(delivered),
            "by_sender": dict(by_sender),
            "delivery_channels": dict(channels),
            "latency_s": {
                "p50": _percentile(latencies, 0.50),
                "p95": _percentile(latencies, 0.95),
                "max": round(max(latencies), 3) if latencies else None,
            },
            "network_s": {
                "p50": _percentile(network, 0.50),
                "p95": _percentile(network, 0.95),
                "max": round(max(network), 3) if network else None,
            },
        },
        "agents": agent_metrics,
    }

    lines: list[str] = []
    lines.append(f"# Run {run_id}")
    lines.append("")
    if started:
        lines.append(f"- started: {datetime.fromtimestamp(started, tz=UTC).isoformat()}")
    if ended:
        lines.append(f"- ended: {datetime.fromtimestamp(ended, tz=UTC).isoformat()}")
    if duration is not None:
        lines.append(f"- duration: {duration:.1f}s")
    lines.append(
        f"- messages: {len(chat)} chat, {len(system)} system; "
        f"{len(delivered)} delivered, {len(messages) - len(delivered)} pending"
    )
    if latencies:
        lines.append(
            f"- delivery latency: p50={_percentile(latencies, 0.5)}s "
            f"p95={_percentile(latencies, 0.95)}s max={max(latencies):.2f}s"
        )
    lines.append("")
    lines.append("## Conversation")
    lines.append("")
    for message in sorted(messages.values(), key=lambda m: float(m.get("sent_at") or 0)):
        delivery = deliveries.get(message["id"])
        sender = message.get("sender") or "?"
        recipient = message.get("recipient") or "?"
        when = _iso(float(message.get("sent_at") or 0))
        lines.append(f"### {when}  {sender} -> {recipient}  ({message['id']})")
        lines.append("")
        for text_line in (message.get("text") or "").splitlines():
            lines.append(f"> {text_line}")
        if delivery:
            lines.append("")
            lines.append(
                f"_delivered {_iso(float(delivery.get('ts') or 0))} via "
                f"{delivery.get('channel')} (turn {delivery.get('turn')}) "
                f"in {delivery.get('latency_s')}s_"
            )
        elif message.get("kind") == "system":
            lines.append("")
            lines.append("_not delivered_")
        lines.append("")

    if turns:
        lines.append("## Turns")
        lines.append("")
        lines.append("| agent | turn | duration | input | output | cost | stop |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for agent_id, records in sorted(turns.items()):
            for record in records:
                usage = record.get("usage") or {}
                lines.append(
                    f"| {agents.get(agent_id, agent_id)} | {record.get('turn')} | "
                    f"{record.get('duration_ms', 0)}ms | {usage.get('input_tokens', 0)} | "
                    f"{usage.get('output_tokens', 0)} | "
                    f"{float(record.get('cost_usd') or 0):.4f} | {record.get('stop_reason', '')} |"
                )
        lines.append("")

    text = "\n".join(lines).strip() + "\n"
    (run_dir / "transcript.md").write_text(text, encoding="utf-8")
    (run_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    with (run_dir / "messages.jsonl").open("w", encoding="utf-8") as handle:
        for message in sorted(messages.values(), key=lambda m: float(m.get("sent_at") or 0)):
            record = dict(message)
            record["delivery"] = deliveries.get(message["id"])
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return metrics
