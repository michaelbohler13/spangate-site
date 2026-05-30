#!/usr/bin/env bash
# SpanGate Network Monitor — Agent Download Script (Linux / Raspberry Pi)
# Usage:
#   curl -fsSL https://spangate-site.vercel.app/netmonitor/agent-install.sh | bash

set -euo pipefail

DEST="$HOME/spangate-agent/netmonitor-agent"
BASE="https://spangate-site.vercel.app/netmonitor-agent"
FILES="agent.py api_client.py pinger.py ssh_puller.py requirements.txt config.yaml"

echo ""
echo "  SpanGate Network Monitor — Downloading agent files..."
echo ""

mkdir -p "$DEST"

for f in $FILES; do
  curl -fsSL "$BASE/$f" -o "$DEST/$f"
  echo "  ✓  $f"
done

echo ""
echo "  Done. Agent files saved to: $DEST"
echo ""
echo "  Next steps:"
echo "    cd ~/spangate-agent/netmonitor-agent"
echo "    python3 -m venv venv && source venv/bin/activate"
echo "    pip install -r requirements.txt"
echo ""
