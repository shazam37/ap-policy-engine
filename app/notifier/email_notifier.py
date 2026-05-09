"""
Deviation Notifier Module
Handles email notifications for policy deviations.
Uses APScheduler for delayed dispatch and 48-hour escalation ladder.
Supports Mailtrap (dev), SMTP (prod), and console (test) backends.
"""
from __future__ import annotations
import logging
import smtplib
import json
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Callable

from app.models.schemas import (
    NotificationPayload, RuleAction, DeviationType, Invoice, RuleResult, ExtractedRule
)
from app.utils.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Role → email address mapping
# ---------------------------------------------------------------------------

ROLE_EMAIL_MAP: dict[str, str] = {
    "finance_controller": "finance.controller@company.com",
    "internal_audit": "internal.audit@company.com",
    "dept_head": "dept.head@company.com",
    "ap_clerk": "ap.clerk@company.com",
    "procurement": "procurement@company.com",
    "cfo": "cfo@company.com",
    "ap_manager": "ap.manager@company.com",
}

ESCALATION_LADDER: list[str] = [
    "dept_head",
    "finance_controller",
    "cfo",
]


# ---------------------------------------------------------------------------
# Email template builder
# ---------------------------------------------------------------------------

def _build_email_html(payload: NotificationPayload) -> str:
    """Build a clean HTML email for the deviation notification."""
    deviation_color = {
        DeviationType.AMOUNT_MISMATCH: "#E8593C",
        DeviationType.QUANTITY_MISMATCH: "#BA7517",
        DeviationType.RATE_MISMATCH: "#BA7517",
        DeviationType.MISSING_GRN: "#533AB7",
        DeviationType.TAX_ERROR: "#A32D2D",
        DeviationType.GSTIN_MISMATCH: "#A32D2D",
        DeviationType.DUPLICATE_INVOICE: "#E8593C",
        DeviationType.FUTURE_DATED: "#533AB7",
        DeviationType.QR_VALIDATION_FAILED: "#533AB7",
    }.get(payload.deviation_type, "#888780")

    details_rows = ""
    for k, v in payload.deviation_details.items():
        label = k.replace("_", " ").title()
        if isinstance(v, float):
            v_str = f"{v:,.2f}" if "pct" not in k.lower() else f"{v:+.2f}%"
        else:
            v_str = str(v)
        details_rows += f"""
        <tr>
          <td style="padding:6px 12px;color:#5F5E5A;font-size:13px;border-bottom:1px solid #E8E6DD;">{label}</td>
          <td style="padding:6px 12px;color:#2C2C2A;font-size:13px;border-bottom:1px solid #E8E6DD;font-weight:500;">{v_str}</td>
        </tr>"""

    deadline_row = ""
    if payload.resolve_deadline:
        dl = payload.resolve_deadline.strftime("%Y-%m-%d %H:%M UTC")
        deadline_row = f"""
        <p style="margin:16px 0 0;padding:12px;background:#FAEEDA;border-radius:6px;font-size:13px;color:#633806;">
          ⚠️ <strong>Resolution required by:</strong> {dl}
        </p>"""

    escalation_note = ""
    if payload.escalation_level > 0:
        escalation_note = f"""
        <div style="margin-bottom:16px;padding:10px 14px;background:#FCEBEB;border-left:3px solid #A32D2D;border-radius:0 4px 4px 0;">
          <p style="margin:0;font-size:13px;color:#A32D2D;font-weight:500;">
            Escalation Level {payload.escalation_level} — Previous approver did not respond within the required timeframe.
          </p>
        </div>"""

    return f"""
<!DOCTYPE html>
<html>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#F1EFE8;margin:0;padding:24px;">
  <div style="max-width:600px;margin:0 auto;background:#FFFFFF;border-radius:10px;overflow:hidden;border:1px solid #D3D1C7;">
    <!-- Header -->
    <div style="background:{deviation_color};padding:20px 24px;">
      <p style="margin:0;font-size:11px;color:rgba(255,255,255,0.8);letter-spacing:1px;text-transform:uppercase;">AP Policy Engine</p>
      <h1 style="margin:4px 0 0;font-size:20px;color:#FFFFFF;font-weight:600;">Deviation Detected</h1>
      <p style="margin:4px 0 0;font-size:13px;color:rgba(255,255,255,0.9);">{payload.deviation_type.value.replace('_', ' ').title()}</p>
    </div>

    <!-- Body -->
    <div style="padding:24px;">
      {escalation_note}

      <!-- Key info -->
      <table style="width:100%;border-collapse:collapse;margin-bottom:20px;background:#F1EFE8;border-radius:6px;overflow:hidden;">
        <tr>
          <td style="padding:6px 12px;color:#5F5E5A;font-size:13px;border-bottom:1px solid #E8E6DD;">Invoice Number</td>
          <td style="padding:6px 12px;color:#2C2C2A;font-size:13px;font-weight:500;border-bottom:1px solid #E8E6DD;">{payload.invoice_number}</td>
        </tr>
        <tr>
          <td style="padding:6px 12px;color:#5F5E5A;font-size:13px;border-bottom:1px solid #E8E6DD;">Vendor Name</td>
          <td style="padding:6px 12px;color:#2C2C2A;font-size:13px;font-weight:500;border-bottom:1px solid #E8E6DD;">{payload.vendor_name}</td>
        </tr>
        <tr>
          <td style="padding:6px 12px;color:#5F5E5A;font-size:13px;border-bottom:1px solid #E8E6DD;">PO Number</td>
          <td style="padding:6px 12px;color:#2C2C2A;font-size:13px;font-weight:500;border-bottom:1px solid #E8E6DD;">{payload.po_number}</td>
        </tr>
        <tr>
          <td style="padding:6px 12px;color:#5F5E5A;font-size:13px;">Recommended Action</td>
          <td style="padding:6px 12px;color:{deviation_color};font-size:13px;font-weight:600;">{payload.recommended_action.replace('_', ' ')}</td>
        </tr>
      </table>

      <!-- Deviation details -->
      <h3 style="margin:0 0 10px;font-size:14px;color:#2C2C2A;font-weight:600;">Deviation Details</h3>
      <table style="width:100%;border-collapse:collapse;background:#F1EFE8;border-radius:6px;overflow:hidden;">
        {details_rows if details_rows else '<tr><td style="padding:8px 12px;color:#888780;font-size:13px;">No additional details available.</td></tr>'}
      </table>

      {deadline_row}

      <p style="margin:20px 0 0;font-size:12px;color:#888780;">
        Sent by AP Policy Engine · {payload.created_at.strftime("%Y-%m-%d %H:%M UTC")}
      </p>
    </div>
  </div>
</body>
</html>"""


