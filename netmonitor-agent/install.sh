#!/usr/bin/env bash
# SpanGate Network Monitor Agent — Linux/Raspberry Pi Installer
# Installs the agent as a systemd service that starts automatically on boot.

set -euo pipefail

INSTALL_DIR="/opt/spangate/netmonitor"
CONFIG_DIR="/etc/spangate/netmonitor"
SERVICE_NAME="spangate-netmonitor"
AGENT_ENTRY="agent.py"
MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=9

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # no colour

info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
die()     { error "$*"; exit 1; }

# ── Root check ────────────────────────────────────────────────────────────────
if [[ "$EUID" -ne 0 ]]; then
  die "This installer must be run as root. Try: sudo bash install.sh"
fi

# ── Python version check ──────────────────────────────────────────────────────
info "Checking Python version …"
PYTHON_BIN=""
for candidate in python3 python3.12 python3.11 python3.10 python3.9; do
  if command -v "$candidate" &>/dev/null; then
    version=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    major=$(echo "$version" | cut -d. -f1)
    minor=$(echo "$version" | cut -d. -f2)
    if [[ "$major" -ge "$MIN_PYTHON_MAJOR" && "$minor" -ge "$MIN_PYTHON_MINOR" ]]; then
      PYTHON_BIN="$candidate"
      info "Found Python $version at $(command -v "$candidate")"
      break
    fi
  fi
done

if [[ -z "$PYTHON_BIN" ]]; then
  die "Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ is required but was not found. Install it and re-run this script."
fi

# ── Install Python dependencies ───────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQUIREMENTS="$SCRIPT_DIR/requirements.txt"

if [[ ! -f "$REQUIREMENTS" ]]; then
  die "requirements.txt not found in $SCRIPT_DIR"
fi

info "Installing Python dependencies …"
"$PYTHON_BIN" -m pip install --quiet --upgrade pip
"$PYTHON_BIN" -m pip install --quiet -r "$REQUIREMENTS"
info "Dependencies installed."

# ── Copy agent files to install directory ─────────────────────────────────────
info "Installing agent to $INSTALL_DIR …"
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR"/*.py   "$INSTALL_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"
info "Agent files copied."

# ── Copy config to /etc (only if it doesn't already exist) ───────────────────
info "Setting up config directory …"
mkdir -p "$CONFIG_DIR"

if [[ -f "$CONFIG_DIR/config.yaml" ]]; then
  warn "Config already exists at $CONFIG_DIR/config.yaml — not overwriting."
else
  cp "$SCRIPT_DIR/config.yaml" "$CONFIG_DIR/config.yaml"
  chmod 600 "$CONFIG_DIR/config.yaml"
  info "Config copied to $CONFIG_DIR/config.yaml"
fi

# Symlink config into install dir so agent.py finds it
ln -sf "$CONFIG_DIR/config.yaml" "$INSTALL_DIR/config.yaml"

# ── Create systemd service ────────────────────────────────────────────────────
info "Creating systemd service: $SERVICE_NAME …"

SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=SpanGate Network Monitor Agent
Documentation=https://spangate.com/netmonitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$PYTHON_BIN $INSTALL_DIR/$AGENT_ENTRY
WorkingDirectory=$INSTALL_DIR
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=spangate-netmonitor

[Install]
WantedBy=multi-user.target
EOF

# ── Enable service ────────────────────────────────────────────────────────────
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
info "Service enabled."

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}✓ SpanGate Network Monitor installed successfully.${NC}"
echo ""
echo "  Next steps:"
echo "  1. Edit your config:  nano $CONFIG_DIR/config.yaml"
echo "  2. Add your API key and device list to the config file."
echo "  3. Start the agent:   systemctl start $SERVICE_NAME"
echo "  4. Check logs:        journalctl -u $SERVICE_NAME -f"
echo ""
