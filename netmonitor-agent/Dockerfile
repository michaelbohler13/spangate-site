FROM python:3.11-slim

LABEL org.opencontainers.image.title="SpanGate Network Monitor Agent"
LABEL org.opencontainers.image.description="Lightweight agent that pings devices and ships status to SpanGate"
LABEL org.opencontainers.image.vendor="SpanGate Technology"

WORKDIR /app

# openssh-client is needed by Netmiko for SSH config pulls
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py api_client.py pinger.py ssh_puller.py ./

# config.yaml is bind-mounted at /config/config.yaml at runtime
# (created by setup.sh or placed manually)
CMD ["python", "agent.py", "--config", "/config/config.yaml"]
