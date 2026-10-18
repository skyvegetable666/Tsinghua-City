FROM node:22-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    HOME=/home/agent \
    PYTHONPATH=/opt/genesis

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

# Pin the harness so a surprise npm release cannot change the experiment.
RUN npm install -g @anthropic-ai/claude-code@2.1.296 \
    && npm cache clean --force

# The node base image ships a `node` user at uid 1000; remove it so `agent` can take that uid.
RUN userdel -r node 2>/dev/null; groupdel node 2>/dev/null; true \
    && useradd --create-home --uid 1000 --shell /bin/bash agent

WORKDIR /opt/genesis
COPY runtime/requirements.txt /opt/genesis/requirements.txt
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /opt/genesis/requirements.txt

ENV PATH="/opt/venv/bin:${PATH}"

COPY runtime/ /opt/genesis/runtime/
RUN chmod -R a+rX /opt/genesis

RUN mkdir -p /work && chown agent:agent /work
WORKDIR /work

USER agent

ENTRYPOINT ["/opt/venv/bin/python", "-m", "runtime.bridge"]
