#!/usr/bin/env bash
# ============================================================================
# SpanGate Network Monitor Agent — Setup Wizard
# Tested on: Raspberry Pi OS (bookworm/bullseye), Ubuntu 22+, Debian 11+
#
# What this does:
#   1. Checks that Docker is installed and running
#   2. Asks you a few questions (API key, site name)
#   3. Writes config.yaml
#   4. Builds the Docker image and starts the agent
#
# Usage:
#   chmod +x setup.sh && sudo ./setup.sh
# ============================================================================

set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*"; exit 1; }
ask()     { echo -e "${BOLD}$*${RESET}"; }

# ── Banner ────────────────────────────────────────────────────────────────────
clear
echo ""
echo -e "${CYAN}╔═══════════════════════════════════════════════════╗${RESET}"
echo -e "${CYAN}║${RESET}     ${BOLD}SpanGate Network Monitor — Setup Wizard${RESET}      ${CYAN}║${RESET}"
echo -e "${CYAN}╚═══════════════════════════════════════════════════╝${RESET}"
echo ""
echo "  This wizard will create your config file and start the"
echo "  monitoring agent as a Docker container on this machine."
echo ""
echo "  Before you begin, make sure you have your SpanGate API key."
echo "  Find it at: ${CYAN}https://spangate.com/netmonitor/settings${RESET}"
echo ""
read -rp "  Press ENTER to continue..." _

# ── Docker check ──────────────────────────────────────────────────────────────
echo ""
info "Checking Docker..."

if ! command -v docker &>/dev/null; then
  error "Docker is not installed. Install it first:\n  curl -fsSL https://get.docker.com | sh\n  sudo usermod -aG docker \$USER && newgrp docker"
fi

if ! docker info &>/dev/null 2>&1; then
  error "Docker is installed but not running. Start it with:\n  sudo systemctl start docker"
fi

if ! command -v docker compose version &>/dev/null 2>&1; then
  # Fall back to docker-compose v1
  if ! command -v docker-compose &>/dev/null; then
    error "Docker Compose not found. Install it:\n  sudo apt-get install docker-compose-plugin"
  fi
  COMPOSE_CMD="docker-compose"
else
  COMPOSE_CMD="docker compose"
fi

success "Docker $(docker --version | awk '{print $3}' | tr -d ',') is ready."

# ── Gather info ───────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}──────────────────────────────────────────────────────${RESET}"
echo -e "${BOLD}  Step 1 of 2 — API Key & Site Name${RESET}"
echo -e "${BOLD}──────────────────────────────────────────────────────${RESET}"
echo ""

while true; do
  ask "  Paste your SpanGate API key:"
  read -rp "  > " API_KEY
  API_KEY="$(echo "$API_KEY" | tr -d '[:space:]')"
  if [[ "$API_KEY" == spng_* ]] && [[ ${#API_KEY} -gt 10 ]]; then
    success "API key looks good."
    break
  else
    warn "That doesn't look like a SpanGate API key (should start with 'spng_'). Try again."
  fi
done

echo ""
ask "  What should this site be called? (e.g. Main Campus, Building A)"
read -rp "  > " SITE_NAME
SITE_NAME="${SITE_NAME:-My Site}"

echo ""
echo -e "${BOLD}──────────────────────────────────────────────────────${RESET}"
echo -e "${BOLD}  Step 2 of 2 — Ping Settings${RESET}"
echo -e "${BOLD}──────────────────────────────────────────────────────${RESET}"
echo ""
echo "  Tip: You can add devices from the SpanGate dashboard and they"
echo "  will appear here automatically — no need to list them now."
echo ""
ask "  How often should devices be pinged? (seconds, default: 60)"
read -rp "  > " PING_INTERVAL
PING_INTERVAL="${PING_INTERVAL:-60}"
if ! [[ "$PING_INTERVAL" =~ ^[0-9]+$ ]]; then PING_INTERVAL=60; fi

# ── Write config.yaml ─────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.yaml"

echo ""
info "Writing config.yaml..."

cat > "$CONFIG_FILE" <<EOF
agent:
  site_name: "${SITE_NAME}"
  api_url: "https://spangate-site-r81b.vercel.app"
  api_key: "${API_KEY}"
  ping_interval: ${PING_INTERVAL}
  config_pull_interval: 604800   # SSH config backups every 7 days

# Devices are managed from the SpanGate dashboard.
# Add them at https://spangate.com/netmonitor/settings
# They will sync to this agent automatically within 5 minutes.
devices: []
EOF

success "config.yaml written to $CONFIG_FILE"

# ── Build + start ─────────────────────────────────────────────────────────────
echo ""
info "Building Docker image (this may take a few minutes the first time)..."
cd "$SCRIPT_DIR"
docker build -t spangate-agent:latest . 2>&1 | tail -5

echo ""
info "Starting agent container..."
$COMPOSE_CMD up -d

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔═══════════════════════════════════════════════════╗${RESET}"
echo -e "${GREEN}║${RESET}            ${BOLD}Agent is running!${RESET}                      ${GREEN}║${RESET}"
echo -e "${GREEN}╚═══════════════════════════════════════════════════╝${RESET}"
echo ""
echo "  Site name : ${BOLD}${SITE_NAME}${RESET}"
echo "  Container : spangate-agent"
echo ""
echo "  Useful commands:"
echo "    View logs  : docker logs -f spangate-agent"
echo "    Stop agent : cd $SCRIPT_DIR && $COMPOSE_CMD down"
echo "    Restart    : cd $SCRIPT_DIR && $COMPOSE_CMD restart"
echo ""
echo "  Add devices from your dashboard:"
echo "  ${CYAN}https://spangate.com/netmonitor/settings${RESET}"
echo ""
