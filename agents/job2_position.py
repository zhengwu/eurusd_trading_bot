"""Job 2 — Position Monitor.

Monitors open EUR/USD positions via MT5 and uses Claude Sonnet to generate
risk management recommendations: Hold / Trim / Exit.

Runs every JOB2_CHECK_INTERVAL_MINUTES during forex market hours.
Recommendations are sent to Slack and saved as pending signals for Job 3.

Usage (standalone):
    conda activate mt5_env
    cd C:\\Users\\zz10c\\Documents\\eurusd_agent
    python -m agents.job2_position

Typically run as a background thread inside agents/job1_opportunity.py.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import anthropic
from dotenv import load_dotenv

load_dotenv()

import config
from mt5.connector import connect, disconnect, is_connected
from mt5.position_reader import get_open_positions, get_account_summary, get_current_tick
from analysis.context_builder import build_context
from notifications.notifier import notify
from utils.logger import get_logger
from utils.date_utils import is_forex_market_open, to_est

logger = get_logger(__name__)

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _client


# ── prompts ───────────────────────────────────────────────────────────────────

_SYSTEM = (
    "You are a professional EUR/USD forex risk manager. "
    "Your job is to protect open positions, not to predict new trades."
)

_PROMPT_TEMPLATE = """\
You are monitoring an open EUR/USD position. Review the position details,
account health, and current market context. Recommend whether to hold, trim,
or exit the position to protect capital.

=== OPEN POSITION ===
{position_details}

=== ACCOUNT STATUS ===
{account_details}

=== CURRENT MARKET SNAPSHOT ===
{market_snapshot}

=== RECENT MARKET CONTEXT ===
{market_context}

Provide your recommendation in the following JSON format:
{{
  "action": "Hold | Trim | Exit",
  "confidence": "High | Medium | Low",
  "rationale": "2-3 sentence explanation referencing specific position and market data",
  "risk_note": "Any immediate risks to the position",
  "suggested_sl": null,
  "suggested_tp": null
}}

Rules:
- Recommend "Exit" only if there is clear evidence the original thesis is broken
- Recommend "Trim" if risk/reward has deteriorated but direction is still valid
- Recommend "Hold" if the position is on track
- suggested_sl / suggested_tp should be float prices or null

