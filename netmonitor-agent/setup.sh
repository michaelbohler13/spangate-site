#!/usr/bin/env bash
# ============================================================================
# SpanGate Network Monitor Agent — Setup Wizard (Linux / macOS / Raspberry Pi)
#
# Run this ONE command on your Pi, Mac, or Linux box:
#
#   curl -fsSL https://spangate-site-r81b.vercel.app/netmonitor-agent/setup.sh | bash
#
# What this does:
#   1. Checks that Docker is installed (and helps you install it if not)
#   2. Asks for your API key and site name
#   3. Pulls the pre-built image from Docker Hub (no compile step)
#   4. Writes config.yaml and docker-compose.yml
#   5. Starts the agent — restarts automatically on reboot
# ============================================================================

set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*"; exit 1; }

IMAGE="spangate/netmonitor-agent:latest"
SETUP_DIR="$HOME/spangate-agent"

# Optional: pre-fill values from environment variables so the dashboard
# can inject them into the one-liner command shown to users.
#   export SPNG_API_KEY="spng_..."
#   curl -fsSL .../setup.sh | bash
API_KEY="${SPNG_API_KEY:-}"
SITE_NAME="${SPNG_SITE_NAME:-}"

# ── Banner ────────────────────────────────────────────────────────────────────
clear
echo ""
echo -e "${CYAN}╔═══════════════════════════════════════════════════╗${RESET}"
echo -e "${CYAN}║${RESET}     ${BOLD}SpanGate Network Monitor — Setup Wizard${RESET}      ${CYAN}║${RESET}"
echo -e "${CYAN}╚═══════════════════════════════════════════════════╝${RESET}"
echo ""
echo "  Before you begin, copy your API key from the dashboard:"
echo "  ${CYAN}https://spangate.com/netmonitor/settings${RESET}"
echo ""
read -rp "  Press ENTER to continue..." _

# ── Detect OS ─────────────────────────────────────────────────────────────────
OS="$(uname -s)"
IS_LINUX=false; IS_MAC=false
[[ "$OS" == "Linux" ]]  && IS_LINUX=true
[[ "$OS" == "Darwin" ]] && IS_MAC=true

# ── Docker check ──────────────────────────────────────────────────────────────
echo ""
info "Checking for Docker..."

if ! command -v docker &>/dev/null; then
  if $IS_LINUX; then
    echo ""
    warn "Docker not found. Installing now..."
    curl -fsSL https://get.docker.com | sh
    # Add current user to docker group (takes effect next login)
    sudo usermod -aG docker "$USER" 2>/dev/null || true
    # Activate group for this session
    newgrp docker <<INNERSCRIPT
      docker --version
INNERSCRIPT
  elif $IS_MAC; then
    echo ""
    error "Docker Desktop is not installed.\n\n  Download it from: https://www.docker.com/products/docker-desktop\n  Install it, start it, then re-run this script."
  fi
fi

if ! docker info &>/dev/null 2>&1; then
  if $IS_MAC; then
    error "Docker is installed but not running.\n  Open Docker Desktop from your Applications folder and try again."
  else
    error "Docker is installed but not running.\n  Start it with:  sudo systemctl start docker"
  fi
fi

# Prefer 'docker compose' (v2 plugin), fall back to docker-compose (v1)
if docker compose version &>/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose &>/dev/null; then
  COMPOSE="docker-compose"
else
  if $IS_LINUX; then
    info "Installing Docker Compose plugin..."
    sudo apt-get install -y docker-compose-plugin 2>/dev/null || \
      sudo yum install -y docker-compose-plugin 2>/dev/null || \
      error "Could not install docker-compose-plugin. Install it manually."
    COMPOSE="docker compose"
  else
    error "Docker Compose not found. Make sure Docker Desktop is up to date."
  fi
fi

success "Docker is ready."

# ── Gather config ─────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}──────────────────────────────────────────────────────${RESET}"
echo -e "${BOLD}  Step 1 of 2 — Your SpanGate API Key${RESET}"
echo -e "${BOLD}──────────────────────────────────────────────────────${RESET}"
echo ""

