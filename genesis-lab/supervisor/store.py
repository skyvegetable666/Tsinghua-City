"""Durable event log for one run.

All writes happen on the broker's asyncio event loop. Each event is one JSON
line, flushed immediately so a crash still leaves a usable research trace.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_iso(ts: float | None = None) -> str:
    ts = time.time() if ts is None else ts
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class RunStore:
    def __init__(self, run_dir: Path, run_id: str) -> None:
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.run_dir / "events.jsonl"
        self._fh = self.events_path.open("a", encoding="utf-8", buffering=1)

    def append(self, event: str, **fields: Any) -> dict:
        ts = time.time()
        record = {"ts": ts, "ts_iso": utc_iso(ts), "run_id": self.run_id, "event": event}
        record.update(fields)
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()
        return record

    def write_json(self, name: str, data: dict) -> Path:
        path = self.run_dir / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        tmp.replace(path)
        return path

    def write_text(self, name: str, text: str) -> Path:
        path = self.run_dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass
