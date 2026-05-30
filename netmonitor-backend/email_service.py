"""
email_service.py — SpanGate Network Monitor Backend
Transactional email helpers supporting two delivery providers:

  1. Resend (default)  — API-based, sends from alerts@spangate.com
  2. SMTP relay        — user-supplied server (Gmail, Google Workspace,
                         Microsoft 365, district mail server, etc.)

Provider selection is per-user, stored in nm_profiles.email_provider.
When email_provider == 'smtp' and smtp_host is set, all alert emails
are routed through the user's SMTP config instead of Resend.

Feedback notification emails always use Resend (they go to ADMIN_EMAIL,
not a user-configured address).

Cooldown:
  Ping-down alerts are rate-limited to one email per (site_id, hostname)
  per COOLDOWN_MINUTES.  Ping-up alerts pair to their matching DOWN and
  clear the cooldown entry so the next DOWN fires fresh.
"""

import asyncio
import logging
import os
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import resend  # pip install resend

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

resend.api_key = os.environ.get("RESEND_API_KEY", "")

FROM_ALERTS  = "SpanGate Alerts <alerts@spangate.com>"
FROM_NOREPLY = "SpanGate <noreply@spangate.com>"
ADMIN_EMAIL  = os.environ.get("ADMIN_EMAIL", "")
DASHBOARD_URL = "https://spangate-site.vercel.app/netmonitor/dashboard"

COOLDOWN_MINUTES = 60   # minimum gap between repeat DOWN emails for same device


# ── SMTP config dataclass ─────────────────────────────────────────────────────

@dataclass
class SmtpConfig:
    """User-supplied SMTP relay credentials."""
    host:      str
    port:      int
    user:      str
    password:  str
    from_addr: str   # falls back to user if blank


# ── In-process cooldown tracker ───────────────────────────────────────────────

_last_alert_email: dict[tuple[str, str], datetime] = {}


def _should_send_alert(site_id: str, hostname: str) -> bool:
    """Return True if enough time has passed since the last DOWN alert email."""
    key = (site_id, hostname)
    last = _last_alert_email.get(key)
    if last is None:
        return True
    return datetime.now(timezone.utc) - last > timedelta(minutes=COOLDOWN_MINUTES)


def _mark_alert_sent(site_id: str, hostname: str) -> None:
    _last_alert_email[(site_id, hostname)] = datetime.now(timezone.utc)


def _has_down_alert(site_id: str, hostname: str) -> bool:
    """Return True if a DOWN alert was sent for this device (UP email is warranted)."""
    return (site_id, hostname) in _last_alert_email


def _clear_down_alert(site_id: str, hostname: str) -> None:
    """Remove the cooldown entry after sending a UP alert so the next DOWN fires fresh."""
    _last_alert_email.pop((site_id, hostname), None)


def _format_downtime(seconds: float) -> str:
    """Human-readable downtime duration, e.g. '~14 minutes' or '~2 hours'."""
    if seconds < 90:
        return "less than a minute"
    minutes = int(seconds / 60)
    if minutes < 90:
        return f"~{minutes} minute{'s' if minutes != 1 else ''}"
    hours = round(minutes / 60, 1)
    return f"~{hours} hour{'s' if hours != 1.0 else ''}"


# ── SMTP dispatcher ───────────────────────────────────────────────────────────

def _smtp_send_sync(
    to_email: str,
    subject:  str,
    html:     str,
    cfg:      SmtpConfig,
) -> None:
    """
    Synchronous SMTP send — always called via asyncio.to_thread.

    Supports:
      Port 465  → SSL from the start (SMTP_SSL)
      Port 587  → STARTTLS upgrade (standard for Gmail / Microsoft 365)
      Other     → STARTTLS attempted
    """
    from_addr = cfg.from_addr or cfg.user

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to_email
    msg.attach(MIMEText(html, "html", "utf-8"))

    context = ssl.create_default_context()

    if cfg.port == 465:
        with smtplib.SMTP_SSL(cfg.host, cfg.port, context=context, timeout=15) as server:
            server.login(cfg.user, cfg.password)
            server.sendmail(from_addr, [to_email], msg.as_string())
    else:
        with smtplib.SMTP(cfg.host, cfg.port, timeout=15) as server:
            server.ehlo()
            server.starttls(context=context)
            server.login(cfg.user, cfg.password)
            server.sendmail(from_addr, [to_email], msg.as_string())


