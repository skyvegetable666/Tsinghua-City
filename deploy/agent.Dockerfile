# Reuses archive/genesis-lab/image/Dockerfile, the official Claude Code CLI, and
# Agent-Town Docker tooling (MIT; see THIRD_PARTY_NOTICES.md).
FROM node:22-bookworm-slim AS node
FROM python:3.11-slim-bookworm

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false HOME=/work/home \
    PATH=/opt/venv/bin:$PATH CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DISABLE_TELEMETRY=1 DISABLE_ERROR_REPORTING=1 \
    CITY_AGENT_WORKDIR=/work CITY_TRACE_DIR=/work/transcripts CITY_TRACE_STDOUT=1

RUN apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        jq \
        procps \
        ripgrep \
        util-linux \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code@2.1.296 && npm cache clean --force
RUN python -m venv /opt/venv
RUN groupadd --gid 1000 agent \
    && useradd --uid 1000 --gid 1000 --home-dir /work/home --no-create-home agent
WORKDIR /opt/city
COPY pyproject.toml README.md ./
COPY tsinghua_city/ ./tsinghua_city/
RUN pip install --no-cache-dir . \
    && mkdir -p /work/home /shared \
    && chown -R 1000:1000 /work /shared
WORKDIR /work
USER 1000:1000
ENTRYPOINT ["python", "-m", "tsinghua_city.sandbox_entrypoint"]
CMD ["python", "-m", "tsinghua_city.claude_agent"]
