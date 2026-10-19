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

The first-product runtime adapter still needs to:

1. generate an MCP config whose command points at the container Python:
   /opt/venv/bin/python -m tsinghua_city mcp
2. inject the per-agent World API token
3. mount a private workspace volume
4. expose the World API to the container
5. start this image with the generated config

Do not mount the host admin token into the container.

## Status

This Dockerfile is a reference artifact until the Docker runtime adapter and
Docker Desktop WSL integration are available. The local runtime adapter is not
a sandbox.
