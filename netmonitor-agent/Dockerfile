# Stage 1 — grab Docker CLI + compose plugin binaries
FROM docker:27-cli AS docker-cli

# Stage 2 — main agent image
FROM python:3.11-slim

LABEL org.opencontainers.image.title="SpanGate Network Monitor Agent"
LABEL org.opencontainers.image.description="Lightweight agent that pings devices and ships status to SpanGate"
LABEL org.opencontainers.image.vendor="SpanGate Technology"

WORKDIR /app

# Build tools are needed by cryptography (via paramiko → netmiko) on ARM.
# We install, pip-compile, then purge the build tools to keep the image lean.
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    iputils-ping \
    gcc \
    libssl-dev \
    libffi-dev \
    python3-dev \
    cargo \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# --prefer-binary uses pre-built wheels where available (faster + avoids Rust
# compilation). Falls back to source-build with the tools installed above.
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt

# Purge build tools after pip is done — runtime doesn't need them
RUN apt-get purge -y --auto-remove \
    gcc libssl-dev libffi-dev python3-dev cargo pkg-config \
    && rm -rf /var/lib/apt/lists/* /root/.cargo

# Copy Docker CLI + compose plugin from the docker:cli stage.
# Required for the self-update feature (agent_command_loop).
# The socket must be mounted at runtime: /var/run/docker.sock:/var/run/docker.sock
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/ /usr/local/libexec/docker/cli-plugins/

COPY agent.py agent_metrics.py api_client.py pinger.py ssh_puller.py VERSION ./

# config.yaml is bind-mounted at /config/config.yaml at runtime
CMD ["python", "agent.py", "--config", "/config/config.yaml"]
