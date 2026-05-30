"""
email_service.py — SpanGate Network Monitor Backend
Resend-powered transactional email helpers.

Sending domain: send.spangate.com  (verified in Resend)
FROM addresses:
  alerts@send.spangate.com  — device down/up alerts
  noreply@send.spangate.com — feedback notifications, system messages

All send functions are async-safe (sync Resend SDK is wrapped in
asyncio.to_thread so the event loop is never blocked).

Cooldown:
  Ping-down alerts are rate-limited to one email per (site_id, hostname)
  per COOLDOWN_MINUTES to prevent flooding when many devices go offline
  simultaneously (e.g. agent restart or upstream outage).
  State is in-process — survives within a Vercel function lifetime but
  resets on cold start, which is an acceptable trade-off for Phase 1.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import resend  # pip install resend

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

resend.api_key = os.environ.get("RESEND_API_KEY", "")

FROM_ALERTS  = "SpanGate Alerts <alerts@spangate.com>"
FROM_NOREPLY = "SpanGate <noreply@spangate.com>"
ADMIN_EMAIL  = os.environ.get("ADMIN_EMAIL", "")
DASHBOARD_URL = "https://spangate-site.vercel.app/netmonitor/dashboard"

COOLDOWN_MINUTES = 60   # minimum gap between repeat DOWN emails for same device

# ── In-process cooldown tracker ───────────────────────────────────────────────

_last_alert_email: dict[tuple[str, str], datetime] = {}


def _should_send_alert(site_id: str, hostname: str) -> bool:
    """Return True if enough time has passed since the last alert email."""
    key = (site_id, hostname)
    last = _last_alert_email.get(key)
    if last is None:
        return True
    return datetime.now(timezone.utc) - last > timedelta(minutes=COOLDOWN_MINUTES)


def _mark_alert_sent(site_id: str, hostname: str) -> None:
    _last_alert_email[(site_id, hostname)] = datetime.now(timezone.utc)


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
    to_email:  str,
    site_name: str,
    site_id:   str,
    hostname:  str,
    ip:        str,
    timestamp: datetime,
) -> None:
    """
    Send a device-down alert email.

    Silently no-ops when:
    - RESEND_API_KEY is not configured
    - to_email is empty
    - A down-alert for this device was already sent within COOLDOWN_MINUTES

    Args:
        to_email:  Recipient address (the site owner's email).
        site_name: Human-readable site label shown in the email.
        site_id:   Used for cooldown key.
        hostname:  Device hostname that went down.
        ip:        Device IP address.
        timestamp: Time of the status change.
    """
    if not resend.api_key:
        logger.debug("[EMAIL] RESEND_API_KEY not set — skipping alert for %s/%s", site_id, hostname)
        return
    if not to_email:
        logger.debug("[EMAIL] No recipient email — skipping alert for %s/%s", site_id, hostname)
        return
    if not _should_send_alert(site_id, hostname):
        logger.debug("[EMAIL] Cooldown active for %s/%s — skipping", site_id, hostname)
        return

    ts = timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
    params: resend.Emails.SendParams = {
        "from":    FROM_ALERTS,
        "to":      [to_email],
        "subject": f"\U0001f534 DOWN: {hostname} ({ip})",
        "html":    _ping_down_html(site_name, hostname, ip, ts),
    }
    try:
        await asyncio.to_thread(resend.Emails.send, params)
        _mark_alert_sent(site_id, hostname)
        logger.info("[EMAIL] Ping-down alert sent to %s for %s/%s", to_email, site_id, hostname)
    except Exception as exc:
        logger.error("[EMAIL] Failed to send ping-down alert for %s/%s: %s", site_id, hostname, exc)


async def send_feedback_notification(
    *,
    name:    str,
    email:   str,
    subject: str,
    message: str,
) -> None:
    """
    Send an admin notification email when a user submits feedback.

    Destination is ADMIN_EMAIL env var.  Silently no-ops when either
    RESEND_API_KEY or ADMIN_EMAIL is not configured.

    Args:
        name:    Submitter's name.
        email:   Submitter's email (set as Reply-To).
        subject: Feedback subject category.
        message: Feedback body text.
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
