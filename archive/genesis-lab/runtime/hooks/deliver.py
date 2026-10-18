#!/usr/bin/env python3
"""Claude Code PostToolBatch hook.

Claude Code sends the hook payload as JSON on stdin. This script forwards it to
the local bridge and prints the bridge's JSON response, which Claude Code turns
into an additionalContext system reminder. On any failure it prints {} so the
agent turn is never blocked by the message bus.
"""

from __future__ import annotations

import os
import sys
import urllib.request


def main() -> None:
    raw = sys.stdin.buffer.read()
    base = os.environ.get("AGENT_LOCAL_URL", "http://127.0.0.1:8765").rstrip("/")
    url = os.environ.get("AGENT_HOOK_URL", base + "/hook/post-tool-batch")
    token = os.environ.get("AGENT_LOCAL_TOKEN", "")
    request = urllib.request.Request(
        url,
        data=raw,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            sys.stdout.write(response.read().decode("utf-8"))
    except Exception:
        sys.stdout.write("{}")


if __name__ == "__main__":
    main()
