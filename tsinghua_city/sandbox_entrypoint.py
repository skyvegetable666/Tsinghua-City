"""Generate the existing MCP configuration inside the container, then exec argv."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> None:
    os.umask(0o077)
    Path(os.environ["HOME"]).mkdir(parents=True, exist_ok=True)
    path = Path("/tmp/city-mcp.json")
    path.write_text(json.dumps({"mcpServers": {"tsinghua-city": {
        "command": sys.executable, "args": ["-m", "tsinghua_city", "mcp"],
        "env": {key: os.environ[key] for key in (
            "CITY_API_URL", "CITY_AGENT_TOKEN", "CITY_AGENT_ID", "CITY_WORLD_ID"
        )},
    }}}), encoding="utf-8")
    os.environ["CITY_MCP_CONFIG"] = str(path)
    command = sys.argv[1:]
    if not command:
        command = [sys.executable, "-m", "tsinghua_city.claude_agent"]
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
