# SpanGate Network Monitor — Windows Install Guide

---

## Requirements

- Windows 10 (version 1903 or later) or Windows 11
- Python 3.9 or higher
- Network access to your managed devices (same VLAN or routed)
- SSH enabled on all target switches/routers
- Your SpanGate API key (from your Network Monitor dashboard → Settings)
- Git for Windows (optional but recommended)

---

## Step 1 — Install Python

1. Go to [python.org/downloads](https://python.org/downloads)
2. Click **Download Python 3.11.x** (or any 3.9+ release)
3. Run the installer
4. ✅ **Check "Add Python to PATH"** on the first screen — do not skip this
5. Click **Install Now**

Verify in Command Prompt:

```cmd
python --version
pip --version
```

Both commands must succeed. If `python` is not recognized, re-run the installer and ensure "Add to PATH" is checked.

---

## Step 2 — Install Git (Recommended)

1. Go to [git-scm.com/download/win](https://git-scm.com/download/win)
2. Download and run the installer
3. All default settings are fine — click **Next** through each screen
4. Verify:

```cmd
git --version
```

---

## Step 3 — Download the Agent

**Option A — Git (recommended):**

Open Command Prompt and run:

```cmd
cd %USERPROFILE%\Documents
git clone https://github.com/michaelbohler13/spangate-site.git
cd spangate-site\netmonitor-agent
```

**Option B — Manual download:**

1. Go to [github.com/michaelbohler13/spangate-site](https://github.com/michaelbohler13/spangate-site)
2. Click **Code → Download ZIP**
3. Extract the ZIP to `Documents\spangate-site`
4. Open Command Prompt:

```cmd
cd %USERPROFILE%\Documents\spangate-site\netmonitor-agent
```

---

## Step 4 — Create a Virtual Environment and Install Dependencies

```cmd
python -m venv venv
venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

You should see `(venv)` at the start of your prompt when the virtual environment is active.
Installation takes 2–5 minutes depending on your connection speed.

---

## Step 5 — Configure the Agent

1. In File Explorer, navigate to `Documents\spangate-site\netmonitor-agent`
2. Copy `config.yaml` and rename the copy to `my-config.yaml`
3. Open `my-config.yaml` in Notepad or VS Code
4. Edit the following fields:

```yaml
agent:
  site_name: "Main Office"                  # ← your site name
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
Save the file.

---

## Step 6 — Test the Agent Manually

Make sure the virtual environment is active (`(venv)` in prompt), then:

```cmd
python agent.py --config my-config.yaml
```

Expected output:

```
2026-05-25 14:32:01  INFO      SpanGate Network Monitor Agent v1.0.0 — Main Office — 3 device(s)
2026-05-25 14:32:01  INFO      [PING] Loop started — 3 device(s), interval 60s
2026-05-25 14:32:01  INFO      [SSH]  Pull loop started — 3 device(s), interval 604800s (168.0 hours)
2026-05-25 14:32:01  INFO      [HB]   Heartbeat loop started — interval 300s
2026-05-25 14:32:02  INFO      [PING] core-sw-01 (10.1.1.1) initial state: up
2026-05-25 14:32:02  INFO      [PING] dist-sw-02 (10.1.1.2) initial state: up
2026-05-25 14:32:03  INFO      [PING] fw-edge-01 (10.0.0.1) initial state: up
```

Stop with **Ctrl+C** once confirmed working.

---

## Step 7 — Run as a Windows Service with NSSM

Running the agent as a Windows service keeps it alive across reboots without needing a logged-in user.

**Install NSSM:**

1. Go to [nssm.cc/download](https://nssm.cc/download)
2. Download the latest release ZIP
3. Extract it and copy `nssm.exe` from the `win64` folder to `C:\Windows\System32\`

**Install the service — open Command Prompt as Administrator:**

```cmd
nssm install SpangateNetMonitor
```

Fill in the NSSM GUI as follows:

**Application tab:**
- Path: `C:\Users\YourName\Documents\spangate-site\netmonitor-agent\venv\Scripts\python.exe`
  *(use your actual username — check with `echo %USERNAME%`)*
- Startup directory: `C:\Users\YourName\Documents\spangate-site\netmonitor-agent`
- Arguments: `agent.py --config my-config.yaml`

**Details tab:**
- Display name: `SpanGate Network Monitor`
- Description: `SpanGate Technology — Network Monitor Agent`

Click **Install service**.

**Start the service:**

```cmd
nssm start SpangateNetMonitor
```

---

## Step 8 — Verify the Service

Open **Services** (press **Win+R**, type `services.msc`, press Enter).
Find **SpanGate Network Monitor** — status should be **Running**.

To view live logs, NSSM writes stdout/stderr to files by default. Configure log output in the NSSM GUI under the **I/O** tab, or check the application's console output file if you set one.

Your SpanGate dashboard should show the agent as **● Agent reporting** within a few minutes.

---

## Updating the Agent

```cmd
cd %USERPROFILE%\Documents\spangate-site
git pull
nssm restart SpangateNetMonitor
```

---

## Windows Firewall Note

The agent uses ICMP (ping) for device status checks. Windows Firewall may block outbound ICMP by default on some configurations. If devices show as DOWN even though they're reachable:

1. Open **Windows Defender Firewall with Advanced Security**
2. Go to **Outbound Rules → New Rule**
3. Rule type: **Custom**
4. Protocol: **ICMPv4**
5. Action: **Allow the connection**
6. Apply to all profiles, name it `SpanGate ICMP Out`, click **Finish**

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `python` not recognized | Re-run installer, check "Add Python to PATH" |
| `pip install` fails on netmiko | Run `pip install --upgrade pip` first, then retry |
| ICMP ping shows all devices DOWN | Allow outbound ICMP in Windows Firewall (see above) |
| SSH connection refused to device | Verify SSH is enabled on the switch and port 22 is open |
| Service won't start | Use absolute paths in NSSM — not relative paths |
| Agent runs manually but not as service | The Path must point to `venv\Scripts\python.exe`, not the system Python |
| Config pull fails | Ensure the Windows machine has a route to the device management IP |
| API key rejected (401) | Copy the key from the dashboard exactly — no extra spaces |
| `nssm` not found | Verify `nssm.exe` is in `C:\Windows\System32\` and re-open Command Prompt |