def _build_email_text(payload: NotificationPayload) -> str:
    """Plain text fallback for the email."""
    lines = [
        "AP Policy Engine — Deviation Notification",
        "=" * 50,
        f"Deviation Type: {payload.deviation_type.value}",
        f"Invoice Number: {payload.invoice_number}",
        f"Vendor Name:    {payload.vendor_name}",
        f"PO Number:      {payload.po_number}",
        f"Recommended:    {payload.recommended_action}",
        "",
        "Deviation Details:",
    ]
    for k, v in payload.deviation_details.items():
        lines.append(f"  {k}: {v}")
    if payload.resolve_deadline:
        lines.append(f"\nResolve by: {payload.resolve_deadline.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"\nSent: {payload.created_at.strftime('%Y-%m-%d %H:%M UTC')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Email backends
# ---------------------------------------------------------------------------

def _send_via_smtp(to_addresses: list[str], subject: str, html: str, text: str) -> bool:
    """Send via SMTP (production or Mailtrap)."""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings.SMTP_FROM
        msg["To"] = ", ".join(to_addresses)
        msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
            server.ehlo()
            if settings.SMTP_TLS:
                server.starttls()
            if settings.SMTP_USER and settings.SMTP_PASSWORD:
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.sendmail(settings.SMTP_FROM, to_addresses, msg.as_string())

        logger.info(f"Email sent to {to_addresses}: {subject}")
        return True
    except Exception as e:
        logger.error(f"SMTP send failed: {e}")
        return False


def _send_via_console(to_addresses: list[str], subject: str, html: str, text: str) -> bool:
    """Console backend for testing — logs the email instead of sending."""
    logger.info("=" * 60)
    logger.info(f"[EMAIL CONSOLE] To: {to_addresses}")
    logger.info(f"[EMAIL CONSOLE] Subject: {subject}")
    logger.info(f"[EMAIL CONSOLE] Body:\n{text}")
    logger.info("=" * 60)
    return True


def _get_send_fn() -> Callable:
    """Select email backend based on settings."""
    if settings.EMAIL_BACKEND == "console" or not settings.SMTP_HOST:
        return _send_via_console
    return _send_via_smtp


# ---------------------------------------------------------------------------
# Notification dispatcher
# ---------------------------------------------------------------------------

def dispatch_notification(payload: NotificationPayload) -> bool:
    """
    Send deviation notification email(s) to all relevant stakeholders.
    Returns True if at least one email was sent successfully.
    """
    recipients = [
        ROLE_EMAIL_MAP.get(role, f"{role}@company.com")
        for role in payload.notify_roles
    ]

    if not recipients:
        logger.warning(f"No recipients for notification: {payload.invoice_number}")
        return False

    subject = (
        f"[AP Alert] {payload.deviation_type.value.replace('_', ' ').title()} — "
        f"Invoice {payload.invoice_number}"
    )
    if payload.escalation_level > 0:
        subject = f"[ESCALATION L{payload.escalation_level}] " + subject

    html = _build_email_html(payload)
    text = _build_email_text(payload)

    send_fn = _get_send_fn()
    return send_fn(recipients, subject, html, text)


def schedule_notifications(
    payloads: list[NotificationPayload],
    scheduler,  # APScheduler instance
) -> int:
    """
    Schedule immediate + follow-up escalation notifications via APScheduler.
    Returns number of jobs scheduled.
    """
    jobs_scheduled = 0

    for payload in payloads:
        # Immediate notification
        scheduler.add_job(
            dispatch_notification,
            args=[payload],
            trigger="date",
            run_date=datetime.now(timezone.utc) + timedelta(seconds=5),
            id=f"notif-{payload.invoice_number}-{payload.deviation_type.value}-{jobs_scheduled}",
            replace_existing=True,
            misfire_grace_time=300,
        )
        jobs_scheduled += 1

        # 48-hour escalation job
        if payload.resolve_deadline:
            escalation_payload = payload.model_copy(update={
                "escalation_level": payload.escalation_level + 1,
                "notify_roles": _get_escalation_roles(payload.escalation_level + 1),
            })
            scheduler.add_job(
                _escalate_if_unresolved,
                args=[escalation_payload],
                trigger="date",
                run_date=payload.resolve_deadline,
                id=f"escalate-{payload.invoice_number}-{payload.deviation_type.value}",
                replace_existing=True,
                misfire_grace_time=3600,
            )
            jobs_scheduled += 1

    return jobs_scheduled


def _get_escalation_roles(level: int) -> list[str]:
    """Get the escalation recipients for a given level."""
    if level <= 0:
        return ["dept_head"]
    if level == 1:
        return ["finance_controller", "dept_head"]
    return ["cfo", "finance_controller", "internal_audit"]


def _escalate_if_unresolved(payload: NotificationPayload) -> None:
    """
    Called by APScheduler after deadline. Checks if still unresolved and escalates.
    In production, this would query the DB for resolution status.
    """
    if not payload.resolved:
        logger.warning(
            f"Deviation not resolved for {payload.invoice_number}. "
            f"Escalating to level {payload.escalation_level}."
        )
        dispatch_notification(payload)


# ---------------------------------------------------------------------------
# Standalone notification builder (used by execution engine)
# ---------------------------------------------------------------------------

def build_payloads_from_report(
    invoice: Invoice,
    triggered_rules: list[RuleResult],
    rules_map: dict[str, ExtractedRule],
) -> list[NotificationPayload]:
    """Build notification payloads for all rules that require notification."""
    payloads: list[NotificationPayload] = []

    for result in triggered_rules:
        if not result.requires_notification:
            continue

        rule = rules_map.get(result.rule_id)
        if not rule or not rule.action_config.notification:
            continue

        from datetime import timezone as tz
        resolve_deadline = None
        if rule.action_config.next_action_if_unresolved_hours:
            resolve_deadline = (
                datetime.now(tz.utc)
                + timedelta(hours=rule.action_config.next_action_if_unresolved_hours)
            )

        payload = NotificationPayload(
            invoice_number=invoice.invoice_number,
            vendor_name=invoice.vendor_name,
            po_number=invoice.po_number,
            deviation_type=result.deviation_type or DeviationType.AMOUNT_MISMATCH,
            deviation_details=result.deviation_details or {},
            recommended_action=rule.action_config.action.value,
            notify_roles=rule.action_config.notification.recipients,
            resolve_deadline=resolve_deadline,
        )
        payloads.append(payload)

    return payloads