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


def _get_display(signal: dict[str, Any]) -> str:
    """Return the display name for the pair this signal belongs to."""
    sym = signal.get("_symbol", config.MT5_SYMBOL)
    return config.PAIRS.get(sym, config.PAIRS[config.MT5_SYMBOL])["display"]


def _format_alert(signal: dict[str, Any], trigger_item: dict[str, Any] | None = None) -> str:
    """Format a signal dict into a human-readable alert string."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    display = _get_display(signal)
    levels = signal.get("key_levels") or {}
    support = levels.get("support", "N/A")
    resistance = levels.get("resistance", "N/A")

    trigger_line = ""
    if trigger_item:
        headline = trigger_item.get("headline") or trigger_item.get("title", "")
        score = trigger_item.get("score") or trigger_item.get("triage_score", "?")
        tag = trigger_item.get("tag") or trigger_item.get("triage_tag", "")
        trigger_line = f"\nTrigger  : [{score}] {headline} ({tag})\n"

    snap = signal.get("price_snapshot") or {}
    current     = snap.get("current", "N/A")
    sess_open   = snap.get("session_open", "N/A")
    sess_high   = snap.get("session_high", "N/A")
    sess_low    = snap.get("session_low", "N/A")
    sess_chg    = snap.get("session_change_pct", "")
    trend_lbl   = snap.get("trend", "")
    today_sum   = signal.get("today_summary", "")
    week_sum    = signal.get("week_summary", "")

    return (
        f"{'═' * 55}\n"
        f"  {display} SIGNAL ALERT — {now}\n"
        f"{'═' * 55}\n"
        f"  Signal     : {signal.get('signal', 'N/A')}\n"
        f"  Confidence : {signal.get('confidence', 'N/A')}\n"
        f"  Horizon    : {signal.get('time_horizon', 'N/A')}\n"
        f"{trigger_line}"
        f"\n  Price Snapshot:\n"
        f"    Current    : {current}\n"
        f"    Session    : Open {sess_open}  H {sess_high}  L {sess_low}  {sess_chg}\n"
        + (f"    Trend      : {trend_lbl}\n" if trend_lbl else "")
        + (f"\n  Today:\n  {today_sum}\n" if today_sum else "")
        + (f"\n  This Week:\n  {week_sum}\n" if week_sum else "")
        + f"\n  Rationale:\n  {signal.get('rationale', '')}\n"
        f"\n  Key Levels:\n"
        f"    Support    : {support}\n"
        f"    Resistance : {resistance}\n"
        f"\n  Invalidation:\n  {signal.get('invalidation', '')}\n"
        f"\n  Risk Note:\n  {signal.get('risk_note', '')}\n"
        + _fmt_order_preview(signal.get("_order_preview"))
        + _fmt_debate(signal.get("_debate"), signal.get("_uncertainty_score"), signal.get("_debate_veto"), signal.get("signal", ""))
        + f"{'═' * 55}"
    )


def _fmt_order_preview(preview: dict | None) -> str:
    if not preview:
        return ""
    src = " (live)" if preview.get("live_price") else " (estimated)"
    rr_warn = "  [!] R:R below 1.5 — consider skipping" if preview.get("rr_warning") else ""
    return (
        f"\n  Order Details{src}:\n"
        f"    Entry      : {preview.get('entry_price', '—')}\n"
        f"    SL         : {preview.get('sl', '—')}  (-{preview.get('sl_pips', '—')} pips)\n"
        f"    TP         : {preview.get('tp', '—')}  (+{preview.get('tp_pips', '—')} pips)\n"
        f"    Lot        : {preview.get('lot_size', '—')}\n"
        f"    Risk       : ${preview.get('risk_amount', '—')}\n"
        f"    R:R        : {preview.get('risk_reward', '—')}{rr_warn}\n"
    )


def _debate_header(signal_action: str, final_d: str, unc: int | None, veto_reason: str | None) -> str:
    """Return a single-line debate outcome label based on what actually happened."""
    if veto_reason:
        return "Ensemble Debate — VETOED"
    if not isinstance(unc, int):
        return "Ensemble Debate"
    if final_d == signal_action:
        # Judge confirmed the original direction
        if unc <= 39:
            return f"Ensemble Debate — Confirmed {signal_action} (high conviction)"
        return f"Ensemble Debate — Confirmed {signal_action} (uncertainty {unc}/100)"
    if final_d in ("Long", "Short") and final_d != signal_action:
        return f"Ensemble Debate — Direction flipped to {final_d} (uncertainty {unc}/100)"
    # Wait was blocked by code guard — signal survived
    return f"Ensemble Debate — Proceeded with caution (uncertainty {unc}/100)"


def _fmt_debate(
    debate: dict | None,
    uncertainty: int | None,
    veto_reason: str | None,
    signal_action: str = "",
) -> str:
    if not debate:
        return ""
    evals     = debate.get("evaluations") or {}
    bull      = evals.get("bull_score", "?")
    bear      = evals.get("bear_score", "?")
    win       = evals.get("winning_case", "?")
    top_risk  = evals.get("top_risk", "")
    risk_lvl  = evals.get("risk_level", "")
    unc       = uncertainty if uncertainty is not None else debate.get("uncertainty_score", "?")
    rat       = debate.get("uncertainty_rationale", "")
    final_d   = debate.get("final_decision", "")
    conf_ov   = debate.get("confidence_override") or ""
    setup     = debate.get("trade_setup") or {}
    entry_as  = setup.get("entry_assessment", "")
    sl_ass    = setup.get("sl_assessment", "")
    tp_ass    = setup.get("tp_assessment", "")
    sl_adj    = setup.get("suggested_sl_adjustment")
    tp_adj    = setup.get("suggested_tp_adjustment")
    veto      = f"\n  [!] VETO: {veto_reason}\n" if veto_reason else ""

    header    = _debate_header(signal_action, final_d, unc if isinstance(unc, int) else None, veto_reason)
    conf_str  = f" [{conf_ov}]" if conf_ov and conf_ov != "null" else ""
    adj_line  = ""
    if sl_adj or tp_adj:
        adj_line = (
            f"\n  Adjustments:\n"
            + (f"    SL adjusted -> {sl_adj}\n" if sl_adj else "")
            + (f"    TP adjusted -> {tp_adj}\n" if tp_adj else "")
        )
    assess_lines = ""
    if entry_as:
        assess_lines += f"    Entry      : {entry_as}\n"
    if sl_ass:
        assess_lines += f"    SL         : {sl_ass}\n"
    if tp_ass:
        assess_lines += f"    TP         : {tp_ass}\n"

    risk_line = f"    Top Risk   : [{risk_lvl}] {top_risk}\n" if top_risk else ""

    return (
        f"\n  {header}:\n"
        f"    Scores     : Bull {bull} | Bear {bear} | Winner: {win}\n"
        f"    Uncertainty: {unc}/100  — {rat}\n"
        + risk_line
        + (f"    Verdict    : {final_d}{conf_str}\n" if final_d else "")
        + (f"  Trade Setup Assessment:\n" + assess_lines if assess_lines else "")
        + adj_line
        + veto
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

    display = _get_display(signal)
    signal_label = signal.get("signal", "SIGNAL")
    confidence = signal.get("confidence", "")
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subject = f"{display} {signal_label} [{confidence}] — {now_str}"
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


def _build_signal_blocks(text: str, signal_id: str | None, signal_label: str) -> list[dict]:
    """
    Wrap the signal text in Slack Block Kit blocks and append Approve/Reject buttons
    for actionable signals (Long/Short with a signal_id).

    Splits text into ≤2900-char sections to respect Slack's per-block limit.
    """
    blocks: list[dict] = []
    chunk_size = 2900
    for i in range(0, len(text), chunk_size):
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": text[i : i + chunk_size]},
        })

    if signal_id and signal_label in ("Long", "Short"):
        btn_text = ":chart_with_upwards_trend: Execute Long" if signal_label == "Long" else ":chart_with_downwards_trend: Execute Short"
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": btn_text, "emoji": True},
                    "style": "primary",
                    "action_id": "approve_signal",
                    "value": signal_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":x: Reject", "emoji": True},
                    "style": "danger",
                    "action_id": "reject_signal",
                    "value": signal_id,
                },
            ],
        })
    return blocks


def _notify_slack(signal: dict[str, Any], trigger_item: dict[str, Any] | None) -> None:
    webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        logger.warning("SLACK_WEBHOOK_URL not set — skipping Slack notification")
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    display = _get_display(signal)
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

    # Order preview block
    order_preview = signal.get("_order_preview") or {}
    order_line = ""
    if order_preview:
        entry   = order_preview.get("entry_price", "—")
        sl      = order_preview.get("sl", "—")
        tp      = order_preview.get("tp", "—")
        sl_p    = order_preview.get("sl_pips", "—")
        tp_p    = order_preview.get("tp_pips", "—")
        lot     = order_preview.get("lot_size", "—")
        risk    = order_preview.get("risk_amount", "—")
        rr      = order_preview.get("risk_reward", "—")
        live    = " _(live)_" if order_preview.get("live_price") else " _(estimated)_"
        rr_warn = "  :warning: _R:R below 1.5_" if order_preview.get("rr_warning") else ""
        size_reason = signal.get("_size_reason", "")
        size_line   = f"\n>_Sizing: {size_reason}_" if size_reason else ""
        order_line = (
            f"\n\n*Order Details*{live}\n"
            f">Entry `{entry}`  SL `{sl}` (-{sl_p}p)  TP `{tp}` (+{tp_p}p)\n"
            f">Lot `{lot}`  Risk `${risk}`  R:R `{rr}`{rr_warn}"
            f"{size_line}"
        )

    # Ensemble debate block
    debate       = signal.get("_debate") or {}
    debate_line  = ""
    if debate:
        evals    = debate.get("evaluations") or {}
        bull_s   = evals.get("bull_score", "?")
        bear_s   = evals.get("bear_score", "?")
        win      = evals.get("winning_case", "?")
        top_risk = evals.get("top_risk", "")
        risk_lvl = evals.get("risk_level", "")
        unc      = signal.get("_uncertainty_score", debate.get("uncertainty_score", "?"))
        unc_rat  = debate.get("uncertainty_rationale", "")
        final_d  = debate.get("final_decision", "")
        conf_ov  = debate.get("confidence_override") or ""
        veto     = signal.get("_debate_veto", "")
        setup    = debate.get("trade_setup") or {}
        entry_as = setup.get("entry_assessment", "")
        sl_ass   = setup.get("sl_assessment", "")
        tp_ass   = setup.get("tp_assessment", "")
        sl_adj   = setup.get("suggested_sl_adjustment")
        tp_adj   = setup.get("suggested_tp_adjustment")

        unc_emoji = (
            ":large_green_circle:" if isinstance(unc, int) and unc <= 39 else
            ":large_yellow_circle:" if isinstance(unc, int) and unc <= 69 else
            ":red_circle:"
        )
        risk_emoji = {
            "High":   ":red_circle:",
            "Medium": ":large_yellow_circle:",
            "Low":    ":large_green_circle:",
        }.get(risk_lvl, ":white_circle:")

        # Debate outcome header — reflects what actually happened
        header = _debate_header(
            signal_action=signal_label,
            final_d=final_d,
            unc=unc if isinstance(unc, int) else None,
            veto_reason=veto or None,
        )

        debate_line = (
            f"\n\n*{header}*\n"
            f">:chart_with_upwards_trend: Bull `{bull_s}` | "
            f":chart_with_downwards_trend: Bear `{bear_s}` | Winner: *{win}*\n"
            f">{unc_emoji} Uncertainty `{unc}/100` — {unc_rat}"
        )
        if top_risk:
            debate_line += f"\n>{risk_emoji} *Top risk [{risk_lvl}]:* {top_risk}"
        if final_d:
            conf_str = f" [{conf_ov}]" if conf_ov and conf_ov != "null" else ""
            if veto:
                debate_line += f"\n>:no_entry: *Vetoed — {veto}*"
            elif final_d != signal_label:
                debate_line += f"\n>:scales: Judge adjusted to *{final_d}*{conf_str}"
        if entry_as:
            debate_line += f"\n>:mag: Entry: {entry_as}"
        if sl_ass:
            debate_line += f"\n>:mag: SL: {sl_ass}"
        if tp_ass:
            debate_line += f"\n>:mag: TP: {tp_ass}"
        if sl_adj or tp_adj:
            debate_line += (
                "\n>:wrench: *Adjustments:*"
                + (f" SL -> `{sl_adj}`" if sl_adj else "")
                + (f" TP -> `{tp_adj}`" if tp_adj else "")
            )

    signal_id = signal.get("_signal_id")
    source = signal.get("_source", "job1")

    # Source label for Job 2 position alerts
    source_label = " _(Position Monitor)_" if source == "job2" else ""

    snap = signal.get("price_snapshot") or {}
    current       = snap.get("current")
    session_open  = snap.get("session_open")
    session_high  = snap.get("session_high")
    session_low   = snap.get("session_low")
    session_chg   = snap.get("session_change_pct", "")
    trend_label   = snap.get("trend", "")

    price_line = ""
    if current:
        price_line = f"\n>*Price:* `{current}`"
        if session_open:
            price_line += (
                f"  Open `{session_open}`  "
                f"H `{session_high}`  L `{session_low}`  {session_chg}"
            )
        if trend_label:
            price_line += f"\n>*Trend:* {trend_label}"

    today_sum = signal.get("today_summary", "")
    week_sum  = signal.get("week_summary", "")
    summary_lines = ""
    if today_sum:
        summary_lines += f"\n>*Today:* {today_sum}"
    if week_sum:
        summary_lines += f"\n>*This week:* {week_sum}"

    text = (
        f"{emoji} *{display} {signal_label}* {confidence_emoji} `{confidence}` — "
        f"_{signal.get('time_horizon', 'N/A')}_{source_label}\n"
        f"_{now}_{trigger_line}"
        f"{price_line}"
        f"{summary_lines}\n\n"
        f">*Rationale:* {signal.get('rationale', '')}\n\n"
        f">*Key Levels:* Support `{support}` | Resistance `{resistance}`\n"
        f">*Invalidation:* {signal.get('invalidation', '')}\n"
        f">*Risk Note:* {signal.get('risk_note', '')}"
        f"{order_line}"
        f"{debate_line}"
        + (f"\n\n:pencil: _To modify SL/TP/lot before executing: type `chat` and ask me_"
           if signal_id and signal_label not in ("Wait", "Hold") else "")
    )

    # Build Block Kit payload — buttons for actionable signals, plain text fallback for others
    payload: dict[str, Any] = {"text": text}
    if signal_id and signal_label not in ("Wait", "Hold"):
        payload["blocks"] = _build_signal_blocks(text, signal_id, signal_label)

    try:
        resp = requests.post(
            webhook_url,
            data=json.dumps(payload),
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

    # Record Long/Short signals in the trade journal
    if signal.get("signal") in ("Long", "Short") and signal.get("_signal_id"):
        try:
            from analysis.trade_journal import record_signal
            record_signal(signal)
        except Exception as e:
            logger.error(f"Journal record_signal failed: {e}")


def notify_text(text: str) -> None:
    """Send a plain-text message to all configured notification channels."""
    for channel in config.NOTIFICATION_CHANNELS:
        if channel == "print":
            print(text)
        elif channel == "slack":
            webhook_url = os.getenv("SLACK_WEBHOOK_URL")
            if not webhook_url:
                continue
            try:
                requests.post(
                    webhook_url,
                    data=json.dumps({"text": text}),
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                ).raise_for_status()
            except Exception as e:
                logger.error(f"notify_text Slack failed: {e}")
        elif channel == "email":
            logger.debug("notify_text: email channel skipped for plain-text messages")
