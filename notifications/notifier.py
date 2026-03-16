"""Alert delivery — dispatches to all configured notification channels.

Channels are configured in config.NOTIFICATION_CHANNELS:
  "print"  — console output
  "slack"  — Slack Incoming Webhook (SLACK_WEBHOOK_URL in .env)
  "email"  — SMTP to config.NOTIFICATION_EMAIL_TO
"""
from __future__ import annotations

import json
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import requests
from dotenv import load_dotenv

import config
from utils.logger import get_logger

load_dotenv()
logger = get_logger(__name__)


def _format_alert(signal: dict[str, Any], trigger_item: dict[str, Any] | None = None) -> str:
    """Format a signal dict into a human-readable alert string."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    levels = signal.get("key_levels") or {}
    support = levels.get("support", "N/A")
    resistance = levels.get("resistance", "N/A")

    trigger_line = ""
    if trigger_item:
        headline = trigger_item.get("headline") or trigger_item.get("title", "")
        score = trigger_item.get("score") or trigger_item.get("triage_score", "?")
        tag = trigger_item.get("tag") or trigger_item.get("triage_tag", "")
        trigger_line = f"\nTrigger  : [{score}] {headline} ({tag})\n"

    return (
        f"{'═' * 55}\n"
        f"  EUR/USD SIGNAL ALERT — {now}\n"
        f"{'═' * 55}\n"
        f"  Signal     : {signal.get('signal', 'N/A')}\n"
        f"  Confidence : {signal.get('confidence', 'N/A')}\n"
        f"  Horizon    : {signal.get('time_horizon', 'N/A')}\n"
        f"{trigger_line}"
        f"\n  Rationale:\n  {signal.get('rationale', '')}\n"
        f"\n  Key Levels:\n"
        f"    Support    : {support}\n"
        f"    Resistance : {resistance}\n"
        f"\n  Invalidation:\n  {signal.get('invalidation', '')}\n"
        f"\n  Risk Note:\n  {signal.get('risk_note', '')}\n"
        f"{'═' * 55}"
    )


def _notify_print(signal: dict[str, Any], trigger_item: dict[str, Any] | None) -> None:
    print(_format_alert(signal, trigger_item))


def _notify_email(signal: dict[str, Any], trigger_item: dict[str, Any] | None) -> None:
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASSWORD")

    if not smtp_user or not smtp_pass:
        logger.warning("SMTP credentials not set — skipping email notification")
        return

    signal_label = signal.get("signal", "SIGNAL")
    confidence = signal.get("confidence", "")
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subject = f"EUR/USD {signal_label} [{confidence}] — {now_str}"
    body = _format_alert(signal, trigger_item)

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = smtp_user
        msg["To"] = config.NOTIFICATION_EMAIL_TO
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, config.NOTIFICATION_EMAIL_TO, msg.as_string())

        logger.info(f"Email alert sent to {config.NOTIFICATION_EMAIL_TO}")
    except Exception as e:
        logger.error(f"Email notification failed: {e}")


def _notify_slack(signal: dict[str, Any], trigger_item: dict[str, Any] | None) -> None:
    webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        logger.warning("SLACK_WEBHOOK_URL not set — skipping Slack notification")
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    levels = signal.get("key_levels") or {}
    support = levels.get("support", "N/A")
    resistance = levels.get("resistance", "N/A")

    signal_label = signal.get("signal", "N/A")
    confidence = signal.get("confidence", "N/A")

    # Emoji per signal direction
    emoji = {"Long": ":chart_with_upwards_trend:", "Short": ":chart_with_downwards_trend:"}.get(
        signal_label, ":pause_button:"
    )
    confidence_emoji = {"High": ":large_green_circle:", "Medium": ":large_yellow_circle:", "Low": ":red_circle:"}.get(
        confidence, ""
    )

    trigger_line = ""
    if trigger_item:
        headline = trigger_item.get("headline") or trigger_item.get("title", "")
        score = trigger_item.get("score") or trigger_item.get("triage_score", "?")
        tag = trigger_item.get("tag") or trigger_item.get("triage_tag", "")
        trigger_line = f"\n>*Trigger* [{score}]: {headline} `{tag}`"

    # Show signal ID + approval instructions if this signal is pending execution
    signal_id = signal.get("_signal_id")
    source = signal.get("_source", "job1")
    approval_line = ""
    if signal_id and signal_label not in ("Wait", "Hold"):
        approval_line = (
            f"\n\n:white_check_mark: *To execute:* `python -m agents.job3_executor --approve {signal_id}`"
            f"\n:x: *To reject:*  `python -m agents.job3_executor --reject {signal_id}`"
        )

    # Source label for Job 2 position alerts
    source_label = " _(Position Monitor)_" if source == "job2" else ""

    text = (
        f"{emoji} *EUR/USD {signal_label}* {confidence_emoji} `{confidence}` — "
        f"_{signal.get('time_horizon', 'N/A')}_{source_label}\n"
        f"_{now}_{trigger_line}\n\n"
        f">*Rationale:* {signal.get('rationale', '')}\n\n"
        f">*Key Levels:* Support `{support}` | Resistance `{resistance}`\n"
        f">*Invalidation:* {signal.get('invalidation', '')}\n"
        f">*Risk Note:* {signal.get('risk_note', '')}"
        f"{approval_line}"
    )

    try:
        resp = requests.post(
            webhook_url,
            data=json.dumps({"text": text}),
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Slack alert sent")
    except Exception as e:
        logger.error(f"Slack notification failed: {e}")


def notify(signal: dict[str, Any], trigger_item: dict[str, Any] | None = None) -> None:
    """Dispatch signal to all configured notification channels."""
    for channel in config.NOTIFICATION_CHANNELS:
        if channel == "print":
            _notify_print(signal, trigger_item)
        elif channel == "slack":
            _notify_slack(signal, trigger_item)
        elif channel == "email":
            _notify_email(signal, trigger_item)
        else:
            logger.warning(f"Unknown notification channel: {channel!r}")