Return ONLY valid JSON, no other text."""


# ── formatters ────────────────────────────────────────────────────────────────

def _fmt_position(pos: dict) -> str:
    direction = pos.get("type", "").upper()
    open_price = pos.get("open_price", 0)
    current_price = pos.get("current_price", 0)
    pip = 0.0001
    pips_move = (current_price - open_price) / pip if direction == "BUY" else (open_price - current_price) / pip
    profit_str = f"${pos.get('profit', 0):.2f} ({pips_move:+.1f} pips)"
    open_est = to_est(datetime.fromisoformat(pos["open_time"])).strftime("%Y-%m-%d %H:%M EST") if pos.get("open_time") else "N/A"

    return (
        f"  Ticket       : {pos.get('ticket')}\n"
        f"  Direction    : {direction}\n"
        f"  Volume       : {pos.get('volume')} lots\n"
        f"  Open Price   : {open_price}\n"
        f"  Current Price: {current_price}\n"
        f"  P&L          : {profit_str}\n"
        f"  Stop Loss    : {pos.get('sl') or 'None'}\n"
        f"  Take Profit  : {pos.get('tp') or 'None'}\n"
        f"  Opened       : {open_est}"
    )


def _fmt_account(acct: dict) -> str:
    equity = acct.get("equity")
    balance = acct.get("balance")
    drawdown_pct = None
    if equity is not None and balance and balance > 0:
        drawdown_pct = round((balance - equity) / balance * 100, 2)

    lines = (
        f"  Equity       : ${equity}\n"
        f"  Balance      : ${balance}\n"
        f"  Free Margin  : ${acct.get('free_margin')}\n"
        f"  Margin Level : {acct.get('margin_level')}%"
    )
    if drawdown_pct is not None:
        lines += f"\n  Drawdown     : {drawdown_pct}%"
    return lines


def _fmt_market_snapshot(tick: dict | None) -> str:
    if tick is None:
        return "  (tick unavailable)"
    now_est = to_est(datetime.now(timezone.utc)).strftime("%H:%M EST")
    return (
        f"  Time  : {now_est}\n"
        f"  Bid   : {tick.get('bid')}\n"
        f"  Ask   : {tick.get('ask')}\n"
        f"  Spread: {round(tick.get('spread', 0) / 0.0001, 1)} pips"
    )


# ── analysis ──────────────────────────────────────────────────────────────────

def analyze_position(pos: dict) -> dict[str, Any] | None:
    """Run Claude Sonnet analysis on a single open position."""
    account = get_account_summary()
    tick = get_current_tick()

    # Build a lightweight market context (today's intraday + recent prices only)
    try:
        market_context = build_context(trigger_item=None)
        # Keep context short for position monitor — first 2000 chars is enough
        market_context = market_context[:2000]
    except Exception as e:
        logger.warning(f"Context build failed: {e}")
        market_context = "(market context unavailable)"

    prompt = _PROMPT_TEMPLATE.format(
        position_details=_fmt_position(pos),
        account_details=_fmt_account(account),
        market_snapshot=_fmt_market_snapshot(tick),
        market_context=market_context,
    )

    try:
        message = _get_client().messages.create(
            model=config.ANALYSIS_MODEL,
            max_tokens=512,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
    except Exception as e:
        logger.error(f"Job 2 LLM call failed: {e}")
        return None

    clean = re.sub(r"^```[a-z]*\n?", "", raw)
    clean = re.sub(r"\n?```$", "", clean).strip()

    try:
        result = json.loads(clean)
        if not isinstance(result, dict):
            raise ValueError("Expected JSON object")
        result["ticket"] = pos.get("ticket")
        result["source"] = "job2"
        return result
    except Exception as e:
        logger.error(f"Job 2 parse failed: {e}\nRaw: {raw[:500]}")
        return None


# ── notifier integration ──────────────────────────────────────────────────────

def _build_signal_from_recommendation(pos: dict, rec: dict) -> dict[str, Any]:
    """Convert a Job 2 recommendation into a signal dict for the notifier."""
    action = rec.get("action", "Hold")
    return {
        "signal": action,
        "confidence": rec.get("confidence", "Low"),
        "time_horizon": "Intraday",
        "rationale": rec.get("rationale", ""),
        "key_levels": {
            "support": rec.get("suggested_tp") if pos.get("type") == "sell" else rec.get("suggested_sl"),
            "resistance": rec.get("suggested_sl") if pos.get("type") == "sell" else rec.get("suggested_tp"),
        },
        "invalidation": "",
        "risk_note": rec.get("risk_note", ""),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "_ticket": pos.get("ticket"),
        "_source": "job2",
    }


def _build_trigger_from_position(pos: dict, rec: dict) -> dict[str, Any]:
    """Build a trigger_item dict for the notifier header."""
    direction = pos.get("type", "").upper()
    profit = pos.get("profit", 0)
    profit_str = f"+${profit:.2f}" if profit >= 0 else f"-${abs(profit):.2f}"
    action = rec.get("action", "Hold")
    action_emoji = {"Exit": "🚨", "Trim": "✂️", "Hold": "✅"}.get(action, "")
    return {
        "headline": (
            f"{action_emoji} Position Monitor — #{pos.get('ticket')} "
            f"{direction} {pos.get('volume')}L @ {pos.get('open_price')} | P&L: {profit_str}"
        ),
        "score": 8 if action == "Exit" else (6 if action == "Trim" else 4),
        "tag": "position_monitor",
    }


# ── main scan cycle ───────────────────────────────────────────────────────────

def run_position_check() -> int:
    """
    Check all open EURUSD positions and generate recommendations.
    Returns number of positions checked.
    """
    if not is_connected():
        logger.warning("MT5 not connected — skipping position check")
        return 0

    positions = get_open_positions(symbol=config.MT5_SYMBOL)
    if not positions:
        logger.info("No open EURUSD positions")
        return 0

    logger.info(f"=== Job 2 checking {len(positions)} position(s) ===")

    for pos in positions:
        ticket = pos.get("ticket")
        try:
            rec = analyze_position(pos)
            if rec is None:
                continue

            action = rec.get("action", "Hold")
            confidence = rec.get("confidence", "Low")
            logger.info(f"Position #{ticket}: {action} [{confidence}]")

            signal = _build_signal_from_recommendation(pos, rec)
            trigger = _build_trigger_from_position(pos, rec)

            # Save as pending signal for Job 3 (Exit and Trim require user approval)
            # Hold with High confidence also alerts but does not require execution
            if action in ("Exit", "Trim"):
                # Import here to avoid circular imports at module load time
                from pipeline.signal_store import save_pending_signal
                signal_id = save_pending_signal(signal, source="job2")
                signal["_signal_id"] = signal_id
                logger.info(f"Pending signal saved: {signal_id} — awaiting approval")

            notify(signal, trigger_item=trigger)

        except Exception as e:
            logger.error(f"Position check failed for #{ticket}: {e}", exc_info=True)

    return len(positions)


# ── loop ──────────────────────────────────────────────────────────────────────

def run_job2_loop() -> None:
    """Run Job 2 position monitor loop continuously."""
    logger.info(f"Job 2 loop started — interval: {config.JOB2_CHECK_INTERVAL_MINUTES} min")
    while True:
        try:
            if is_forex_market_open(
                config.MARKET_HOURS_START,
                config.MARKET_HOURS_END,
                config.MARKET_CLOSE_HOUR_EST,
                config.MARKET_OPEN_HOUR_EST,
            ):
                run_position_check()
            else:
                logger.debug("Market closed — skipping position check")
        except Exception as e:
            logger.error(f"Job 2 cycle error: {e}", exc_info=True)
        time.sleep(config.JOB2_CHECK_INTERVAL_MINUTES * 60)


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("=" * 60)
    logger.info("EUR/USD Position Monitor — Job 2")
    logger.info(f"  Analysis model  : {config.ANALYSIS_MODEL}")
    logger.info(f"  Check interval  : {config.JOB2_CHECK_INTERVAL_MINUTES} min")
    logger.info(f"  Symbol          : {config.MT5_SYMBOL}")
    logger.info("=" * 60)

    if not connect():
        logger.error("Cannot connect to MT5 — is the terminal running?")
        return

    try:
        run_job2_loop()
    except KeyboardInterrupt:
        logger.info("Job 2 stopped by user")
    finally:
        disconnect()


if __name__ == "__main__":
    main()