if [[ "$API_KEY" == spng_* ]] && [[ ${#API_KEY} -gt 10 ]]; then
  success "API key pre-filled from dashboard."
else
  while true; do
    echo -e "${BOLD}  Paste your API key (starts with spng_):${RESET}"
    read -rp "  > " API_KEY
    API_KEY="$(echo "$API_KEY" | tr -d '[:space:]')"
    if [[ "$API_KEY" == spng_* ]] && [[ ${#API_KEY} -gt 10 ]]; then
      success "API key accepted."
      break
    fi
    warn "That doesn't look right — the key should start with 'spng_'. Try again."
  done
fi

echo ""
echo -e "${BOLD}──────────────────────────────────────────────────────${RESET}"
echo -e "${BOLD}  Step 2 of 2 — Site Name & Ping Interval${RESET}"
echo -e "${BOLD}──────────────────────────────────────────────────────${RESET}"
echo ""
if [ -n "$SITE_NAME" ]; then
  success "Site name pre-filled: $SITE_NAME"
else
  echo -e "${BOLD}  What should this site be called?${RESET}"
  echo "  (e.g. Main Campus, District Office, Elementary School)"
  read -rp "  > " SITE_NAME
  SITE_NAME="${SITE_NAME:-My Site}"
fi

echo ""
echo -e "${BOLD}  Ping interval in seconds? [60]${RESET}"
read -rp "  > " PING_INTERVAL
PING_INTERVAL="${PING_INTERVAL:-60}"
[[ "$PING_INTERVAL" =~ ^[0-9]+$ ]] || PING_INTERVAL=60

# ── Create setup directory ────────────────────────────────────────────────────
mkdir -p "$SETUP_DIR"

# ── Write config.yaml ─────────────────────────────────────────────────────────
echo ""
info "Writing config.yaml..."

cat > "$SETUP_DIR/config.yaml" <<EOF
agent:
  site_name: "${SITE_NAME}"
  api_url: "https://spangate-site-r81b.vercel.app"
  api_key: "${API_KEY}"
  ping_interval: ${PING_INTERVAL}
  config_pull_interval: 604800   # SSH config backups every 7 days

# Devices are managed from the SpanGate dashboard — no need to list them here.
# Add devices at https://spangate.com/netmonitor/settings
# They sync to this agent automatically within 5 minutes.
devices: []
EOF

success "config.yaml created."

# ── Write docker-compose.yml ──────────────────────────────────────────────────
# Linux uses network_mode: host so the container can send ICMP pings to all
# local devices.  macOS Docker Desktop handles this automatically via its VM.
if $IS_LINUX; then
  NETWORK_BLOCK="    network_mode: host"
else
  NETWORK_BLOCK="    # macOS: Docker Desktop routes pings through its VM automatically"
fi

cat > "$SETUP_DIR/docker-compose.yml" <<EOF
services:
  spangate-agent:
    image: ${IMAGE}
    container_name: spangate-agent
    restart: unless-stopped
${NETWORK_BLOCK}
    volumes:
      - ${SETUP_DIR}/config.yaml:/config/config.yaml:ro
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
EOF

success "docker-compose.yml created."

# ── Pull image + start ────────────────────────────────────────────────────────
echo ""
info "Pulling latest agent image from Docker Hub..."
docker pull "$IMAGE"

echo ""
info "Starting SpanGate agent..."
cd "$SETUP_DIR"
$COMPOSE up -d

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔═══════════════════════════════════════════════════╗${RESET}"
echo -e "${GREEN}║${RESET}         ${BOLD}Agent is running — you're all set!${RESET}        ${GREEN}║${RESET}"
echo -e "${GREEN}╚═══════════════════════════════════════════════════╝${RESET}"
echo ""
echo "  Site:      ${BOLD}${SITE_NAME}${RESET}"
echo "  Files:     ${BOLD}${SETUP_DIR}${RESET}"
echo "  Container: spangate-agent  (auto-restarts on reboot)"
echo ""
echo "  Useful commands:"
echo "    View live logs  :  docker logs -f spangate-agent"
echo "    Stop the agent  :  cd ${SETUP_DIR} && ${COMPOSE} down"
echo "    Update image    :  docker pull ${IMAGE} && cd ${SETUP_DIR} && ${COMPOSE} up -d"
echo ""
echo "  Now add devices from your dashboard:"
echo "  ${CYAN}https://spangate.com/netmonitor/settings${RESET}"
echo ""
