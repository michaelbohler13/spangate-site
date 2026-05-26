# SpanGate Network Monitor — Raspberry Pi Install Guide

---

## Recommended Hardware

| Pi Model | Max Devices | Approx. Cost |
|---|---|---|
| Pi Zero 2W | 25 | ~$15 |
| Pi 4 (4GB) | 150 | ~$55 |
| Pi 5 (8GB) | 500 | ~$80 |

For most school networks (50–150 devices), a Pi 4 (4GB) is the recommended choice.

---

## Requirements

- Raspberry Pi (any model above — Pi Zero 2W through Pi 5)
- Raspberry Pi OS Lite, 64-bit (recommended)
- Python 3.9 or higher (pre-installed on Raspberry Pi OS Bullseye and later)
- Network access to your managed devices (same VLAN or routed)
- SSH enabled on all target switches/routers
- Your SpanGate API key (from your Network Monitor dashboard → Settings)

---

## Step 1 — Flash Raspberry Pi OS

1. Download **Raspberry Pi Imager** from [raspberrypi.com/software](https://raspberrypi.com/software)
2. Insert a microSD card (8 GB minimum, 16 GB recommended)
3. In the Imager: **Choose Device** → select your Pi model
4. **Choose OS** → Raspberry Pi OS Lite (64-bit)
5. **Choose Storage** → select your microSD card
6. Click the **gear icon (⚙️)** before clicking Write to pre-configure:
   - ✅ Enable SSH
   - Set hostname: `spangate-monitor` (or your preference)
   - Set username: `spangate`
   - Set password: *(choose a strong password)*
   - Configure WiFi SSID and password if not using Ethernet
7. Click **Write** and wait for it to finish
8. Insert the microSD into your Pi and power it on

---

## Step 2 — Connect to Your Pi

Give the Pi 60 seconds to boot, then find it on your network:

```bash
ping spangate-monitor.local
ssh spangate@spangate-monitor.local
```

If `.local` doesn't resolve, check your router's DHCP table for the Pi's IP and SSH directly:

```bash
ssh spangate@192.168.1.XXX
```

---

## Step 3 — Update the System

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git
python3 --version
```

The version must be 3.9 or higher. Raspberry Pi OS Bullseye and Bookworm both ship with Python 3.11.

---

## Step 4 — Download the Agent

```bash
cd ~
git clone https://github.com/michaelbohler13/spangate-site.git
cd spangate-site/netmonitor-agent
```

---

## Step 5 — Create a Virtual Environment and Install Dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Installation takes 2–5 minutes on a Pi 4. The `netmiko` and `napalm` packages are the heaviest.

---

## Step 6 — Configure the Agent

Create the config directory and copy the default config:

```bash
sudo mkdir -p /etc/spangate/netmonitor
sudo cp config.yaml /etc/spangate/netmonitor/config.yaml
sudo chmod 600 /etc/spangate/netmonitor/config.yaml
sudo nano /etc/spangate/netmonitor/config.yaml
```

Edit the following fields:

```yaml
agent:
  site_name: "Lincoln ISD - Main Campus"   # ← your site name
  api_url: "https://api.spangate.com"
  api_key: "spng_paste_your_key_here"       # ← your SpanGate API key
  ping_interval: 60
  config_pull_interval: 604800

devices:
  - hostname: core-sw-01
    ip: 10.1.1.1                            # ← your device IP
    vendor: cisco
    device_type: cisco_ios
    ssh_username: admin
    ssh_password: "your-switch-password"    # ← device SSH credentials
    ssh_port: 22
```

Add one entry under `devices:` for each switch or router you want to monitor.
Save with **Ctrl+X → Y → Enter**.

---

## Step 7 — Test the Agent Manually

Run the agent once to confirm it connects to the API and reaches your devices:

```bash
cd ~/spangate-site/netmonitor-agent
source venv/bin/activate
python3 agent.py --config /etc/spangate/netmonitor/config.yaml
```

Expected output:

```
2026-05-25 14:32:01  INFO      SpanGate Network Monitor Agent v1.0.0 — Lincoln ISD - Main Campus — 3 device(s)
2026-05-25 14:32:01  INFO      [PING] Loop started — 3 device(s), interval 60s
2026-05-25 14:32:01  INFO      [SSH]  Pull loop started — 3 device(s), interval 604800s (168.0 hours)
2026-05-25 14:32:01  INFO      [HB]   Heartbeat loop started — interval 300s
2026-05-25 14:32:02  INFO      [PING] core-sw-01 (10.1.1.1) initial state: up
2026-05-25 14:32:02  INFO      [PING] dist-sw-02 (10.1.1.2) initial state: up
2026-05-25 14:32:03  INFO      [PING] fw-edge-01 (10.0.0.1) initial state: up
```

Stop with **Ctrl+C** once confirmed working.

---

## Step 8 — Install as a Systemd Service

Create the service unit file:

```bash
sudo nano /etc/systemd/system/spangate-netmonitor.service
```

Paste the following (adjust the username if you used something other than `spangate`):

```ini
[Unit]
Description=SpanGate Network Monitor Agent
Documentation=https://spangate.com/netmonitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=spangate
WorkingDirectory=/home/spangate/spangate-site/netmonitor-agent
ExecStart=/home/spangate/spangate-site/netmonitor-agent/venv/bin/python3 agent.py --config /etc/spangate/netmonitor/config.yaml
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=spangate-netmonitor

[Install]
WantedBy=multi-user.target
```

Save with **Ctrl+X → Y → Enter**, then enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable spangate-netmonitor
sudo systemctl start spangate-netmonitor
```

---

## Step 9 — Verify the Service

```bash
sudo systemctl status spangate-netmonitor
```

You should see `Active: active (running)`.

Follow live logs:

```bash
sudo journalctl -u spangate-netmonitor -f
```

Your SpanGate dashboard should show the agent as **● Agent reporting** within a few minutes.

---

## Updating the Agent

```bash
cd ~/spangate-site
git pull
sudo systemctl restart spangate-netmonitor
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `python3` shows 3.8 or lower | `sudo apt install python3.11 python3.11-venv` |
| SSH connection refused to device | Verify SSH is enabled on the switch and port 22 is open |
| Agent starts then immediately stops | `sudo journalctl -u spangate-netmonitor -n 50` to read the error |
| API key rejected (401) | Copy the key from the dashboard exactly — no extra spaces |
| Device shows DOWN but is reachable | Confirm the Pi has a route: `ping 10.1.1.1` from the Pi |
| Config pull fails for Aruba CX | Run `web-management ssl` on the switch first |
| `netmiko` install fails | `sudo apt install python3-dev libssl-dev` then retry `pip install` |
| Service says `failed` status | Check permissions: `ls -la /etc/spangate/netmonitor/config.yaml` (should be `-rw-------`) |
