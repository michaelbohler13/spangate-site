# SpanGate Network Monitor — Agent Download Script (Windows)
# Usage:
#   irm https://spangate-site.vercel.app/netmonitor/agent-install.ps1 | iex

$ErrorActionPreference = "Stop"

$dest = "$env:USERPROFILE\Documents\spangate-agent\netmonitor-agent"
$base = "https://spangate-site.vercel.app/netmonitor-agent"
$files = @("agent.py","api_client.py","pinger.py","ssh_puller.py","requirements.txt","config.yaml")

Write-Host ""
Write-Host "  SpanGate Network Monitor - Downloading agent files..."
Write-Host ""

New-Item -ItemType Directory -Force -Path $dest | Out-Null

foreach ($f in $files) {
    Invoke-WebRequest "$base/$f" -OutFile "$dest\$f" -UseBasicParsing
    Write-Host "  OK  $f"
}

Write-Host ""
Write-Host "  Done. Agent files saved to: $dest"
Write-Host ""
Write-Host "  Next steps:"
Write-Host "    cd `"$dest`""
Write-Host "    python -m venv venv"
Write-Host "    venv\Scripts\activate"
Write-Host "    pip install -r requirements.txt"
Write-Host ""
