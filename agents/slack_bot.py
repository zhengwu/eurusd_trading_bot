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
    "> `analyze` / `outlook` — full market analysis: news + prices + technicals → trading signal\n"
    "> `chat` — run full analysis, then open a conversation seeded with the results\n"
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


def _do_analyze(say: Callable) -> None:
    """Run the full Job 1 analysis pipeline on demand, bypassing news triage."""
    say(":brain: Running full market analysis (news + prices + technicals)…")
    try:
        from analysis.context_builder import build_context
        from analysis.full_analysis_prompt import run_full_analysis
        from analysis.signal_formatter import format_signal
        from notifications.notifier import notify

        context = build_context(trigger_item=None)
        raw_signal = run_full_analysis(context)
        signal = format_signal(raw_signal)

        if signal.get("signal") in ("Long", "Short"):
            from pipeline.signal_store import save_pending_signal
            from agents.job3_executor import compute_order_preview
            preview = compute_order_preview(signal)
            if preview:
                signal["_order_preview"] = preview
            signal_id = save_pending_signal(signal, source="manual")
            signal["_signal_id"] = signal_id
            signal["_source"] = "manual"

        notify(signal, trigger_item=None)
        action = signal.get("signal", "Hold")
        confidence = signal.get("confidence", "")
        say(
            f":white_check_mark: Analysis complete — *{action}* [{confidence}]. "
            "Check above for the full signal."
        )
    except Exception as e:
        logger.error(f"On-demand analysis failed: {e}", exc_info=True)
        say(f":x: Analysis failed: {e}")


def _do_chat_command(say: Callable, thread_ts: str) -> None:
    """Start an analysis-seeded chat session in the given Slack thread."""
    say(":brain: Running full market analysis to brief you…")
    try:
        from analysis.context_builder import build_context
        from analysis.full_analysis_prompt import run_full_analysis
        from analysis.signal_formatter import format_signal
        from agents.job4_chat import seed_thread

        context = build_context(trigger_item=None)
        raw_signal = run_full_analysis(context)
        signal = format_signal(raw_signal)

        action     = signal.get("signal", "Wait")
        confidence = signal.get("confidence", "")
        horizon    = signal.get("time_horizon", "")
        rationale  = signal.get("rationale", "")
        levels     = signal.get("key_levels", {})
        invalidate = signal.get("invalidation", "")
        risk       = signal.get("risk_note", "")
        today_sum  = signal.get("today_summary", "")
        week_sum   = signal.get("week_summary", "")
        snap       = signal.get("price_snapshot") or {}

        support    = levels.get("support", "—")
        resistance = levels.get("resistance", "—")

        # Price snapshot line
        price_line = ""
        if snap.get("current"):
            price_line = (
                f"*Price:* `{snap['current']}`"
                + (f"  Open `{snap['session_open']}`  "
                   f"H `{snap['session_high']}`  L `{snap['session_low']}`  "
                   f"{snap.get('session_change_pct', '')}" if snap.get("session_open") else "")
                + (f"\n*Trend:* {snap['trend']}" if snap.get("trend") else "")
                + "\n"
            )

        briefing = (
            f"*EUR/USD Market Analysis*\n\n"
            + (price_line)
            + (f"*Today:* {today_sum}\n" if today_sum else "")
            + (f"*This week:* {week_sum}\n" if week_sum else "")
            + f"\n*Signal:* {action} [{confidence}] | {horizon}\n"
            f"*Rationale:* {rationale}\n"
            f"*Key levels:* Support `{support}` | Resistance `{resistance}`\n"
            + (f"*Invalidation:* {invalidate}\n" if invalidate else "")
            + (f"*Risk note:* {risk}\n" if risk else "")
        )

        # Seed the Job 4 thread so Claude has the analysis in its history
        seed_thread(thread_ts, briefing)

        # Save Long/Short signals to the pending store
        if action in ("Long", "Short"):
            from pipeline.signal_store import save_pending_signal
            from agents.job3_executor import compute_order_preview
            preview = compute_order_preview(signal)
            if preview:
                signal["_order_preview"] = preview
            signal_id = save_pending_signal(signal, source="manual")
            signal["_signal_id"] = signal_id
            preview = signal.get("_order_preview") or {}
            order_line = ""
            if preview:
                order_line = (
                    f"\n*Order Details:*"
                    + (" _(live)_" if preview.get("live_price") else " _(estimated)_") + "\n"
                    f">Entry `{preview.get('entry_price')}`  "
                    f"SL `{preview.get('sl')}` (-{preview.get('sl_pips')}p)  "
                    f"TP `{preview.get('tp')}` (+{preview.get('tp_pips')}p)\n"
                    f">Lot `{preview.get('lot_size')}`  "
                    f"Risk `${preview.get('risk_amount')}`  "
                    f"R:R `{preview.get('risk_reward')}`"
                )
            briefing += (
                order_line
                + f"\n:pencil: Signal `{signal_id}` pending."
                + f" `approve {signal_id}` to execute"
                + " or ask me to adjust SL/TP/lot."
            )

        say(
            briefing
            + "\n\n_Chat session open. Ask me anything about this analysis or the market. "
            "Say `end chat` when you're done._"
        )
    except Exception as e:
        logger.error(f"Chat command failed: {e}", exc_info=True)
        say(f":x: Analysis failed: {e}")


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
- "analyze"    — user wants a full market analysis, trading outlook, or trade idea/opportunity
- "chat"       — user wants to start an interactive chat session seeded with market analysis
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
    elif re.fullmatch(r"analyze|analyse|analysis|outlook", clean):
        _run_in_thread(say, _do_analyze)
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
        elif intent == "analyze":
            _run_in_thread(say, _do_analyze)
        elif intent == "chat":
            # NLP can't easily get thread_ts here — fall through to unknown so
            # the caller's event handler picks it up with proper thread_ts
            return False
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
                from agents.job4_chat import get_active_thread_ids, close_thread
                if thread_ts in get_active_thread_ids():
                    tsay = _make_threaded_say(say, thread_ts)
                    if re.search(r"\bend\s+chat\b", text.lower()):
                        close_thread(thread_ts)
                        tsay(":wave: Chat session ended. Start a new one anytime with `chat`.")
                    else:
                        _run_in_thread(tsay, _do_chat, thread_ts, text)
            elif is_dm:
                # DM — check for "chat" command, then try other commands, fall back to chat
                clean = re.sub(r"\s+", " ", text.strip().lower())
                if re.fullmatch(r"chat", clean):
                    dm_thread_ts = event["ts"]
                    tsay = _make_threaded_say(say, dm_thread_ts)
                    _run_in_thread(tsay, _do_chat_command, dm_thread_ts)
                elif not _dispatch(text, say):
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
            clean = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
            clean = re.sub(r"\s+", " ", clean).lower()

            # "end chat" — close an active thread
            if re.search(r"\bend\s+chat\b", clean):
                from agents.job4_chat import close_thread
                if close_thread(thread_ts):
                    tsay(":wave: Chat session ended. Start a new one anytime with `chat`.")
                else:
                    tsay(":grey_question: No active chat session to end.")
                return

            # @mention inside a thread → always continue/start chat in that thread
            if event.get("thread_ts"):
                _run_in_thread(tsay, _do_chat, thread_ts, text)
                return

            # "chat" command at top level → start analysis-seeded chat
            if re.fullmatch(r"chat", clean):
                _run_in_thread(tsay, _do_chat_command, thread_ts)
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
