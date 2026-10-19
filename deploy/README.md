# Deploy

## Claude agent sandbox

~~~bash
docker build -f deploy/agent.Dockerfile -t tsinghua-city-agent:0.1 .
~~~

The image:

- pins Claude Code 2.1.296
- runs as non-root uid 1000
- keeps application code read-only to the agent
- uses /work for private agent files, native Claude sessions, and transcripts

The Docker runtime adapter supplies each agent with:

1. an MCP config generated inside the container
2. a rotated World API token and explicitly selected provider credentials
3. a private persistent workspace volume
4. an independent bridge network with access to the World API and model provider
5. read-only root, dropped capabilities, CPU/RAM/PID limits, and transcript export

Do not mount the host admin token into the container.

## Status

The official CLI owns the model loop; the existing stream-json harness captures
messages and tool calls. See [setup and validation](../docs/SANDBOX.md). Select
`"adapter": "docker"` in the runtime start API. The local adapter remains a host
process. This is a single-node research setup with a trusted Docker operator.
