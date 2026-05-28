# ============================================================================
# SpanGate Network Monitor Agent — Setup Wizard (Windows)
#
# Run this ONE command in PowerShell (as Administrator):
#
#   irm https://raw.githubusercontent.com/michaelbohler13/spangate-site/main/netmonitor-agent/setup.ps1 | iex
#
# What this does:
#   1. Checks that Docker Desktop is installed and running
#   2. Asks for your API key and site name
#   3. Pulls the pre-built image from Docker Hub (no compile step)
#   4. Writes config.yaml and docker-compose.yml
#   5. Starts the agent — restarts automatically on reboot
# ============================================================================

$ErrorActionPreference = 'Stop'

$IMAGE     = 'spangate/netmonitor-agent:latest'
$SETUP_DIR = "$env:USERPROFILE\spangate-agent"
$DOCS_URL  = 'https://spangate.com/netmonitor/settings'

# ── Helper functions ──────────────────────────────────────────────────────────
function Write-Info    { param($m) Write-Host "[INFO]  $m" -ForegroundColor Cyan }
function Write-OK      { param($m) Write-Host "[OK]    $m" -ForegroundColor Green }
function Write-Warn    { param($m) Write-Host "[WARN]  $m" -ForegroundColor Yellow }
function Write-Fail    { param($m) Write-Host "[ERROR] $m" -ForegroundColor Red; exit 1 }
function Write-Step    { param($m) Write-Host "`n$m" -ForegroundColor White }

# ── Banner ────────────────────────────────────────────────────────────────────
Clear-Host
Write-Host ""
Write-Host "  ╔═══════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "  ║     SpanGate Network Monitor — Setup Wizard       ║" -ForegroundColor Cyan
Write-Host "  ╚═══════════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Before you begin, copy your API key from the dashboard:"
Write-Host "  $DOCS_URL" -ForegroundColor Cyan
Write-Host ""
Read-Host "  Press ENTER to continue"

# ── Docker check ──────────────────────────────────────────────────────────────
Write-Host ""
Write-Info "Checking for Docker..."

$dockerOk = $false
try {
    $null = docker info 2>&1
    $dockerOk = $true
} catch {}

if (-not $dockerOk) {
    Write-Host ""
    Write-Warn "Docker Desktop is not installed or not running."
    Write-Host ""
    Write-Host "  1. Download Docker Desktop:" -ForegroundColor White
    Write-Host "     https://www.docker.com/products/docker-desktop" -ForegroundColor Cyan
    Write-Host "  2. Install it and start it"
    Write-Host "  3. Re-run this script"
    Write-Host ""
    Start-Process "https://www.docker.com/products/docker-desktop"
    Read-Host "  Press ENTER after Docker Desktop is running to try again"

    try {
        $null = docker info 2>&1
    } catch {
        Write-Fail "Docker still not reachable. Please start Docker Desktop and try again."
    }
}

Write-OK "Docker is ready."

# ── Gather config ─────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  ──────────────────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host "  Step 1 of 2 — Your SpanGate API Key" -ForegroundColor White
Write-Host "  ──────────────────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host ""

# Optional: pre-fill from env vars set by the dashboard one-liner
#   $env:SPNG_API_KEY = "spng_..."; irm .../setup.ps1 | iex
$API_KEY = if ($env:SPNG_API_KEY -match '^spng_.{10,}') { $env:SPNG_API_KEY } else { '' }

if ($API_KEY) {
    Write-OK "API key pre-filled from dashboard."
} else {
    while ($true) {
        $API_KEY = (Read-Host "  Paste your API key (starts with spng_)").Trim()
        if ($API_KEY -match '^spng_.{10,}') {
            Write-OK "API key accepted."
            break
        }
        Write-Warn "That doesn't look right — the key should start with 'spng_'. Try again."
    }
}

Write-Host ""
Write-Host "  ──────────────────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host "  Step 2 of 2 — Site Name & Ping Interval" -ForegroundColor White
Write-Host "  ──────────────────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host ""
$SITE_NAME = if ($env:SPNG_SITE_NAME) { $env:SPNG_SITE_NAME } else { '' }
if ($SITE_NAME) {
    Write-OK "Site name pre-filled: $SITE_NAME"
} else {
    Write-Host "  What should this site be called?"
    Write-Host "  (e.g. Main Campus, District Office, Elementary School)"
    $SITE_NAME = (Read-Host "  >").Trim()
    if (-not $SITE_NAME) { $SITE_NAME = 'My Site' }
}

Write-Host ""
Write-Host "  Ping interval in seconds? (default: 60)"
$raw = (Read-Host "  >").Trim()
$PING_INTERVAL = if ($raw -match '^\d+$') { $raw } else { '60' }

# ── Create setup directory ────────────────────────────────────────────────────
New-Item -ItemType Directory -Force $SETUP_DIR | Out-Null

# ── Write config.yaml ─────────────────────────────────────────────────────────
Write-Host ""
Write-Info "Writing config.yaml..."

@"
agent:
  site_name: "$SITE_NAME"
  api_url: "https://spangate-site-r81b.vercel.app"
  api_key: "$API_KEY"
  ping_interval: $PING_INTERVAL
  config_pull_interval: 604800   # SSH config backups every 7 days

# Devices are managed from the SpanGate dashboard - no need to list them here.
# Add devices at https://spangate.com/netmonitor/settings
# They sync to this agent automatically within 5 minutes.
devices: []
"@ | Set-Content -Path "$SETUP_DIR\config.yaml" -Encoding UTF8

Write-OK "config.yaml created at $SETUP_DIR\config.yaml"

# ── Write docker-compose.yml ──────────────────────────────────────────────────
# Note: network_mode: host is not supported on Docker Desktop for Windows.
# Docker Desktop routes container traffic through its VM which can reach
# all devices on your local network automatically.

@"
services:
  spangate-agent:
    image: $IMAGE
    container_name: spangate-agent
    restart: unless-stopped
    volumes:
      - $SETUP_DIR\config.yaml:/config/config.yaml:ro
    logging:
      driver: json-file
      options:
        max-size: 10m
        max-file: "3"
"@ | Set-Content -Path "$SETUP_DIR\docker-compose.yml" -Encoding UTF8

Write-OK "docker-compose.yml created."

# ── Pull image + start ────────────────────────────────────────────────────────
Write-Host ""
Write-Info "Pulling latest agent image from Docker Hub..."
docker pull $IMAGE

Write-Host ""
Write-Info "Starting SpanGate agent..."
Set-Location $SETUP_DIR
docker compose up -d

# ── Done ──────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  ╔═══════════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "  ║         Agent is running — you're all set!        ║" -ForegroundColor Green
Write-Host "  ╚═══════════════════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""
Write-Host "  Site:      $SITE_NAME"
Write-Host "  Files:     $SETUP_DIR"
Write-Host "  Container: spangate-agent  (auto-restarts on reboot)"
Write-Host ""
Write-Host "  Useful commands (run in PowerShell):"
Write-Host "    View live logs  :  docker logs -f spangate-agent"
Write-Host "    Stop the agent  :  cd $SETUP_DIR; docker compose down"
Write-Host "    Update image    :  docker pull $IMAGE; cd $SETUP_DIR; docker compose up -d"
Write-Host ""
Write-Host "  Now add devices from your dashboard:" -ForegroundColor White
Write-Host "  $DOCS_URL" -ForegroundColor Cyan
Write-Host ""
