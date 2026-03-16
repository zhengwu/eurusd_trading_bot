"""Slack Socket Mode bot — command interface for EUR/USD agent.

Requires:
  pip install slack-bolt
  SLACK_BOT_TOKEN="xoxb-..."  in .env
  SLACK_APP_TOKEN="xapp-..."  in .env

Commands (send as DM or @mention in the alerts channel):
  scan / update    — force an immediate news scan (bypasses market hours)
  positions        — run Job 2 position check now
  status           — show system status (cooldown, MT5, pending signals)
  collect          — trigger end-of-day data collection
  pending          — list pending signals awaiting approval
  approve <ID>     — approve a pending signal for Job 3
  reject <ID>      — reject a pending signal
  help             — show this command list

Runs as a background thread inside agents/job1_opportunity.py.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

import anthropic
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import config
from utils.logger import get_logger

logger = get_logger(__name__)

_HELP_TEXT = (
    "*EUR/USD Agent Commands*\n"
    "> `scan` / `update` — force an immediate news scan (bypasses market hours)\n"
    "> `positions` — run Job 2 position check now\n"
    "> `status` — show system status (cooldown, MT5, pending signals)\n"
    "> `collect` — trigger end-of-day data collection\n"
    "> `pending` — list pending signals awaiting approval\n"
    "> `approve <ID> [%]` — approve a signal; for Trim, optionally specify % to close (default 50%)\n"
    "> `reject <ID>` — reject a pending signal\n"
    "> `help` — show this message\n"
    "> _Any other message starts a conversation — ask me anything about the market or your positions_"
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _run_in_thread(say: Callable, fn: Callable, *args) -> None:
    """Run fn(say, *args) in a daemon thread; send errors back to Slack."""
    def wrapper():
        try:
            fn(say, *args)
        except Exception as e:
            logger.error(f"Slack command error: {e}", exc_info=True)
            say(f":x: Unexpected error: {e}")
    threading.Thread(target=wrapper, daemon=True).start()


# ── command implementations ───────────────────────────────────────────────────

def _do_scan(say: Callable) -> None:
    say(":mag: Starting news scan (forced)…")
    from triage.scanner import run_scan
    triggered = run_scan(force=True)
    if triggered:
        say(
            f":bell: Scan complete — {len(triggered)} article(s) triggered full analysis. "
            "Check above for the signal."
        )
    else:
        say(":white_check_mark: Scan complete — no high-score articles found.")


def _do_positions(say: Callable) -> None:
    from mt5.connector import is_connected
    if not is_connected():
        say(":x: MT5 is not connected — cannot check positions.")
        return
    say(":eyes: Checking open EURUSD positions…")
    from agents.job2_position import run_position_check
    count = run_position_check()
    if count == 0:
        say(":white_check_mark: No open EURUSD positions.")
    else:
        say(
            f":white_check_mark: Position check complete — {count} position(s) analysed. "
            "Check above for recommendations."
        )


def _do_status(say: Callable) -> None:
    from mt5.connector import is_connected
    from pipeline.signal_store import list_signals

    # Cooldown
    cooldown_lock = config.DATA_DIR / ".cooldown_lock"
    cooldown_str = ":white_check_mark: Inactive"
    if cooldown_lock.exists():
        try:
            ts = datetime.fromisoformat(cooldown_lock.read_text(encoding="utf-8").strip())
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            expiry = ts + timedelta(minutes=config.COOLDOWN_MINUTES)
            now = datetime.now(timezone.utc)
            if now < expiry:
                remaining = int((expiry - now).total_seconds() / 60)
                cooldown_str = f":pause_button: Active — {remaining} min remaining"
        except Exception:
            pass

    # MT5
    mt5_str = ":large_green_circle: Connected" if is_connected() else ":red_circle: Disconnected"

    # Pending signals
    pending = list_signals(status="pending")
    pending_str = f"{len(pending)} pending" if pending else "none"

    say(
        f"*EUR/USD Agent Status*\n"
        f">*Cooldown:* {cooldown_str}\n"
        f">*MT5:* {mt5_str}\n"
        f">*Pending signals:* {pending_str}"
    )


def _do_collect(say: Callable) -> None:
    say(":inbox_tray: Starting end-of-day data collection…")
    from pipeline.daily_collector import run_daily_collection
    from utils.date_utils import today_str_utc
    run_daily_collection(today_str_utc())
    say(":white_check_mark: Data collection complete.")


def _do_pending(say: Callable) -> None:
    from pipeline.signal_store import list_signals
    pending = list_signals(status="pending")
    if not pending:
        say(":white_check_mark: No pending signals.")
        return
    lines = [f"*Pending signals ({len(pending)}):*"]
    for s in pending:
        lines.append(
            f">*{s['id']}* — `{s.get('signal')}` [{s.get('confidence')}] "
            f"from `{s.get('source', '?')}` | created {s.get('created_at', '')[:16]} UTC"
        )
    say("\n".join(lines))


def _do_approve(say: Callable, signal_id: str, trim_pct: float | None = None) -> None:
    from agents.job3_executor import approve_and_execute
    pct_str = f" (trim {trim_pct}%)" if trim_pct is not None else ""
    say(f":hourglass: Approving and executing signal `{signal_id}`{pct_str}…")
    result = approve_and_execute(signal_id, trim_pct=trim_pct)
    if result.get("ok"):
        ticket = result.get("ticket", "?")
        price = result.get("price", "?")
        volume = result.get("volume", "?")
        say(
            f":white_check_mark: Signal `{signal_id}` executed!\n"
            f">*Ticket:* `{ticket}`\n"
            f">*Fill price:* `{price}`\n"
            f">*Volume:* `{volume}` lots"
        )
    else:
        say(f":x: Execution failed: {result.get('error')}")


def _do_reject(say: Callable, signal_id: str) -> None:
    from pipeline.signal_store import reject_signal
    if reject_signal(signal_id):
        say(f":white_check_mark: Signal `{signal_id}` rejected.")
    else:
        say(f":x: Signal `{signal_id}` not found.")


def _do_introduce(say: Callable) -> None:
    say(
        "*Hi! I'm your EUR/USD AI Trading Agent.* :robot_face:\n\n"
        "*What I do:*\n"
        "> :newspaper: *Job 1 — News Scanner*: Every 30 minutes I scan financial news "
        "(NewsAPI, AlphaVantage, EODHD), score headlines 1–10 for EUR/USD relevance, "
        "and run a full Claude Sonnet analysis when something important happens. "
        "Signals are sent here to Slack.\n"
        "> :eyes: *Job 2 — Position Monitor*: Every 30 minutes I check your open MT5 "
        "positions and recommend Hold / Trim / Exit based on current market conditions.\n"
        "> :robot_face: *Job 3 — Trade Executor* (coming soon): Will execute approved "
        "signals directly on MT5.\n\n"
        "*How to talk to me:*\n"
        "> Just ask naturally — _\"scan the news\"_, _\"check my positions\"_, "
        "_\"what's the system status?\"_ — or use the keyword shortcuts below.\n\n"
        + _HELP_TEXT
    )


# ── NLP intent classifier ─────────────────────────────────────────────────────

_INTENT_SYSTEM = """\
You are a command parser for a EUR/USD trading agent Slack bot.
Given a user message, identify the intent and return JSON only.

