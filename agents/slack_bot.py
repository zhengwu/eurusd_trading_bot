"""Slack Socket Mode bot — command interface for multi-pair forex agent.

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

def _pairs_str() -> str:
    return " | ".join(config.ACTIVE_PAIRS)


_HELP_TEXT = (
    "*Forex Multi-Pair Agent Commands*\n"
    f"> Active pairs: see `status`\n"
    "> `scan` / `update` — force an immediate news scan across all pairs\n"
    "> `scan GBPUSD` — scan a specific pair only\n"
    "> `analyze` / `outlook` — full analysis for the primary pair\n"
    "> `analyze GBPUSD` — full analysis for a specific pair\n"
    "> `chat` — analysis-seeded conversation (primary pair)\n"
    "> `chat USDJPY` — analysis-seeded conversation for a specific pair\n"
    "> `positions` — run Job 2 position check now\n"
    "> `status` — show system status (pairs, cooldown, MT5, pending signals)\n"
    "> `collect` — trigger end-of-day data collection\n"
    "> `pending` — list pending signals awaiting approval\n"
    "> `approve <ID> [%]` — approve a signal; for Trim, optionally specify % to close (default 50%)\n"
    "> `approve <ID> limit <price>` — approve a Long/Short signal as a limit order at the given price\n"
    "> `reject <ID>` — reject a pending signal\n"
    "> `debate positions` — run Hold/Trim/Exit ensemble debate on all open positions\n"
    "> `debate positions GBPUSD` — debate a specific pair's open position(s)\n"
    "> `journal` — show the 10 most recent trade journal entries\n"
    "> `journal report` — performance summary (win rate, pips, P&L)\n"
    "> `journal validate` — force validation of pending journal records against MT5\n"
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

def _do_scan(say: Callable, symbol: str | None = None) -> None:
    if symbol:
        say(f":mag: Starting news scan for *{symbol}* (forced)…")
        from triage.scanner import run_scan_pair
        triggered = run_scan_pair(symbol, force=True)
    else:
        say(f":mag: Starting news scan for all pairs ({_pairs_str()})…")
        from triage.scanner import run_scan
        triggered = run_scan(force=True)
    if triggered:
        say(
            f":bell: Scan complete — {len(triggered)} article(s) triggered full analysis. "
            "Check above for the signal."
        )
    else:
        say(":white_check_mark: Scan complete — no high-score articles found.")


def _do_analyze(say: Callable, symbol: str | None = None) -> None:
    """Run the full analysis pipeline on demand for one pair, bypassing news triage."""
    sym = symbol or config.MT5_SYMBOL
    display = config.PAIRS.get(sym, config.PAIRS[config.MT5_SYMBOL])["display"]
    say(f":brain: Running full market analysis for *{display}* (news + prices + technicals)…")
    try:
        from analysis.context_builder import build_context
        from analysis.full_analysis_prompt import run_full_analysis
        from analysis.signal_formatter import format_signal
        from notifications.notifier import notify

        context = build_context(trigger_item=None, symbol=sym)
        raw_signal = run_full_analysis(context, symbol=sym)
        signal = format_signal(raw_signal)
        signal["_symbol"] = sym

        if signal.get("signal") in ("Long", "Short"):
            from pipeline.signal_store import save_pending_signal
            from agents.job3_executor import compute_order_preview

            preview = compute_order_preview(signal, symbol=sym)
            if preview:
                signal["_order_preview"] = preview

            if config.USE_MULTI_AGENT_DEBATE:
                try:
                    from analysis.ensemble_agent import run_ensemble_debate
                    say(":scales: Running ensemble debate (Bull / Bear / Devil's Advocate)…")
                    run_ensemble_debate(context, signal, symbol=sym)
                    if signal.get("sl") or signal.get("tp"):
                        updated_preview = compute_order_preview(signal, symbol=sym)
                        if updated_preview:
                            signal["_order_preview"] = updated_preview
                except Exception as e:
                    logger.error(f"Ensemble debate failed: {e}", exc_info=True)

            signal_id = save_pending_signal(signal, source="manual")
            signal["_signal_id"] = signal_id
            signal["_source"] = "manual"

        notify(signal, trigger_item=None)
        action = signal.get("signal", "Hold")
        confidence = signal.get("confidence", "")
        say(
            f":white_check_mark: Analysis complete for *{display}* — *{action}* [{confidence}]. "
            "Check above for the full signal."
        )
    except Exception as e:
        logger.error(f"On-demand analysis failed: {e}", exc_info=True)
        say(f":x: Analysis failed: {e}")


def _do_chat_command(say: Callable, thread_ts: str, symbol: str | None = None) -> None:
    """Start an analysis-seeded chat session in the given Slack thread."""
    sym = symbol or config.MT5_SYMBOL
    display = config.PAIRS.get(sym, config.PAIRS[config.MT5_SYMBOL])["display"]
    say(f":brain: Running full market analysis for *{display}* to brief you…")
    try:
        from analysis.context_builder import build_context
        from analysis.full_analysis_prompt import run_full_analysis
        from analysis.signal_formatter import format_signal
        from agents.job4_chat import seed_thread

        context = build_context(trigger_item=None, symbol=sym)
        raw_signal = run_full_analysis(context, symbol=sym)
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
            f"*{display} Market Analysis*\n\n"
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
        seed_thread(thread_ts, briefing, symbol=sym)

        # Save Long/Short signals to the pending store
        if action in ("Long", "Short"):
            from pipeline.signal_store import save_pending_signal
            from agents.job3_executor import compute_order_preview
            preview = compute_order_preview(signal, symbol=sym)
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
    pairs_str = _pairs_str()
    say(f":eyes: Checking open positions for {pairs_str}…")
    from agents.job2_position import run_position_check
    count = run_position_check()
    if count == 0:
        say(f":white_check_mark: No open positions for {pairs_str}.")
    else:
        say(
            f":white_check_mark: Position check complete — {count} position(s) analysed. "
            "Check above for recommendations."
        )


def _do_status(say: Callable) -> None:
    from mt5.connector import is_connected
    from pipeline.signal_store import list_signals
    from triage.cooldown import _lock_file

    # Per-pair cooldown status
    cooldown_parts = []
    for sym in config.ACTIVE_PAIRS:
        lock = _lock_file(sym)
        label = ":white_check_mark: ready"
        if lock.exists():
            try:
                ts = datetime.fromisoformat(lock.read_text(encoding="utf-8").strip())
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                expiry = ts + timedelta(minutes=config.COOLDOWN_MINUTES)
                now = datetime.now(timezone.utc)
                if now < expiry:
                    remaining = int((expiry - now).total_seconds() / 60)
                    label = f":pause_button: {remaining} min"
            except Exception:
                pass
        cooldown_parts.append(f"`{sym}` {label}")
    cooldown_str = "  ".join(cooldown_parts)

    # MT5
    mt5_str = ":large_green_circle: Connected" if is_connected() else ":red_circle: Disconnected"

    # Pending signals
    pending = list_signals(status="pending")
    pending_str = f"{len(pending)} pending" if pending else "none"

    say(
        f"*Forex Multi-Pair Agent Status*\n"
        f">*Active pairs:* {_pairs_str()}\n"
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


def _do_approve(
    say: Callable,
    signal_id: str,
    trim_pct: float | None = None,
    limit_price: float | None = None,
) -> None:
    from agents.job3_executor import approve_and_execute
    if limit_price is not None:
        desc = f" as limit order @ `{limit_price}`"
    elif trim_pct is not None:
        desc = f" (trim {trim_pct}%)"
    else:
        desc = ""
    say(f":hourglass: Approving and executing signal `{signal_id}`{desc}…")
    result = approve_and_execute(signal_id, trim_pct=trim_pct, limit_price=limit_price)
    if result.get("ok"):
        ticket = result.get("ticket", "?")
        price = result.get("price", "?")
        volume = result.get("volume", "?")
        if result.get("order_type") == "limit":
            say(
                f":white_check_mark: Limit order placed for signal `{signal_id}`!\n"
                f">*Ticket:* `{ticket}`\n"
                f">*Limit price:* `{price}` _(order pending fill)_\n"
                f">*Volume:* `{volume}` lots"
            )
        else:
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
        try:
            from analysis.trade_journal import reject_in_journal
            reject_in_journal(signal_id)
        except Exception as e:
            logger.error(f"Journal reject failed: {e}")
        say(f":white_check_mark: Signal `{signal_id}` rejected.")
    else:
        say(f":x: Signal `{signal_id}` not found.")


def _do_journal(say: Callable) -> None:
    """Show the 10 most recent trade journal entries."""
    from analysis.trade_journal import list_recent
    records = list_recent(10)
    if not records:
        say(":open_file_folder: Trade journal is empty — signals will appear here after the next alert.")
        return
    lines = [f"*Trade Journal — last {len(records)} entries*"]
    for r in records:
        sig_id  = r.get("signal_id", "?")
        sym     = r.get("symbol", "?")
        action  = r.get("signal", "?")
        conf    = r.get("confidence", "")
        status  = r.get("status", "?")
        outcome = r.get("outcome", "")
        pnl_p   = r.get("pnl_pips", "")
        pnl_u   = r.get("pnl_usd", "")
        rec_at  = r.get("recorded_at", "")[:16]

        # Build outcome / P&L string
        if outcome == "still_open":
            result_str = f":hourglass: open  {pnl_p:+}p" if pnl_p != "" else ":hourglass: open"
        elif outcome == "TP_hit":
            result_str = f":white_check_mark: TP  `{pnl_p:+}p`" + (f"  `${pnl_u:+}`" if pnl_u else "")
        elif outcome == "SL_hit":
            result_str = f":x: SL  `{pnl_p}p`" + (f"  `${pnl_u}`" if pnl_u else "")
        elif outcome == "manual_close":
            result_str = f":scissors: manual  `{pnl_p:+}p`"
        elif outcome == "hypothetical_tp":
            result_str = f":chart_with_upwards_trend: hyp-TP  `{pnl_p:+}p`"
        elif outcome == "hypothetical_sl":
            result_str = f":chart_with_downwards_trend: hyp-SL  `{pnl_p}p`"
        elif outcome == "hypothetical_open":
            result_str = ":hourglass: hyp-still-open"
        elif outcome == "expired_untracked":
            result_str = ":grey_question: expired"
        else:
            result_str = f":clock3: {status}"

        lines.append(
            f">`{sig_id}` {sym} *{action}* [{conf}]  {rec_at}  {result_str}"
        )

    say("\n".join(lines))


def _do_journal_report(say: Callable) -> None:
    """Show performance summary from the trade journal."""
    from analysis.trade_journal import journal_report
    report = journal_report(last_n=30)
    say(f"```{report}```")


def _do_journal_validate(say: Callable) -> None:
    """Force validate pending journal records against MT5."""
    from mt5.connector import is_connected
    if not is_connected():
        say(":x: MT5 is not connected — cannot validate journal records.")
        return
    say(":hourglass: Validating trade journal against MT5…")
    from analysis.trade_journal import validate_pending
    n = validate_pending()
    say(f":white_check_mark: Journal validation complete — {n} record(s) updated.")


def _do_debate_positions(say: Callable, symbol: str | None = None) -> None:
    """Run the position ensemble debate on all open positions (or one symbol)."""
    from mt5.connector import is_connected
    from mt5.position_reader import get_open_positions

    if not is_connected():
        say(":x: MT5 is not connected — cannot read positions.")
        return

    positions = get_open_positions(symbol=symbol)
    if not positions:
        target = f"*{symbol}*" if symbol else "any active pair"
        say(f":white_check_mark: No open positions for {target}.")
        return

    say(
        f":scales: Running Hold/Trim/Exit ensemble debate on "
        f"{len(positions)} position(s)… _(this takes ~30s)_"
    )

    from analysis.context_builder import build_context
    from analysis.ensemble_agent import run_position_debate

    for pos in positions:
        sym     = pos.get("symbol", config.MT5_SYMBOL)
        display = config.PAIRS.get(sym, config.PAIRS[config.MT5_SYMBOL])["display"]
        ticket  = pos.get("ticket")
        direction = pos.get("type", "buy").upper()
        open_p  = pos.get("open_price", 0)
        curr_p  = pos.get("current_price", 0)
        profit  = pos.get("profit", 0)
        pip     = config.PAIRS.get(sym, config.PAIRS[config.MT5_SYMBOL])["pip"]
        pips_pnl = round(
            (curr_p - open_p) / pip if direction == "BUY" else (open_p - curr_p) / pip,
            1,
        )

        try:
            context = build_context(trigger_item=None, symbol=sym)
            result  = run_position_debate(context, pos, symbol=sym)
        except Exception as e:
            logger.error(f"Position debate failed for #{ticket}: {e}", exc_info=True)
            say(f":x: Debate failed for #{ticket} {display}: {e}")
            continue

        if not result:
            say(f":x: Debate returned no result for #{ticket} {display}.")
            continue

        evals   = result.get("evaluations") or {}
        hold_s  = evals.get("bull_score", "?")
        exit_s  = evals.get("bear_score", "?")
        dev_s   = evals.get("neutral_score", "?")
        win     = evals.get("winning_case", "?")
        unc     = result.get("uncertainty_score", "?")
        unc_rat = result.get("uncertainty_rationale", "")
        dec     = result.get("final_decision", "Hold")
        conf    = result.get("confidence_override") or "—"
        flaws   = evals.get("flaws", "")
        assess  = result.get("position_assessment") or {}
        sl_ass  = assess.get("sl_assessment", "")
        tp_ass  = assess.get("tp_assessment", "")
        sl_adj  = assess.get("suggested_sl_adjustment")
        tp_adj  = assess.get("suggested_tp_adjustment")
        trim_rec = assess.get("trim_recommendation", "")
        trim_pct = assess.get("trim_percentage")

        # Uncertainty emoji
        unc_emoji = (
            ":large_green_circle:" if isinstance(unc, int) and unc <= 39 else
            ":large_yellow_circle:" if isinstance(unc, int) and unc <= 69 else
            ":red_circle:"
        )

        # Decision emoji
        dec_emoji = {
            "Hold": ":white_check_mark:",
            "Trim": ":scissors:",
            "Exit": ":octagonal_sign:",
        }.get(dec, ":scales:")

        pnl_str = f"{pips_pnl:+.1f} pips  (${profit:.2f})"

        # Build the adjustments / recommendations block
        adj_lines = ""
        if sl_adj:
            adj_lines += f"\n>:wrench: Suggested SL: `{sl_adj}`  _{sl_ass}_"
        elif sl_ass:
            adj_lines += f"\n>:mag: SL: {sl_ass}"
        if tp_adj:
            adj_lines += f"\n>:wrench: Suggested TP: `{tp_adj}`  _{tp_ass}_"
        elif tp_ass:
            adj_lines += f"\n>:mag: TP: {tp_ass}"
        if trim_rec:
            adj_lines += f"\n>:scissors: {trim_rec}"

        # Approval shortcut lines
        action_lines = ""
        if dec == "Exit":
            action_lines = (
                f"\n\n_Propose exit:_ ask `chat` to create an Exit signal for #{ticket}, "
                f"then `approve <ID>` to execute."
            )
        elif dec == "Trim" and trim_pct:
            action_lines = (
                f"\n\n_Propose trim:_ ask `chat` to create a Trim signal for #{ticket}, "
                f"then `approve <ID> {trim_pct}` to close {trim_pct}%."
            )

        say(
            f"*{display} Position #{ticket}* — {direction}  {pnl_str}\n\n"
            f"*Ensemble Debate — Hold/Trim/Exit*\n"
            f">:handshake: Hold `{hold_s}` | :octagonal_sign: Exit `{exit_s}` | :scales: Devil `{dev_s}` | Winner: *{win}*\n"
            f">{unc_emoji} *Uncertainty:* `{unc}/100` — {unc_rat}\n"
            + (f">:speech_balloon: _{flaws}_\n" if flaws else "")
            + f"\n{dec_emoji} *Judge verdict: {dec}* [{conf}]"
            + adj_lines
            + action_lines
        )


def _do_introduce(say: Callable) -> None:
    say(
        f"*Hi! I'm your Forex Multi-Pair AI Trading Agent.* :robot_face:\n\n"
        f"*Active pairs:* {_pairs_str()}\n\n"
        "*What I do:*\n"
        "> :newspaper: *Job 1 — News Scanner*: Every 30 minutes I scan financial news "
        "(NewsAPI, AlphaVantage, EODHD), score headlines 1–10 for each pair's relevance, "
        "and run a full Claude Sonnet analysis when something important happens. "
        "Signals are sent here to Slack.\n"
        "> :eyes: *Job 2 — Position Monitor*: Every 30 minutes I check your open MT5 "
        "positions and recommend Hold / Trim / Exit based on current market conditions.\n"
        "> :robot_face: *Job 3 — Trade Executor*: Executes approved signals directly on MT5.\n\n"
        "*How to talk to me:*\n"
        "> Just ask naturally — _\"scan the news\"_, _\"check my positions\"_, "
        "_\"what's the system status?\"_ — or add a pair symbol: _\"scan GBPUSD\"_, "
        "_\"analyze USDJPY\"_.\n\n"
        + _HELP_TEXT
    )


# ── NLP intent classifier ─────────────────────────────────────────────────────

_INTENT_SYSTEM = """\
You are a command parser for a multi-pair forex trading agent Slack bot.
Given a user message, identify the intent and return JSON only.

