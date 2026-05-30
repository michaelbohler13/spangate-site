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

COPY agent.py agent_metrics.py api_client.py pinger.py ssh_puller.py ./

# config.yaml is bind-mounted at /config/config.yaml at runtime
CMD ["python", "agent.py", "--config", "/config/config.yaml"]