Possible intents:
- "scan"       — user wants to scan/fetch/update news or market data
- "positions"  — user wants to check open positions or portfolio
- "status"     — user wants system status, health check, or overview
- "collect"    — user wants to collect/save end-of-day data
- "pending"    — user wants to see pending or waiting signals
- "approve"    — user wants to approve a signal (extract signal_id if present)
- "reject"     — user wants to reject a signal (extract signal_id if present)
- "introduce"  — user wants to know what the bot does, or is greeting it
- "help"       — user wants a list of commands
- "unknown"    — cannot determine intent

Return JSON with this exact shape:
{"intent": "<intent>", "signal_id": "<8-char ID or null>"}
"""


def _classify_intent(text: str) -> tuple[str, str | None]:
    """
    Use Claude Haiku to classify natural language into a command intent.
    Returns (intent, signal_id_or_None).
    """
    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        msg = client.messages.create(
            model=config.TRIAGE_MODEL,
            max_tokens=64,
            system=_INTENT_SYSTEM,
            messages=[{"role": "user", "content": text}],
        )
        raw = msg.content[0].text.strip()
        if not raw:
            return "unknown", None
        # Strip markdown code fences if present
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
        parsed = json.loads(raw)
        intent = parsed.get("intent", "unknown")
        signal_id = parsed.get("signal_id") or None
        return intent, signal_id
    except Exception as e:
        logger.warning(f"Intent classification failed: {e}")
        return "unknown", None


# ── dispatcher ────────────────────────────────────────────────────────────────

def _dispatch(text: str, say: Callable) -> bool:
    """
    Parse raw message text, strip bot mentions, and route to the right handler.
    Falls back to Claude Haiku NLP classification for natural language.
    Returns True if the command was recognised.
    """
    clean = re.sub(r"<@[A-Z0-9]+>", "", text).strip().lower()
    clean = re.sub(r"\s+", " ", clean)  # collapse multiple spaces/tabs

    # Fast path — exact keyword matching
    if re.fullmatch(r"scan|update", clean):
        _run_in_thread(say, _do_scan)
    elif re.fullmatch(r"positions?", clean):
        _run_in_thread(say, _do_positions)
    elif clean == "status":
        _run_in_thread(say, _do_status)
    elif clean == "collect":
        _run_in_thread(say, _do_collect)
    elif re.fullmatch(r"pending|signals", clean):
        _run_in_thread(say, _do_pending)
    elif m := re.fullmatch(r"approve\s+([A-Za-z0-9]{8})(?:\s+([\d.]+))?", clean):
        trim_pct = float(m.group(2)) if m.group(2) else None
        _run_in_thread(say, _do_approve, m.group(1).upper(), trim_pct)
    elif m := re.fullmatch(r"reject\s+([A-Za-z0-9]{8})", clean):
        _run_in_thread(say, _do_reject, m.group(1).upper())
    elif clean == "help":
        say(_HELP_TEXT)
    elif re.fullmatch(r"hi|hello|hey|introduce|who are you", clean):
        _run_in_thread(say, _do_introduce)
    else:
        # NLP fallback — classify with Claude Haiku
        intent, signal_id = _classify_intent(clean)
        if intent == "scan":
            _run_in_thread(say, _do_scan)
        elif intent == "positions":
            _run_in_thread(say, _do_positions)
        elif intent == "status":
            _run_in_thread(say, _do_status)
        elif intent == "collect":
            _run_in_thread(say, _do_collect)
        elif intent == "pending":
            _run_in_thread(say, _do_pending)
        elif intent == "approve" and signal_id:
            _run_in_thread(say, _do_approve, signal_id.upper(), None)  # NLP path: no pct
        elif intent == "reject" and signal_id:
            _run_in_thread(say, _do_reject, signal_id.upper())
        elif intent in ("introduce", "help"):
            _run_in_thread(say, _do_introduce)
        else:
            return False
    return True


# ── chat handler ──────────────────────────────────────────────────────────────

def _make_threaded_say(say: Callable, thread_ts: str) -> Callable:
    """Wrap say() so all replies go into a specific Slack thread."""
    def _say(text: str) -> None:
        say(text=text, thread_ts=thread_ts)
    return _say


def _do_chat(say: Callable, thread_ts: str, text: str) -> None:
    """Run a conversational exchange via Job 4 and reply in the thread."""
    from agents.job4_chat import chat
    say(":thought_balloon: Thinking…")
    reply, proposals = chat(thread_ts, text)
    if reply:
        say(reply)
    for signal_id in proposals:
        say(
            f":pencil: *Order proposed — Signal ID: `{signal_id}`*\n"
            f">:white_check_mark: To execute: `approve {signal_id}`\n"
            f">:x: To cancel:  `reject {signal_id}`"
        )


# ── Slack event listeners ─────────────────────────────────────────────────────

def _make_app() -> App:
    return App(token=os.getenv("SLACK_BOT_TOKEN", ""))


_app: App | None = None


def _get_app() -> App:
    global _app
    if _app is None:
        _app = _make_app()

        @_app.event("message")
        def handle_message(event, say):
            # Ignore bot messages and subtypes (edits, deletions, etc.)
            if event.get("bot_id") or event.get("subtype"):
                return

            text = event.get("text", "")
            thread_ts = event.get("thread_ts")
            is_dm = event.get("channel", "").startswith("D")

            if thread_ts:
                # Thread reply — route to chat if it's an active chat session
                from agents.job4_chat import get_active_thread_ids
                if thread_ts in get_active_thread_ids():
                    tsay = _make_threaded_say(say, thread_ts)
                    _run_in_thread(tsay, _do_chat, thread_ts, text)
            elif is_dm:
                # DM — try commands first, fall back to chat
                if not _dispatch(text, say):
                    dm_thread_ts = event["ts"]
                    tsay = _make_threaded_say(say, dm_thread_ts)
                    _run_in_thread(tsay, _do_chat, dm_thread_ts, text)
            else:
                # Channel message (non-thread) — commands only, ignore the rest
                _dispatch(text, say)

        @_app.event("app_mention")
        def handle_mention(event, say):
            text = event.get("text", "")
            thread_ts = event.get("thread_ts") or event["ts"]
            tsay = _make_threaded_say(say, thread_ts)

            # @mention inside a thread → always continue/start chat in that thread
            if event.get("thread_ts"):
                _run_in_thread(tsay, _do_chat, thread_ts, text)
                return

            # Top-level @mention → try commands first, fall back to chat
            if _dispatch(text, say):
                return
            _run_in_thread(tsay, _do_chat, thread_ts, text)

    return _app


# ── public API ────────────────────────────────────────────────────────────────

def run_slack_bot() -> None:
    """Start the Slack Socket Mode bot. Blocks until stopped."""
    import time

    app_token = os.getenv("SLACK_APP_TOKEN")
    bot_token = os.getenv("SLACK_BOT_TOKEN")

    if not app_token:
        logger.warning("SLACK_APP_TOKEN not set — Slack bot will not start")
        return
    if not bot_token:
        logger.warning("SLACK_BOT_TOKEN not set — Slack bot will not start")
        return

    logger.info("Starting Slack Socket Mode bot…")
    handler = SocketModeHandler(_get_app(), app_token)
    # Use connect() instead of start() — start() installs signal handlers
    # which raises ValueError when called from a background thread.
    handler.connect()
    logger.info("Slack bot connected")
    while True:
        time.sleep(1)
