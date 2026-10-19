# First-product Claude agent sandbox.
# Build from the repository root:
#   docker build -f deploy/agent.Dockerfile -t tsinghua-city-agent .
#
# The runtime adapter must mount a container-specific MCP config and a private
# workspace. This image does not contain a host-generated config.

FROM node:22-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    HOME=/home/agent \
    CITY_AGENT_WORKDIR=/workspace \
    CITY_TRACE_DIR=/workspace/transcripts

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        jq \
        procps \
        python3 \
        python3-pip \
        python3-venv \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

# Pin the agent runtime so an npm release cannot silently change experiments.
RUN npm install -g @anthropic-ai/claude-code@2.1.296 \
    && npm cache clean --force

# The node base image already has uid 1000; replace it with our agent user.
RUN userdel -r node 2>/dev/null; groupdel node 2>/dev/null; true \
    && useradd --create-home --uid 1000 --shell /bin/bash agent

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY tsinghua_city /app/tsinghua_city
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir /app

ENV PATH="/opt/venv/bin:$PATH"

RUN mkdir -p /workspace /workspace/transcripts \
    && chown -R agent:agent /workspace \
    && chmod -R a+rX /app

USER agent
WORKDIR /workspace

ENTRYPOINT ["/opt/venv/bin/python", "-m", "tsinghua_city.claude_agent"]
