# Deploy

## Claude agent sandbox

~~~bash
docker build -f deploy/agent.Dockerfile -t tsinghua-city-agent .
~~~

The image:

- pins Claude Code 2.1.296
- runs as non-root uid 1000
- keeps application code read-only to the agent
- uses /workspace for private agent files and transcripts

The Docker runtime adapter now performs the container-side setup:

1. generates an MCP config whose command points at the container Python:
   /opt/venv/bin/python -m tsinghua_city mcp
2. injects the per-agent World API token through that read-only config
3. mounts a named private workspace volume at /workspace
4. maps 127.0.0.1 to host.docker.internal for the World API
5. starts this image with the requested command

The host admin token is not passed into the container.

On Linux Docker Engine, the World API must be reachable from the container.
Bind it to a bridge-accessible address (for example CITY_HOST=0.0.0.0) and
firewall that port; the default 127.0.0.1 bind is reachable through
host.docker.internal on Docker Desktop, but not from a Linux bridge.

## Status

The Dockerfile and adapter command construction are implemented, and the
adapter is tested with a fake Docker CLI. The full container stack has not been
built or run because Docker Desktop WSL integration is unavailable in this
workspace. The local runtime adapter is still the only end-to-end tested path.
