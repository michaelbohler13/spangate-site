# SpanGate Network Monitor Agent

Lightweight Python agent that monitors your network devices from a Raspberry Pi, VM, or any Linux machine — and reports live status, config changes, and weekly backups to your SpanGate dashboard.

---

## Requirements

- Python 3.9+
- SSH access to your network devices
- A SpanGate API key (from your dashboard → Settings)
- Linux or Raspberry Pi OS (for the automated installer)

---

## Quick Install (Linux / Raspberry Pi)

```bash
curl -sSL https://install.spangate.com | bash
```

> **Note:** The hosted installer URL is not live yet. Use the manual install steps below.

---

## Manual Install

```bash
# 1. Clone the repo
git clone https://github.com/michaelbohler13/spangate-site.git
cd spangate-site/netmonitor-agent

# 2. Install dependencies
pip install -r requirements.txt

# 3. Edit config
cp config.yaml my-config.yaml   # optional — agent.py loads config.yaml by default
nano config.yaml

# 4. Run the agent
python3 agent.py
```

---

## Configuration (`config.yaml`)

```yaml
agent:
  site_name: "Main Campus"           # Human-readable label shown in the dashboard
  api_url: "https://api.spangate.com" # SpanGate backend URL (do not change)
  api_key: "YOUR_API_KEY_HERE"       # Paste your key from the dashboard
  ping_interval: 60                  # Seconds between ICMP ping sweeps
  config_pull_interval: 604800       # Seconds between SSH config pulls (default: 7 days)

devices:
  - hostname: core-sw-01             # Friendly name shown in the dashboard
    ip: 10.1.1.1                     # Reachable IP from the agent machine
    vendor: cisco                    # cisco | aruba_cx | juniper
    device_type: cisco_ios           # Netmiko device type
    ssh_username: admin
    ssh_password: "your-password"
    ssh_port: 22                     # Optional — defaults to 22
```

### Config fields

| Field | Required | Default | Description |
|---|---|---|---|
| `agent.site_name` | ✓ | — | Label for this site in the dashboard |
| `agent.api_url` | ✓ | `https://api.spangate.com` | SpanGate API base URL |
| `agent.api_key` | ✓ | — | Your API key from the dashboard |
| `agent.ping_interval` | | `60` | Seconds between ping sweeps |
| `agent.config_pull_interval` | | `604800` | Seconds between config pulls (7 days) |
| `devices[].hostname` | ✓ | — | Device display name |
| `devices[].ip` | ✓ | — | Device IP address |
| `devices[].vendor` | ✓ | — | `cisco`, `aruba_cx`, or `juniper` |
| `devices[].device_type` | ✓ | — | Netmiko device type string |
| `devices[].ssh_username` | ✓ | — | SSH login username |
| `devices[].ssh_password` | ✓ | — | SSH login password |
| `devices[].ssh_port` | | `22` | SSH port |

---

## Supported Vendors

| Vendor | Config Command | Netmiko Type |
|---|---|---|
| Cisco IOS / IOS-XE | `show running-config` | `cisco_ios` |
| Aruba CX (AOS-CX) | `show running-config` | `aruba_osswitch` |
| Juniper JunOS | `show configuration` | `juniper_junos` |

---

## How It Works

The agent runs three loops concurrently:

1. **Ping loop** — ICMP pings every device every `ping_interval` seconds. On any up↔down state change, an alert is posted to the SpanGate API. Results are held in memory only — nothing written to disk.

2. **Config pull loop** — SSH into each device every `config_pull_interval` seconds (default: weekly), pull the running config, SHA256-hash it, compare to the last known hash. If it changed, fire a config-change alert. Always ship a backup regardless.

3. **Heartbeat loop** — Every 5 minutes, posts a heartbeat to the API so the dashboard knows the agent is alive and reports current device counts.

---

## Security Notes

- SSH credentials are stored only in `config.yaml` on your machine — they are **never sent to the cloud**
- Only encrypted config text and ping status are transmitted to the SpanGate API
- Set `config.yaml` permissions to `600` so only root can read it: `chmod 600 config.yaml`

---

## Logs

When running as a systemd service:
```bash
journalctl -u spangate-netmonitor -f
```

When running manually, logs print to stdout in the format:
```
2026-05-25 14:32:01  INFO      [PING] core-sw-01 (10.1.1.1) status changed → DOWN
2026-05-25 14:32:01  INFO      [SSH]  Config CHANGED on dist-sw-02 — old a3f2c1 → new 9d8e4b
```

---

## File Structure

```
netmonitor-agent/
├── agent.py          # Main entry point — starts all loops
├── pinger.py         # Async ICMP ping loop
├── ssh_puller.py     # SSH config-pull loop (Netmiko)
├── api_client.py     # HTTP client for the SpanGate API
├── config.yaml       # Site and device configuration
├── requirements.txt  # Python dependencies
├── install.sh        # Linux/Pi automated installer
└── README.md         # This file
```

---

*SpanGate Technology — Your network. Under control.*