async def _dispatch_alert_email(
    to_email:    str,
    subject:     str,
    html:        str,
    smtp_config: SmtpConfig | None,
) -> None:
    """
    Route an alert email through SMTP relay or Resend.

    Raises on delivery failure so callers can log the error.
    """
    if smtp_config:
        await asyncio.to_thread(_smtp_send_sync, to_email, subject, html, smtp_config)
    else:
        if not resend.api_key:
            raise RuntimeError("RESEND_API_KEY not configured and no SMTP relay set")
        params: resend.Emails.SendParams = {
            "from":    FROM_ALERTS,
            "to":      [to_email],
            "subject": subject,
            "html":    html,
        }
        await asyncio.to_thread(resend.Emails.send, params)


# ── HTML templates ────────────────────────────────────────────────────────────

def _ping_down_html(site_name: str, hostname: str, ip: str, ts: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0d1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="background:#0d1117;padding:40px 16px;">
    <tr><td align="center">
      <table width="540" cellpadding="0" cellspacing="0" role="presentation" style="max-width:540px;width:100%;background:#111827;border-radius:12px;overflow:hidden;border:1px solid #1f2937;">

        <!-- STATUS BAR -->
        <tr><td style="background:#7f1d1d;padding:16px 28px;">
          <span style="font-size:12px;font-weight:700;color:#fca5a5;letter-spacing:0.08em;text-transform:uppercase;">&#11044; Device Offline</span>
        </td></tr>

        <!-- BODY -->
        <tr><td style="padding:28px 28px 24px;">
          <p style="margin:0 0 6px 0;font-size:20px;font-weight:700;color:#f9fafb;letter-spacing:-0.02em;">{hostname}</p>
          <p style="margin:0 0 24px 0;font-size:13px;color:#6b7280;font-family:'JetBrains Mono',Consolas,monospace;">{ip}</p>

          <table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="border-radius:8px;overflow:hidden;border:1px solid #1f2937;margin-bottom:24px;">
            <tr style="border-bottom:1px solid #1f2937;">
              <td style="padding:10px 14px;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;width:80px;">Site</td>
              <td style="padding:10px 14px;font-size:13px;color:#e5e7eb;text-align:right;">{site_name}</td>
            </tr>
            <tr>
              <td style="padding:10px 14px;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Time</td>
              <td style="padding:10px 14px;font-size:13px;color:#e5e7eb;text-align:right;font-family:monospace;">{ts}</td>
            </tr>
          </table>

          <a href="{DASHBOARD_URL}" style="display:inline-block;background:#00d4b8;color:#000000;font-size:13px;font-weight:600;text-decoration:none;padding:10px 20px;border-radius:7px;letter-spacing:-0.01em;">View Dashboard &#8594;</a>
        </td></tr>

        <!-- FOOTER -->
        <tr><td style="padding:14px 28px;border-top:1px solid #1f2937;">
          <p style="margin:0;font-size:11px;color:#4b5563;line-height:1.5;">
            SpanGate Network Monitor &nbsp;·&nbsp;
            <a href="{DASHBOARD_URL}" style="color:#4b5563;text-decoration:underline;">Dashboard</a>
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _ping_up_html(site_name: str, hostname: str, ip: str, ts: str, downtime: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0d1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="background:#0d1117;padding:40px 16px;">
    <tr><td align="center">
      <table width="540" cellpadding="0" cellspacing="0" role="presentation" style="max-width:540px;width:100%;background:#111827;border-radius:12px;overflow:hidden;border:1px solid #1f2937;">

        <!-- STATUS BAR -->
        <tr><td style="background:#14532d;padding:16px 28px;">
          <span style="font-size:12px;font-weight:700;color:#86efac;letter-spacing:0.08em;text-transform:uppercase;">&#11044; Device Online</span>
        </td></tr>

        <!-- BODY -->
        <tr><td style="padding:28px 28px 24px;">
          <p style="margin:0 0 6px 0;font-size:20px;font-weight:700;color:#f9fafb;letter-spacing:-0.02em;">{hostname}</p>
          <p style="margin:0 0 24px 0;font-size:13px;color:#6b7280;font-family:'JetBrains Mono',Consolas,monospace;">{ip}</p>

          <table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="border-radius:8px;overflow:hidden;border:1px solid #1f2937;margin-bottom:24px;">
            <tr style="border-bottom:1px solid #1f2937;">
              <td style="padding:10px 14px;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;width:90px;">Site</td>
              <td style="padding:10px 14px;font-size:13px;color:#e5e7eb;text-align:right;">{site_name}</td>
            </tr>
            <tr style="border-bottom:1px solid #1f2937;">
              <td style="padding:10px 14px;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Recovered</td>
              <td style="padding:10px 14px;font-size:13px;color:#e5e7eb;text-align:right;font-family:monospace;">{ts}</td>
            </tr>
            <tr>
              <td style="padding:10px 14px;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Was down</td>
              <td style="padding:10px 14px;font-size:13px;color:#86efac;text-align:right;">{downtime}</td>
            </tr>
          </table>

          <a href="{DASHBOARD_URL}" style="display:inline-block;background:#00d4b8;color:#000000;font-size:13px;font-weight:600;text-decoration:none;padding:10px 20px;border-radius:7px;letter-spacing:-0.01em;">View Dashboard &#8594;</a>
        </td></tr>

        <!-- FOOTER -->
        <tr><td style="padding:14px 28px;border-top:1px solid #1f2937;">
          <p style="margin:0;font-size:11px;color:#4b5563;line-height:1.5;">
            SpanGate Network Monitor &nbsp;·&nbsp;
            <a href="{DASHBOARD_URL}" style="color:#4b5563;text-decoration:underline;">Dashboard</a>
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _test_email_html() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0d1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="background:#0d1117;padding:40px 16px;">
    <tr><td align="center">
      <table width="540" cellpadding="0" cellspacing="0" role="presentation" style="max-width:540px;width:100%;background:#111827;border-radius:12px;overflow:hidden;border:1px solid #1f2937;">

        <!-- STATUS BAR -->
        <tr><td style="background:#1e3a5f;padding:16px 28px;">
          <span style="font-size:12px;font-weight:700;color:#93c5fd;letter-spacing:0.08em;text-transform:uppercase;">&#9993; Test Email</span>
        </td></tr>

        <!-- BODY -->
        <tr><td style="padding:28px 28px 24px;">
          <p style="margin:0 0 16px 0;font-size:20px;font-weight:700;color:#f9fafb;letter-spacing:-0.02em;">Email delivery is working</p>
          <p style="margin:0 0 24px 0;font-size:14px;color:#9ca3af;line-height:1.65;">
            Your SpanGate Network Monitor alert email configuration is verified.
            Device down and recovery alerts will be delivered to this address.
          </p>

          <table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="border-radius:8px;overflow:hidden;border:1px solid #1f2937;margin-bottom:24px;">
            <tr>
              <td style="padding:10px 14px;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;width:80px;">Sent at</td>
              <td style="padding:10px 14px;font-size:13px;color:#e5e7eb;text-align:right;font-family:monospace;">{ts}</td>
            </tr>
          </table>

          <a href="{DASHBOARD_URL}" style="display:inline-block;background:#00d4b8;color:#000000;font-size:13px;font-weight:600;text-decoration:none;padding:10px 20px;border-radius:7px;letter-spacing:-0.01em;">View Dashboard &#8594;</a>
        </td></tr>

        <!-- FOOTER -->
        <tr><td style="padding:14px 28px;border-top:1px solid #1f2937;">
          <p style="margin:0;font-size:11px;color:#4b5563;line-height:1.5;">
            SpanGate Network Monitor &nbsp;·&nbsp;
            <a href="{DASHBOARD_URL}" style="color:#4b5563;text-decoration:underline;">Dashboard</a>
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _feedback_notify_html(name: str, email: str, subject: str, message: str) -> str:
    safe_msg = (
        message
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br>")
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0d1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="background:#0d1117;padding:40px 16px;">
    <tr><td align="center">
      <table width="540" cellpadding="0" cellspacing="0" role="presentation" style="max-width:540px;width:100%;background:#111827;border-radius:12px;overflow:hidden;border:1px solid #1f2937;">

        <!-- HEADER -->
        <tr><td style="background:#064e3b;padding:16px 28px;">
          <span style="font-size:12px;font-weight:700;color:#6ee7b7;letter-spacing:0.08em;text-transform:uppercase;">&#128236; New Feedback</span>
        </td></tr>

        <!-- BODY -->
        <tr><td style="padding:28px 28px 24px;">
          <table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="border-radius:8px;overflow:hidden;border:1px solid #1f2937;margin-bottom:20px;">
            <tr style="border-bottom:1px solid #1f2937;">
              <td style="padding:10px 14px;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;width:70px;">From</td>
              <td style="padding:10px 14px;font-size:13px;color:#e5e7eb;text-align:right;">{name} &lt;{email}&gt;</td>
            </tr>
            <tr>
              <td style="padding:10px 14px;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Subject</td>
              <td style="padding:10px 14px;font-size:13px;color:#e5e7eb;text-align:right;">{subject}</td>
            </tr>
          </table>

          <div style="background:#0d1117;border:1px solid #1f2937;border-radius:8px;padding:16px 18px;">
            <p style="margin:0;font-size:14px;color:#9ca3af;line-height:1.7;">{safe_msg}</p>
          </div>
        </td></tr>

        <!-- FOOTER -->
        <tr><td style="padding:14px 28px;border-top:1px solid #1f2937;">
          <p style="margin:0;font-size:11px;color:#4b5563;">SpanGate &nbsp;·&nbsp; Reply to this email to respond directly to {name}</p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


# ── Send functions ─────────────────────────────────────────────────────────────

async def send_ping_down_alert(
    *,
    to_email:    str,
    site_name:   str,
    site_id:     str,
    hostname:    str,
    ip:          str,
    timestamp:   datetime,
    smtp_config: SmtpConfig | None = None,
) -> None:
    """
    Send a device-down alert email via Resend or SMTP relay.

    Silently no-ops when:
    - No delivery method is configured (no RESEND_API_KEY, no smtp_config)
    - to_email is empty
    - A down-alert for this device was already sent within COOLDOWN_MINUTES
    """
    if not smtp_config and not resend.api_key:
        logger.debug("[EMAIL] No delivery method — skipping DOWN alert for %s/%s", site_id, hostname)
        return
    if not to_email:
        logger.debug("[EMAIL] No recipient — skipping DOWN alert for %s/%s", site_id, hostname)
        return
    if not _should_send_alert(site_id, hostname):
        logger.debug("[EMAIL] Cooldown active for %s/%s — skipping", site_id, hostname)
        return

    ts      = timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
    subject = f"\U0001f534 DOWN: {hostname} ({ip})"
    html    = _ping_down_html(site_name, hostname, ip, ts)

    try:
        await _dispatch_alert_email(to_email, subject, html, smtp_config)
        _mark_alert_sent(site_id, hostname)
        logger.info("[EMAIL] DOWN alert sent to %s for %s/%s", to_email, site_id, hostname)
    except Exception as exc:
        logger.error("[EMAIL] Failed to send DOWN alert for %s/%s: %s", site_id, hostname, exc)


async def send_ping_up_alert(
    *,
    to_email:     str,
    site_name:    str,
    site_id:      str,
    hostname:     str,
    ip:           str,
    timestamp:    datetime,
    went_down_at: datetime | None = None,
    smtp_config:  SmtpConfig | None = None,
) -> None:
    """
    Send a device-back-online alert email via Resend or SMTP relay.

    Only fires when a DOWN alert was previously sent for this device.
    After sending, the cooldown entry is cleared so the next DOWN fires fresh.
    """
    if not smtp_config and not resend.api_key:
        logger.debug("[EMAIL] No delivery method — skipping UP alert for %s/%s", site_id, hostname)
        return
    if not to_email:
        logger.debug("[EMAIL] No recipient — skipping UP alert for %s/%s", site_id, hostname)
        return
    if not _has_down_alert(site_id, hostname):
        logger.debug("[EMAIL] No prior DOWN alert for %s/%s — skipping UP email", site_id, hostname)
        return

    if went_down_at and went_down_at.tzinfo is not None:
        seconds  = (timestamp - went_down_at).total_seconds()
        downtime = _format_downtime(max(seconds, 0))
    else:
        downtime = "unknown"

    ts      = timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
    subject = f"✅ UP: {hostname} ({ip})"
    html    = _ping_up_html(site_name, hostname, ip, ts, downtime)

    try:
        await _dispatch_alert_email(to_email, subject, html, smtp_config)
        _clear_down_alert(site_id, hostname)
        logger.info("[EMAIL] UP alert sent to %s for %s/%s (down %s)", to_email, site_id, hostname, downtime)
    except Exception as exc:
        logger.error("[EMAIL] Failed to send UP alert for %s/%s: %s", site_id, hostname, exc)


async def send_test_alert_email(
    *,
    to_email:    str,
    smtp_config: SmtpConfig | None = None,
) -> None:
    """
    Send a test email to verify alert delivery configuration.

    Raises on failure so the caller can return a meaningful error to the UI.
    """
    await _dispatch_alert_email(
        to_email,
        "✅ SpanGate Alert — Test Email",
        _test_email_html(),
        smtp_config,
    )
    logger.info("[EMAIL] Test alert email sent to %s", to_email)


async def send_feedback_notification(
    *,
    name:    str,
    email:   str,
    subject: str,
    message: str,
) -> None:
    """
    Send an admin notification when a user submits feedback.
    Always uses Resend — not user-configurable.
    """
    if not resend.api_key or not ADMIN_EMAIL:
        logger.warning("[EMAIL] Feedback notification skipped — RESEND_API_KEY or ADMIN_EMAIL not set")
        return

    params: resend.Emails.SendParams = {
        "from":     FROM_NOREPLY,
        "to":       [ADMIN_EMAIL],
        "reply_to": email,
        "subject":  f"[SpanGate Feedback] {subject} — {name}",
        "html":     _feedback_notify_html(name, email, subject, message),
    }
    try:
        await asyncio.to_thread(resend.Emails.send, params)
        logger.info("[EMAIL] Feedback notification sent for %r <%s>", name, email)
    except Exception as exc:
        logger.error("[EMAIL] Failed to send feedback notification: %s", exc)