Possible intents:
- "scan"       — user wants to scan/fetch/update news or market data
- "analyze"    — user wants a full market analysis, trading outlook, or trade idea/opportunity
- "chat"       — user wants to start an interactive chat session seeded with market analysis
- "positions"  — user wants to check open positions or portfolio
- "status"     — user wants system status, health check, or overview
- "collect"    — user wants to collect/save end-of-day data
- "pending"    — user wants to see pending or waiting signals
- "approve"          — user wants to approve a signal (extract signal_id if present)
- "reject"           — user wants to reject a signal (extract signal_id if present)
- "debate_positions" — user wants to debate / review / analyse whether to hold, trim, or exit open positions
- "journal"          — user wants to see the trade journal, performance report, or past signal outcomes
- "introduce"        — user wants to know what the bot does, or is greeting it
- "help"             — user wants a list of commands
- "unknown"          — cannot determine intent

If the message mentions a specific currency pair (e.g. EURUSD, GBPUSD, USDJPY, AUDUSD),
extract it as the "pair" field (uppercase, no slash). Otherwise null.

Return JSON with this exact shape:
{"intent": "<intent>", "signal_id": "<8-char ID or null>", "pair": "<SYMBOL or null>"}
"""


def _classify_intent(text: str) -> tuple[str, str | None, str | None]:
    """
    Use Claude Haiku to classify natural language into a command intent.
    Returns (intent, signal_id_or_None, pair_or_None).
    """
    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        msg = client.messages.create(
            model=config.TRIAGE_MODEL,
            max_tokens=80,
            system=_INTENT_SYSTEM,
            messages=[{"role": "user", "content": text}],
        )
        raw = msg.content[0].text.strip()
        if not raw:
            return "unknown", None, None
        # Strip markdown code fences if present
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
        parsed = json.loads(raw)
        intent = parsed.get("intent", "unknown")
        signal_id = parsed.get("signal_id") or None
        pair = parsed.get("pair") or None
        # Validate pair is a known active pair
        if pair and pair.upper() not in config.ACTIVE_PAIRS:
            pair = None
        elif pair:
            pair = pair.upper()
        return intent, signal_id, pair
    except Exception as e:
        logger.warning(f"Intent classification failed: {e}")
        return "unknown", None, None


# ── pair extraction helper ────────────────────────────────────────────────────

_PAIR_PATTERN = re.compile(
    r'\b(' + '|'.join(re.escape(p) for p in config.ACTIVE_PAIRS) + r')\b',
    re.IGNORECASE,
)


def _extract_pair(text: str) -> str | None:
    """Extract an active pair symbol from text, e.g. 'scan gbpusd' → 'GBPUSD'."""
    m = _PAIR_PATTERN.search(text)
    return m.group(1).upper() if m else None


# ── dispatcher ────────────────────────────────────────────────────────────────

def _dispatch(text: str, say: Callable) -> bool:
    """
    Parse raw message text, strip bot mentions, and route to the right handler.
    Supports optional pair suffix on scan/analyze commands (e.g. "scan GBPUSD").
    Falls back to Claude Haiku NLP classification for natural language.
    Returns True if the command was recognised.
    """
    clean = re.sub(r"<@[A-Z0-9]+>", "", text).strip().lower()
    clean = re.sub(r"\s+", " ", clean)  # collapse multiple spaces/tabs

    # Fast path — keyword + optional pair symbol
    if m := re.fullmatch(r"(scan|update)(?:\s+(\w+))?", clean):
        pair = _extract_pair(m.group(2) or "")
        _run_in_thread(say, _do_scan, pair)
    elif m := re.fullmatch(r"(analyze|analyse|analysis|outlook)(?:\s+(\w+))?", clean):
        pair = _extract_pair(m.group(2) or "")
        _run_in_thread(say, _do_analyze, pair)
    elif re.fullmatch(r"positions?", clean):
        _run_in_thread(say, _do_positions)
    elif clean == "status":
        _run_in_thread(say, _do_status)
    elif clean == "collect":
        _run_in_thread(say, _do_collect)
    elif re.fullmatch(r"pending|signals", clean):
        _run_in_thread(say, _do_pending)
    elif m := re.fullmatch(
        r"approve\s+([A-Za-z0-9]{8})(?:\s+limit\s+([\d.]+)|\s+([\d.]+))?", clean
    ):
        sig_id = m.group(1).upper()
        limit_price = float(m.group(2)) if m.group(2) else None
        trim_pct    = float(m.group(3)) if m.group(3) else None
        _run_in_thread(say, _do_approve, sig_id, trim_pct, limit_price)
    elif m := re.fullmatch(r"reject\s+([A-Za-z0-9]{8})", clean):
        _run_in_thread(say, _do_reject, m.group(1).upper())
    elif m := re.fullmatch(r"debate\s+(?:my\s+)?positions?(?:\s+(\w+))?", clean):
        pair = _extract_pair(m.group(1) or "")
        _run_in_thread(say, _do_debate_positions, pair)
    elif clean in ("journal", "journal list"):
        _run_in_thread(say, _do_journal)
    elif re.fullmatch(r"journal\s+report(?:\s+\d+)?", clean):
        _run_in_thread(say, _do_journal_report)
    elif re.fullmatch(r"journal\s+validate?", clean):
        _run_in_thread(say, _do_journal_validate)
    elif clean == "help":
        say(_HELP_TEXT)
    elif re.fullmatch(r"hi|hello|hey|introduce|who are you", clean):
        _run_in_thread(say, _do_introduce)
    else:
        # NLP fallback — classify with Claude Haiku
        intent, signal_id, pair = _classify_intent(clean)
        if intent == "scan":
            _run_in_thread(say, _do_scan, pair)
        elif intent == "analyze":
            _run_in_thread(say, _do_analyze, pair)
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
        elif intent == "debate_positions":
            _run_in_thread(say, _do_debate_positions, pair)
        elif intent == "journal":
            _run_in_thread(say, _do_journal)
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
                if m := re.fullmatch(r"chat(?:\s+(\w+))?", clean):
                    pair = _extract_pair(m.group(1) or "")
                    dm_thread_ts = event["ts"]
                    tsay = _make_threaded_say(say, dm_thread_ts)
                    _run_in_thread(tsay, _do_chat_command, dm_thread_ts, pair)
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

            # "chat [PAIR]" command at top level → start analysis-seeded chat
            if m := re.fullmatch(r"chat(?:\s+(\w+))?", clean):
                pair = _extract_pair(m.group(1) or "")
                _run_in_thread(tsay, _do_chat_command, thread_ts, pair)
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
