"""Job 2 — Dynamic Position Manager.

Monitors open positions via MT5 and applies a two-layer decision process:

  Layer 1 — Phase Engine (hard rules, no LLM):
    Determines the current phase of each trade from objective data:
      Phase 0 EARLY     — trade not yet at breakeven threshold, just monitor
      Phase 1 BREAKEVEN — trade reached JOB2_BREAKEVEN_R; move SL to entry + buffer
      Phase 2 PROFIT    — trade reached JOB2_PARTIAL_TP_R; suggest trim
      Phase 3 TRAIL     — trade reached JOB2_TRAIL_R; trail SL at ATR distance
      Phase 4 OVERDUE   — trade open longer than expected horizon; tighten/exit
    Auto-executes protective actions (breakeven SL, trail update) without approval.

  Layer 2 — LLM Judgment (Claude Sonnet):
    Given the phase context, enriched position data, and market indicators,
    the LLM answers phase-specific questions:
      Phase 0/1 — Is the thesis still valid? Any early exit signals?
      Phase 2   — Should we trim 33%, 50%, or hold for a bigger move?
      Phase 3   — Is momentum still supporting the trend, or should we tighten faster?
      Phase 4   — Is there any reason to hold past the expected horizon, or exit now?

Operational safeguards (checked before any SL/TP modification):
  - Spread must be within JOB2_MAX_SPREAD_MULT × normal_spread_pips
  - No high-impact news event within JOB2_NEWS_BLACKOUT_MIN minutes
  - Minimum lot check before Trim: position volume must be > 2 × JOB3_MIN_LOT

Signal routing:
  Auto-execute (no approval):  breakeven SL move, trail SL update
  Approval required:           Trim, Exit, suggested new TP
  Notify only:                 Hold recommendations

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
from datetime import datetime, timezone, timedelta
from typing import Any

import anthropic
from dotenv import load_dotenv

load_dotenv()

import config
from mt5.connector import connect, disconnect, is_connected
from mt5.position_reader import get_open_positions, get_account_summary, get_current_tick
from mt5.order_manager import modify_sl_tp
from mt5.portfolio_risk import get_portfolio_summary
from notifications.notifier import notify
from utils.logger import get_logger
from utils.date_utils import is_forex_market_open, to_est

logger = get_logger(__name__)

_client: anthropic.Anthropic | None = None

# ── phase watcher shared state ────────────────────────────────────────────────
# Keyed by MT5 ticket (int). Written only by the watcher loop.
# _sl_cache stores original_sl_pips at first sight — never updated after that,
# so R-multiples are always relative to the original entry risk even after SL moves.

_phase_state: dict[int, str]   = {}   # ticket → last known phase
_sl_cache: dict[int, float]    = {}   # ticket → original_sl_pips (set once, then frozen)
_last_watcher_trigger: dict[int, float] = {}  # ticket → timestamp of last watcher analysis

# Expected horizon in hours — maps time_horizon strings from signals
_HORIZON_HOURS: dict[str, float] = {
    "scalp":    2.0,
    "intraday": 8.0,
    "today":    8.0,
    "1-day":    24.0,
    "1 day":    24.0,
    "2-day":    48.0,
    "2 day":    48.0,
    "swing":    96.0,
    "week":     168.0,
    "medium":   336.0,
}
_DEFAULT_HORIZON_HOURS = 24.0

# Phase labels
PHASE_LOSS_CRITICAL = "loss_critical"  # R < JOB2_LOSS_DEEP_R — near SL, exit-biased
PHASE_LOSS_DEEP     = "loss_deep"      # JOB2_LOSS_DEEP_R ≤ R < JOB2_LOSS_MODERATE_R — partial close eligible
PHASE_LOSS_MODERATE = "loss_moderate"  # JOB2_LOSS_MODERATE_R ≤ R < JOB2_LOSS_MILD_R — alert, thesis review
PHASE_LOSS_MILD     = "loss_mild"      # JOB2_LOSS_MILD_R ≤ R < 0 — normal noise, monitor
PHASE_EARLY         = "early"          # 0 ≤ R < JOB2_BREAKEVEN_R — not yet breakeven
PHASE_BREAKEVEN = "breakeven"   # JOB2_BREAKEVEN_R ≤ R < JOB2_PARTIAL_TP_R
PHASE_PROFIT    = "profit"      # JOB2_PARTIAL_TP_R ≤ R < JOB2_TRAIL_R
PHASE_TRAIL     = "trail"       # R ≥ JOB2_TRAIL_R
PHASE_OVERDUE   = "overdue"     # hours_open > JOB2_OVERDUE_MULT × expected_horizon
PHASE_UNKNOWN   = "unknown"     # no original SL data — can't compute R


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _client


# ── ticket → original signal lookup ──────────────────────────────────────────

def _get_original_signal(ticket: int | None) -> dict | None:
    """
    Look up the original signal that opened a position via ticket_map.json
    then the trade journal.  Returns the journal record, or None.
    """
    if ticket is None:
        return None
    try:
        from agents.job3_executor import get_signal_id_for_ticket
        signal_id = get_signal_id_for_ticket(ticket)
        if not signal_id:
            return None
        from analysis.trade_journal import _load as _journal_load
        for rec in _journal_load():
            if rec.get("signal_id") == signal_id:
                return rec
    except Exception as e:
        logger.debug(f"Original signal lookup failed for #{ticket}: {e}")
    return None


# ── phase engine ──────────────────────────────────────────────────────────────

def _expected_horizon_hours(original: dict | None) -> float:
    """Map time_horizon string from original signal to expected hours."""
    if not original:
        return _DEFAULT_HORIZON_HOURS
    horizon_str = (original.get("time_horizon") or "").lower()
    for key, hours in _HORIZON_HOURS.items():
        if key in horizon_str:
            return hours
    return _DEFAULT_HORIZON_HOURS


def _compute_phase(pos: dict, original: dict | None) -> dict:
    """
    Compute the current trade phase and supporting metrics from hard rules.

    Returns a dict with:
      phase           — one of the PHASE_* constants
      current_r       — pips-in-profit / original_sl_pips (None if unknown)
      pips_profit     — raw pip move in trade direction
      original_sl_pips — SL distance at time of entry (from journal)
      sl_at_breakeven — True if SL is already at or beyond entry
      hours_in_trade  — time since position opened
      expected_hours  — expected trade horizon in hours
      is_overdue      — True if hours_in_trade > OVERDUE_MULT * expected_hours
      net_pnl         — profit + swap (actual net P&L including carry cost)
    """
    symbol    = pos.get("symbol", config.MT5_SYMBOL)
    pip       = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["pip"]
    direction = pos.get("type", "buy")

    open_price    = pos.get("open_price", 0.0)
    current_price = pos.get("current_price", 0.0)
    sl            = pos.get("sl") or 0.0
    profit        = pos.get("profit", 0.0)
    swap          = pos.get("swap", 0.0)
    net_pnl       = round(profit + swap, 2)

    # Pip move in trade direction
    if direction == "buy":
        pips_profit = (current_price - open_price) / pip
    else:
        pips_profit = (open_price - current_price) / pip
    pips_profit = round(pips_profit, 1)

    # Time in trade
    hours_in_trade = 0.0
    if pos.get("open_time"):
        try:
            opened = datetime.fromisoformat(pos["open_time"])
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            hours_in_trade = (datetime.now(timezone.utc) - opened).total_seconds() / 3600
        except Exception:
            pass

    expected_hours = _expected_horizon_hours(original)
    is_overdue     = hours_in_trade > config.JOB2_OVERDUE_MULT * expected_hours

    # Original SL distance — needed to compute R-multiple
    original_sl_pips: float | None = None

    # 1. Journal lookup (most accurate — actual SL recorded at execution time)
    if original:
        try:
            actual_sl    = float(original.get("actual_sl") or 0)
            actual_entry = float(original.get("actual_entry") or 0)
            if actual_sl and actual_entry:
                if direction == "buy":
                    original_sl_pips = max(0.0, (actual_entry - actual_sl) / pip)
                else:
                    original_sl_pips = max(0.0, (actual_sl - actual_entry) / pip)
        except Exception:
            pass

    # 2. In-memory watcher cache (frozen at first sight — survives SL modifications
    #    such as breakeven or trail moves that would corrupt the current-SL fallback)
    ticket = pos.get("ticket")
    if not original_sl_pips and ticket and ticket in _sl_cache:
        original_sl_pips = _sl_cache[ticket]

    # 3. Current SL fallback — only reliable before any SL modifications
    if not original_sl_pips and sl:
        if direction == "buy":
            original_sl_pips = max(0.0, (open_price - sl) / pip)
        else:
            original_sl_pips = max(0.0, (sl - open_price) / pip)

    # Compute R-multiple
    current_r: float | None = None
    if original_sl_pips and original_sl_pips > 0:
        current_r = round(pips_profit / original_sl_pips, 2)

    # SL at or beyond breakeven?
    if direction == "buy":
        sl_at_breakeven = bool(sl and sl >= open_price)
    else:
        sl_at_breakeven = bool(sl and sl <= open_price)

    # Determine phase
    if is_overdue:
        phase = PHASE_OVERDUE
    elif current_r is None:
        phase = PHASE_UNKNOWN
    elif current_r >= config.JOB2_TRAIL_R:
        phase = PHASE_TRAIL
    elif current_r >= config.JOB2_PARTIAL_TP_R:
        phase = PHASE_PROFIT
    elif current_r >= config.JOB2_BREAKEVEN_R:
        phase = PHASE_BREAKEVEN
    elif current_r >= 0:
        phase = PHASE_EARLY
    elif current_r >= config.JOB2_LOSS_MILD_R:
        phase = PHASE_LOSS_MILD
    elif current_r >= config.JOB2_LOSS_MODERATE_R:
        phase = PHASE_LOSS_MODERATE
    elif current_r >= config.JOB2_LOSS_DEEP_R:
        phase = PHASE_LOSS_DEEP
    else:
        phase = PHASE_LOSS_CRITICAL

    return {
        "phase":             phase,
        "current_r":         current_r,
        "pips_profit":       pips_profit,
        "original_sl_pips":  original_sl_pips,
        "sl_at_breakeven":   sl_at_breakeven,
        "hours_in_trade":    round(hours_in_trade, 1),
        "expected_hours":    expected_hours,
        "is_overdue":        is_overdue,
        "net_pnl":           net_pnl,
    }


# ── operational safeguards ────────────────────────────────────────────────────

def _spread_ok(symbol: str) -> tuple[bool, float, float]:
    """
    Return (ok, current_spread_pips, normal_spread_pips).
    ok=False means spread is too wide for safe SL/TP modification.
    """
    normal = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL]).get(
        "normal_spread_pips", 1.5
    )
    pip    = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["pip"]
    tick   = get_current_tick(symbol=symbol)
    if tick is None:
        return True, 0.0, normal   # can't check — allow through
    current_spread_pips = round(tick.get("spread", 0.0) / pip, 1)
    ok = current_spread_pips <= normal * config.JOB2_MAX_SPREAD_MULT
    return ok, current_spread_pips, normal


def _in_news_blackout(symbol: str) -> tuple[bool, str]:
    """
    Return (in_blackout, event_name).
    in_blackout=True means a high-impact event is imminent — skip modifications.
    """
    try:
        from pipeline.pre_event_agent import get_upcoming_high_impact
        events = get_upcoming_high_impact(
            symbol,
            lookahead_min_low=0,
            lookahead_min_high=config.JOB2_NEWS_BLACKOUT_MIN,
        )
        if events:
            return True, events[0].get("event", "upcoming event")
    except Exception as e:
        logger.debug(f"News blackout check failed: {e}")
    return False, ""


def _can_trim(pos: dict) -> bool:
    """Return True if the position volume allows a partial close."""
    return pos.get("volume", 0.0) > config.JOB3_MIN_LOT * 2


# ── auto-execute protective actions ──────────────────────────────────────────

def _auto_breakeven(pos: dict, phase_info: dict) -> dict | None:
    """
    Move SL to entry + buffer if config.JOB2_AUTO_BREAKEVEN is enabled and
    the SL is not already at/above breakeven.

    Returns the modify_sl_tp result dict, or None if skipped.
    """
    if not config.JOB2_AUTO_BREAKEVEN:
        return None
    if phase_info["sl_at_breakeven"]:
        logger.debug(f"#{pos.get('ticket')} SL already at breakeven — skipping")
        return None

    symbol    = pos.get("symbol", config.MT5_SYMBOL)
    pip       = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["pip"]
    direction = pos.get("type", "buy")
    entry     = pos.get("open_price", 0.0)
    current_tp = pos.get("tp") or None
    buffer    = config.JOB2_BREAKEVEN_BUFFER_PIPS * pip

    new_sl = round(
        entry + buffer if direction == "buy" else entry - buffer,
        config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["price_decimals"],
    )

    sign = "+" if direction == "buy" else "-"
    logger.info(
        f"Auto-breakeven #{pos.get('ticket')}: SL → {new_sl} "
        f"(entry {entry} {sign} {config.JOB2_BREAKEVEN_BUFFER_PIPS}p buffer)"
    )
    return modify_sl_tp(pos["ticket"], sl=new_sl, tp=current_tp)


def _auto_trail(pos: dict, indicators: dict) -> dict | None:
    """
    Trail SL at JOB2_TRAIL_ATR_MULT × ATR-M15 behind current price.
    Only advances the SL — never moves it backwards.

    Returns the modify_sl_tp result dict, or None if skipped.
    """
    if not config.JOB2_AUTO_TRAIL:
        return None

    atr_m15 = indicators.get("atr_m15")
    if not atr_m15:
        logger.debug("No M15 ATR available — skipping trail")
        return None

    symbol        = pos.get("symbol", config.MT5_SYMBOL)
    decimals      = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["price_decimals"]
    direction     = pos.get("type", "buy")
    current_price = pos.get("current_price", 0.0)
    current_sl    = pos.get("sl") or 0.0
    current_tp    = pos.get("tp") or None
    trail_dist    = atr_m15 * config.JOB2_TRAIL_ATR_MULT

    if direction == "buy":
        new_sl = round(current_price - trail_dist, decimals)
        if current_sl and new_sl <= current_sl:
            logger.debug(f"#{pos.get('ticket')} trail would move SL backwards — skipping")
            return None
    else:
        new_sl = round(current_price + trail_dist, decimals)
        if current_sl and new_sl >= current_sl:
            logger.debug(f"#{pos.get('ticket')} trail would move SL backwards — skipping")
            return None

    logger.info(
        f"Auto-trail #{pos.get('ticket')}: SL {current_sl} → {new_sl} "
        f"(price {current_price}, ATR×{config.JOB2_TRAIL_ATR_MULT}={trail_dist:.5f})"
    )
    return modify_sl_tp(pos["ticket"], sl=new_sl, tp=current_tp)


# ── formatters ────────────────────────────────────────────────────────────────

def _fmt_position(pos: dict, phase_info: dict) -> str:
    symbol        = pos.get("symbol", config.MT5_SYMBOL)
    pip           = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["pip"]
    direction     = pos.get("type", "").upper()
    open_price    = pos.get("open_price", 0)
    current_price = pos.get("current_price", 0)
    sl            = pos.get("sl") or "None"
    tp            = pos.get("tp") or "None"
    open_est      = ""
    if pos.get("open_time"):
        try:
            open_est = to_est(
                datetime.fromisoformat(pos["open_time"])
            ).strftime("%Y-%m-%d %H:%M EST")
        except Exception:
            pass

    cr = phase_info["current_r"]
    if cr is None:
        r_str = "unknown"
    elif cr < 0:
        pct_to_sl = round(abs(cr) * 100)
        r_str = f"{cr:.2f}R  [{pct_to_sl}% of the way to SL]"
    else:
        r_str = f"{cr:.2f}R"
    be_str = " [SL at breakeven]" if phase_info["sl_at_breakeven"] else ""

    return (
        f"  Ticket         : {pos.get('ticket')}\n"
        f"  Symbol         : {symbol}\n"
        f"  Direction      : {direction}\n"
        f"  Volume         : {pos.get('volume')} lots\n"
        f"  Open Price     : {open_price}\n"
        f"  Current Price  : {current_price}\n"
        f"  Pips in trade  : {phase_info['pips_profit']:+.1f}p\n"
        f"  R-multiple     : {r_str}{be_str}\n"
        f"  Net P&L        : ${phase_info['net_pnl']:.2f} (incl. swap ${pos.get('swap', 0):.2f})\n"
        f"  Stop Loss      : {sl}\n"
        f"  Take Profit    : {tp}\n"
        f"  Opened         : {open_est}\n"
        f"  Time in trade  : {phase_info['hours_in_trade']:.1f}h "
        f"(expected horizon: {phase_info['expected_hours']:.0f}h)"
        + (f"  *** OVERDUE ***" if phase_info["is_overdue"] else "")
    )


def _fmt_account(acct: dict) -> str:
    equity   = acct.get("equity")
    balance  = acct.get("balance")
    drawdown = None
    if equity is not None and balance and balance > 0:
        drawdown = round((balance - equity) / balance * 100, 2)
    lines = (
        f"  Equity       : ${equity}\n"
        f"  Balance      : ${balance}\n"
        f"  Free Margin  : ${acct.get('free_margin')}\n"
        f"  Margin Level : {acct.get('margin_level')}%"
    )
    if drawdown is not None:
        lines += f"\n  Drawdown     : {drawdown}%"
    return lines


def _fmt_market_snapshot(tick: dict | None, symbol: str) -> str:
    if tick is None:
        return "  (tick unavailable)"
    pip     = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["pip"]
    now_est = to_est(datetime.now(timezone.utc)).strftime("%H:%M EST")
    return (
        f"  Time  : {now_est}\n"
        f"  Bid   : {tick.get('bid')}\n"
        f"  Ask   : {tick.get('ask')}\n"
        f"  Spread: {round(tick.get('spread', 0) / pip, 1)} pips"
    )


def _fmt_indicators(indicators: dict) -> str:
    if not indicators:
        return "  (indicators unavailable)"
    lines = []
    if indicators.get("atr_m15") is not None:
        pair_cfg = {}  # generic
        lines.append(f"  ATR-M15 : {indicators['atr_m15']:.5f}  (intraday noise / trail distance)")
    if indicators.get("rsi_m15") is not None:
        rsi = indicators["rsi_m15"]
        lbl = "Overbought ⚠" if rsi >= 70 else "Oversold ⚠" if rsi <= 30 else "Neutral"
        lines.append(f"  RSI-M15 : {rsi}  — {lbl}")
    if indicators.get("m15_momentum"):
        lines.append(f"  M15 bias: {indicators['m15_momentum']}")
    if indicators.get("rsi_d1") is not None:
        rsi = indicators["rsi_d1"]
        lbl = "Overbought ⚠" if rsi >= 70 else "Oversold ⚠" if rsi <= 30 else "Neutral"
        lines.append(f"  RSI-D1  : {rsi}  — {lbl}")
    if indicators.get("macd_hist") is not None:
        hist = indicators["macd_hist"]
        lbl  = "bullish momentum" if hist > 0 else "bearish momentum"
        lines.append(f"  MACD    : hist={hist:+.6f}  ({lbl})")
    return "\n".join(lines) if lines else "  (no indicator data)"


def _fmt_portfolio(portfolio: dict, pos_ticket: int) -> str:
    """Format portfolio exposure context, excluding the current position."""
    others = [p for p in portfolio.get("positions", []) if p.get("ticket") != pos_ticket]
    if not others:
        return "  No other open positions."
    lines = []
    for p in others:
        lines.append(
            f"  {p['symbol']} {p['direction'].upper()} "
            f"{p['lot']}L  risk={p['risk_pct']:.1f}%  bucket={p['bucket']}"
        )
    lines.append(
        f"  Total open risk (excl. this position): "
        f"{portfolio.get('total_risk_pct', 0):.1f}%"
    )
    return "\n".join(lines)


def _fmt_original_signal(original: dict | None) -> str:
    if not original:
        return "  (original signal not found — position may have been opened manually)"
    return (
        f"  Signal      : {original.get('signal')} [{original.get('confidence')}]\n"
        f"  Time horizon: {original.get('time_horizon')}\n"
        f"  Rationale   : {original.get('rationale', '')[:300]}\n"
        f"  Invalidation: {original.get('invalidation', '')}\n"
        f"  Risk note   : {original.get('risk_note', '')}"
    )


# ── theory invalidation check ────────────────────────────────────────────────

def _build_invalidation_block(original: dict | None, phase_info: dict) -> str:
    """
    Build the cross-cutting theory invalidation section for the LLM prompt.
    Fires at every phase so profit phases also catch thesis reversals.
    """
    phase     = phase_info["phase"]
    current_r = phase_info["current_r"]
    r_str     = f"{current_r:.2f}R" if current_r is not None else "unknown R"

    if not original:
        return (
            "No original thesis available (position may have been opened manually or thesis not recorded).\n"
            "Set theory_invalidated=true ONLY if price action alone is clearly catastrophic "
            "(e.g., price has blown through where the stop should be, or a major macro reversal "
            "is underway that would invalidate any thesis in this direction).\n"
            "Default to theory_invalidated=false when uncertain about a manual position."
        )

    inv_condition = original.get("invalidation") or "(not specified)"
    rationale_snippet = (original.get("rationale") or "")[:200]

    return (
        f"Original invalidation condition: {inv_condition}\n"
        f"Original thesis summary: {rationale_snippet}\n"
        f"Current position: {phase.upper()} at {r_str}\n\n"
        "Thesis is INVALIDATED if ANY of these apply:\n"
        "  1. Price has convincingly closed beyond the stated invalidation level\n"
        "  2. A news/macro event has directly and definitively reversed the original catalyst\n"
        "     (e.g., original thesis was 'ECB hawkish hold' → ECB just cut rates unexpectedly)\n"
        "  3. The structural setup has been negated (e.g., cited support/resistance fully broken)\n\n"
        "Thesis is NOT invalidated by:\n"
        "  - Normal pullbacks or noise within ATR range\n"
        "  - Temporary adverse moves that haven't breached key levels\n"
        "  - Broad uncertainty without a specific thesis reversal\n\n"
        "Set theory_invalidated=true only when you are confident the thesis is definitively broken.\n"
        "If true, your action MUST be Exit — this overrides phase-based logic."
    )


# ── targeted news/price helpers for Job 2 prompts ────────────────────────────

def _build_recent_news_brief(symbol: str, n: int = 6) -> str:
    """Last N intraday headlines for this pair (today). Compact format."""
    try:
        from triage.intraday_logger import read_today_intraday
        from analysis.context_builder import _filter_intraday_by_symbol
        records = _filter_intraday_by_symbol(read_today_intraday(), symbol)
        if not records:
            return "  (no headlines today)"
        recent = records[-n:]
        lines = []
        for r in recent:
            score    = r.get("score") or r.get("triage_score") or "?"
            headline = r.get("headline") or r.get("title", "")
            time_str = r.get("time", "")
            lines.append(f"  [{time_str}] [{score}] {headline}")
        return "\n".join(lines)
    except Exception:
        return "  (unavailable)"


def _build_session_price_brief(symbol: str) -> str:
    """Today's session prices: symbol + DXY + US10Y. Single-line compact format."""
    try:
        import json as _json
        from utils.date_utils import today_str_utc
        prices_path = config.DATA_DIR / today_str_utc() / "prices.json"
        prices      = _json.loads(prices_path.read_text(encoding="utf-8"))

        def _fmt(asset: str) -> str:
            d    = prices.get(asset) or {}
            c    = d.get("close")
            pct  = d.get("pct_change")
            if c is None:
                return f"{asset}: N/A"
            pct_str = f" ({pct:+.2f}%)" if pct is not None else ""
            return f"{asset}: {c}{pct_str}"

        parts = [_fmt(symbol), _fmt("DXY"), _fmt("US10Y")]
        return "  " + "  |  ".join(parts)
    except Exception:
        return "  (unavailable)"


def _build_job2_news_context(symbol: str) -> str:
    """
    Targeted recent-events context for Job 2 full Sonnet calls.
    Replaces the generic build_context()[:2000] truncation with:
      - Last 8 scored headlines for this pair (today)
      - Released economic events today (actual vs forecast / surprise)
      - Session prices: symbol + DXY + US10Y
    """
    import json as _json
    from utils.date_utils import today_str_utc

    today = today_str_utc()
    sections: list[str] = []

    # ── recent headlines ──────────────────────────────────────────────────────
    try:
        from triage.intraday_logger import read_today_intraday
        from analysis.context_builder import _filter_intraday_by_symbol
        records = _filter_intraday_by_symbol(read_today_intraday(), symbol)
        recent  = records[-8:] if len(records) > 8 else records
        if recent:
            lines = ["Recent headlines (today, this pair):"]
            for r in recent:
                score    = r.get("score") or r.get("triage_score") or "?"
                headline = r.get("headline") or r.get("title", "")
                time_str = r.get("time", "")
                lines.append(f"  [{time_str}] [{score}] {headline}")
            sections.append("\n".join(lines))
        else:
            sections.append("Recent headlines: (none today)")
    except Exception as e:
        sections.append(f"Recent headlines: (unavailable: {e})")

    # ── released events today ─────────────────────────────────────────────────
    try:
        events_path = config.DATA_DIR / today / "events.json"
        events      = _json.loads(events_path.read_text(encoding="utf-8"))
        released    = [
            e for e in events
            if e.get("release_date") == today
            and e.get("actual") is not None
            and e.get("source") == "ForexFactory"
        ]
        if released:
            lines = ["Released events today:"]
            for e in sorted(released, key=lambda x: x.get("time", "")):
                actual   = e.get("actual")
                forecast = e.get("forecast")
                surprise = e.get("surprise", "")
                line     = f"  {e.get('time','?')} | {e.get('event','')} ({e.get('currency','')}): actual={actual}"
                if forecast is not None:
                    line += f"  forecast={forecast}"
                if surprise:
                    line += f"  [{surprise.upper()}]"
                lines.append(line)
            sections.append("\n".join(lines))
        else:
            sections.append("Released events today: (none yet)")
    except Exception:
        sections.append("Released events today: (unavailable)")

    # ── session prices ────────────────────────────────────────────────────────
    try:
        prices_path = config.DATA_DIR / today / "prices.json"
        prices      = _json.loads(prices_path.read_text(encoding="utf-8"))

        def _fmt(asset: str) -> str:
            d   = prices.get(asset) or {}
            c   = d.get("close")
            pct = d.get("pct_change")
            if c is None:
                return f"{asset}: N/A"
            pct_str = f" ({pct:+.2f}%)" if pct is not None else ""
            return f"{asset}: {c}{pct_str}"

        sections.append(
            "Session prices: "
            + "  |  ".join([_fmt(symbol), _fmt("DXY"), _fmt("US10Y"), _fmt("Gold"), _fmt("VIX")])
        )
    except Exception:
        sections.append("Session prices: (unavailable)")

    return "\n\n".join(sections)


# ── routine prompt (cheap model) ─────────────────────────────────────────────

def _build_routine_prompt(
    pos: dict,
    phase_info: dict,
    indicators: dict,
    original: dict | None,
) -> str:
    """
    Routine 30-min prompt for cheap model (Azure GPT / Haiku fallback).
    Includes recent news + session price so the model isn't flying blind.
    Includes structured assessment approach to improve output quality.
    Estimated ~600 tokens vs ~2000+ for the full Sonnet prompt.
    """
    symbol  = pos.get("symbol", config.MT5_SYMBOL)
    display = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["display"]
    phase   = phase_info["phase"]
    cr      = phase_info.get("current_r")
    r_str   = f"{cr:.2f}R" if cr is not None else "unknown R"

    if phase in (PHASE_LOSS_CRITICAL, PHASE_LOSS_DEEP):
        phase_note = f"Significant loss at {r_str}. Is the original thesis still valid or should we exit?"
        allowed    = '"Hold" or "Exit" only.'
    elif phase in (PHASE_LOSS_MODERATE, PHASE_LOSS_MILD):
        phase_note = f"In loss at {r_str}. Review thesis and momentum."
        allowed    = '"Hold" or "Exit" only.'
    elif phase in (PHASE_TRAIL, PHASE_PROFIT):
        phase_note = f"In profit at {r_str}. Assess momentum and partial-profit opportunity."
        allowed    = '"Hold", "Exit"' + (', or "Trim".' if _can_trim(pos) else '.')
    else:
        phase_note = f"Phase: {phase.upper()} at {r_str}."
        allowed    = '"Hold" or "Exit".'

    news_section  = _build_recent_news_brief(symbol)
    price_section = _build_session_price_brief(symbol)

    return f"""\
Routine {display} position check — indicators + news based assessment.

=== POSITION ===
{_fmt_position(pos, phase_info)}

=== TECHNICAL INDICATORS ===
{_fmt_indicators(indicators)}

=== SESSION PRICES ===
{price_section}

=== RECENT NEWS (today, {display}) ===
{news_section}

=== ORIGINAL TRADE THESIS ===
{_fmt_original_signal(original)}

=== PHASE ===
{phase.upper()} at {r_str} — {phase_note}

=== ASSESSMENT APPROACH ===
Evaluate these three questions to form your decision:
1. Indicator alignment: Do RSI-M15, MACD histogram, and M15 momentum support or oppose the trade direction?
2. News impact: Does any recent headline materially reverse the original trade catalyst?
3. Phase fit: Is the R-multiple and price action consistent with holding through to the original target?
Capture your conclusions in "reasoning_summary" inside the JSON.

Return ONLY valid JSON:
{{
  "reasoning_summary": "3 short sentences covering: indicator verdict, news impact, phase assessment",
  "action": "Hold | Exit | Trim",
  "confidence": "High | Medium | Low",
  "rationale": "1-2 sentences referencing specific numbers (RSI, pips, news headline)",
  "suggested_trim_pct": null
}}

Rules:
- Allowed actions: {allowed}
- No SL/TP modifications in routine checks — those require full analysis
- If action=Exit AND confidence=High: triggers full Sonnet re-analysis automatically
- Return ONLY valid JSON, no other text."""


# ── phase-specific prompts ────────────────────────────────────────────────────

def _build_prompt(
    pos: dict,
    phase_info: dict,
    account: dict,
    tick: dict | None,
    indicators: dict,
    original: dict | None,
    portfolio: dict,
    spread_ok: bool,
    in_blackout: bool,
    blackout_event: str,
    market_context: str,
) -> str:
    symbol  = pos.get("symbol", config.MT5_SYMBOL)
    display = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["display"]
    phase   = phase_info["phase"]

    safeguard_note = ""
    if not spread_ok:
        safeguard_note += "\n⚠ SPREAD WARNING: Spread is elevated — do NOT recommend SL/TP modifications."
    if in_blackout:
        safeguard_note += f"\n⚠ NEWS BLACKOUT: {blackout_event} is imminent — do NOT recommend SL/TP modifications."

    # Phase-specific question block
    if phase == PHASE_LOSS_CRITICAL:
        pct_to_sl = round(abs(phase_info["current_r"]) * 100)
        phase_block = (
            f"The trade is at {phase_info['current_r']:.2f}R — CRITICAL. "
            f"{pct_to_sl}% of the way to the stop loss.\n"
            "This is a near-SL situation requiring an immediate binary decision. Focus on:\n"
            "1. Has the original thesis been definitively invalidated by price action or news?\n"
            "2. Is this drawdown clearly directional? (pip loss > 2× ATR-M15 = directional)\n"
            "3. Is momentum (RSI, MACD, M15 bias) strongly confirming continuation against you?\n"
            "If thesis is broken OR the move is clearly directional: recommend Exit immediately.\n"
            "If this is extreme noise within the original thesis: Hold requires overwhelming specific evidence. "
            "No partial closes here — make a binary decision."
        )
        allowed_actions = (
            '"Exit" (strongly preferred — thesis broken or directional move confirmed) or '
            '"Hold" (must cite overwhelming thesis evidence and ATR context). '
            "No Trim — binary decision only."
        )

    elif phase == PHASE_LOSS_DEEP:
        pct_to_sl = round(abs(phase_info["current_r"]) * 100)
        trim_pct  = int(config.JOB2_LOSS_DEEP_TRIM_PCT)
        phase_block = (
            f"The trade is at {phase_info['current_r']:.2f}R — deep loss zone. "
            f"{pct_to_sl}% of the way to the stop loss.\n"
            "The original stop is at meaningful risk. Focus on:\n"
            "1. Has the thesis been materially weakened, even if not fully broken?\n"
            "2. Is this a measured pullback or a directional breakdown? (ATR-M15 × 1.5 rule)\n"
            f"3. Would a partial close ({trim_pct}%) meaningfully reduce dollar risk while preserving "
            "upside if the thesis recovers?\n"
            f"Partial close ({trim_pct}%) is preferred over a full hold when uncertain. "
            "If the thesis is clearly broken, recommend full Exit."
        )
        allowed_actions = (
            f'"Exit" (thesis broken), '
            f'"Trim" (partial close {trim_pct}% to reduce risk — requires your approval), or '
            '"Hold" (must cite specific evidence the thesis remains intact and loss is within ATR noise).'
        )

    elif phase == PHASE_LOSS_MODERATE:
        pct_to_sl = round(abs(phase_info["current_r"]) * 100)
        phase_block = (
            f"The trade is at {phase_info['current_r']:.2f}R — early warning zone. "
            f"{pct_to_sl}% of the way to the stop loss.\n"
            "This warrants a thesis review. Focus on:\n"
            "1. Is the pullback directional or noise? Compare pip loss to ATR-M15.\n"
            "2. Has any news or price action since entry weakened the original thesis?\n"
            "3. Are momentum indicators (RSI-M15, MACD histogram) turning against the trade direction?\n"
            "If two or more checks show deterioration: recommend Exit to preserve capital early.\n"
            "Otherwise Hold is justified — but state specifically what evidence supports the thesis."
        )
        allowed_actions = (
            '"Hold" (thesis intact, deterioration not confirmed) or '
            '"Exit" (early deterioration evident). '
            "No Trim at this stage."
        )

    elif phase == PHASE_LOSS_MILD:
        pct_to_sl = round(abs(phase_info["current_r"]) * 100)
        phase_block = (
            f"The trade is at {phase_info['current_r']:.2f}R — within normal noise range. "
            f"{pct_to_sl}% of the way to the stop loss.\n"
            "Focus on:\n"
            "1. Is the original thesis still intact? Any news or price action undermining it?\n"
            "2. Is the pullback normal retracement, or is momentum beginning to turn?\n"
            "   (Use RSI-M15, MACD histogram direction, and M15 momentum bias as evidence.)\n"
            "3. Is the original SL placement still valid given current volatility (ATR-M15)?"
        )
        allowed_actions = (
            '"Hold" (thesis intact, noise within ATR) or '
            '"Exit" (thesis weakening). '
            "Do NOT suggest Trim or SL/TP changes for a losing position."
        )

    elif phase == PHASE_EARLY:
        phase_block = (
            "The trade has not yet reached the breakeven threshold.\n"
            "Focus on: Is the original thesis still intact given current market conditions? "
            "Are there early warning signs that would justify an early exit before SL is hit?"
        )
        allowed_actions = '"Hold" or "Exit" only. Do NOT suggest Trim or SL/TP changes unless spread is OK and no blackout.'
    elif phase == PHASE_BREAKEVEN:
        be_note = (
            "NOTE: Breakeven SL move will be auto-executed by the system if safeguards pass."
            if config.JOB2_AUTO_BREAKEVEN else
            "Breakeven SL move requires your recommendation."
        )
        phase_block = (
            f"The trade has reached {phase_info['current_r']:.2f}R — breakeven zone.\n"
            f"{be_note}\n"
            "Focus on: Is the original thesis still valid and worth holding through? "
            "Or has the move stalled and should we exit while in profit?"
        )
        allowed_actions = '"Hold", "Exit", or "SetSL" (new breakeven SL price).'
    elif phase == PHASE_PROFIT:
        trim_note = (
            "NOTE: Position can be trimmed (volume allows partial close)."
            if _can_trim(pos) else
            "NOTE: Position volume is too small to trim — only Hold or Exit available."
        )
        phase_block = (
            f"The trade has reached {phase_info['current_r']:.2f}R — partial profit zone.\n"
            f"{trim_note}\n"
            "Focus on: Should we take partial profit now (and what %)? "
            "Is momentum continuing or showing exhaustion signals? "
            "Should we move TP to capture the current move?"
        )
        allowed_actions = (
            '"Hold", "Exit", or "Trim" (with suggested_trim_pct 25-75).'
            + (' "SetTP" also allowed if you see a better target.' if not in_blackout and spread_ok else "")
        )
    elif phase == PHASE_TRAIL:
        trail_note = (
            f"NOTE: ATR-based trailing SL will be auto-executed by the system (ATR-M15 × {config.JOB2_TRAIL_ATR_MULT})."
            if config.JOB2_AUTO_TRAIL else
            "ATR-based trailing requires your SetSL recommendation."
        )
        phase_block = (
            f"The trade has reached {phase_info['current_r']:.2f}R — trailing zone.\n"
            f"{trail_note}\n"
            "Focus on: Is momentum still strong enough to trail, or is the move exhausting? "
            "Should we tighten the trail multiplier or take full profit now?"
        )
        allowed_actions = '"Hold" (trail continues), "Exit" (take all profit now), or "Trim" (take some profit, trail remainder).'
    elif phase == PHASE_OVERDUE:
        phase_block = (
            f"The trade has been open {phase_info['hours_in_trade']:.0f}h against "
            f"an expected horizon of {phase_info['expected_hours']:.0f}h — "
            f"it is overdue by {phase_info['hours_in_trade'] - phase_info['expected_hours']:.0f}h.\n"
            "Focus on: Is there a compelling reason to hold beyond the expected horizon? "
            "If not, recommend Exit or at minimum SetSL to protect remaining profit."
        )
        allowed_actions = '"Hold" (must justify), "Exit", "Trim", or "SetSL" to tighten stop.'
    else:  # PHASE_UNKNOWN
        phase_block = (
            "Could not compute R-multiple (no original SL data). "
            "Assess the position on its current P&L and market conditions alone."
        )
        allowed_actions = '"Hold", "Exit", or "Trim".'

    # Suppress the dedicated invalidation section in loss phases — those phase
    # blocks already ask about thesis validity directly, avoiding double-prompting
    # that can confuse the model into Exit when loss is normal noise.
    _LOSS_PHASES = {PHASE_LOSS_MILD, PHASE_LOSS_MODERATE, PHASE_LOSS_DEEP, PHASE_LOSS_CRITICAL}
    if phase not in _LOSS_PHASES:
        invalidation_block = _build_invalidation_block(original, phase_info)
        invalidation_section = f"\n=== THEORY INVALIDATION CHECK ===\n{invalidation_block}\n"
        theory_fields = (
            '  "theory_invalidated": false,\n'
            '  "theory_invalidation_reason": ""\n'
        )
        theory_rules = (
            "- theory_invalidated: true only if the original thesis is definitively broken "
            "(see check above); if true your action MUST be Exit\n"
            "- theory_invalidation_reason: 1 sentence on what was specifically invalidated, or empty\n"
        )
    else:
        invalidation_section = ""
        theory_fields = (
            '  "theory_invalidated": false,\n'
            '  "theory_invalidation_reason": ""\n'
        )
        theory_rules = (
            "- theory_invalidated: set to true if — and only if — you determine in your phase "
            "assessment that the thesis is definitively broken (not just in loss); if true, "
            "action MUST be Exit\n"
            "- theory_invalidation_reason: 1 sentence on what was invalidated, or empty\n"
        )

    prompt = f"""\
You are managing an open {display} position. Review all data below and provide a
recommendation to protect capital and optimise the trade outcome.
{safeguard_note}

=== POSITION ===
{_fmt_position(pos, phase_info)}

=== ACCOUNT ===
{_fmt_account(account)}

=== MARKET SNAPSHOT ===
{_fmt_market_snapshot(tick, symbol)}

=== TECHNICAL INDICATORS ===
{_fmt_indicators(indicators)}

=== ORIGINAL TRADE THESIS ===
{_fmt_original_signal(original)}

=== PORTFOLIO CONTEXT (other open positions) ===
{_fmt_portfolio(portfolio, pos.get('ticket', -1))}

=== RECENT MARKET CONTEXT ===
{market_context}

=== PHASE ASSESSMENT ===
Current phase: {phase.upper()}
{phase_block}
{invalidation_section}
Provide your recommendation as JSON:
{{
  "action": "Hold | Trim | Exit | SetSL | SetTP | SetSLTP",
  "confidence": "High | Medium | Low",
  "rationale": "2-3 sentences referencing specific position and market data",
  "risk_note": "Any immediate risks to the position",
  "suggested_sl": null,
  "suggested_tp": null,
  "suggested_trim_pct": null,
{theory_fields}}}

Rules:
- Allowed actions for this phase: {allowed_actions}
- suggested_sl / suggested_tp: float prices or null
- suggested_trim_pct: integer 1-100 (% of position to close), or null
{theory_rules}- Return ONLY valid JSON, no other text."""

    return prompt


# ── LLM calls ─────────────────────────────────────────────────────────────────

def _call_routine_llm(prompt: str, symbol: str) -> dict | None:
    """
    Cheap model call for routine 30-min checks.
    Tries Azure GPT-5.2 first (free for user); falls back to Claude Haiku.
    """
    display = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["display"]
    system  = f"You are a {display} forex position manager. Be concise and precise."

    raw: str | None = None

    # ── Azure GPT (primary) ───────────────────────────────────────────────────
    try:
        from triage.triage_prompt import _get_azure_client
        azure_client = _get_azure_client()
        if azure_client is None:
            raise RuntimeError("Azure client not configured")
        import os as _os
        deployment = _os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5.2")
        resp = azure_client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=400,
        )
        raw = resp.choices[0].message.content.strip()
        logger.debug(f"Routine check [{symbol}]: Azure {deployment}")
    except Exception as az_err:
        logger.debug(f"Azure routine unavailable ({az_err}) — Haiku fallback")

    # ── Claude Haiku fallback ─────────────────────────────────────────────────
    if raw is None:
        try:
            msg = _get_client().messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()
            logger.debug(f"Routine check [{symbol}]: Haiku fallback")
        except Exception as haiku_err:
            logger.error(f"Routine LLM both failed [{symbol}]: {haiku_err}")
            return None

    clean = re.sub(r"^```[a-z]*\n?", "", raw)
    clean = re.sub(r"\n?```$", "", clean).strip()
    try:
        result = json.loads(clean)
        if not isinstance(result, dict):
            raise ValueError("Expected JSON object")
        return result
    except Exception as e:
        logger.error(f"Routine LLM parse failed [{symbol}]: {e}\nRaw: {raw[:300]}")
        return None


def _call_llm(prompt: str, symbol: str) -> dict | None:
    display = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["display"]
    try:
        message = _get_client().messages.create(
            model=config.ANALYSIS_MODEL,
            max_tokens=800,
            system=(
                f"You are a professional {display} forex risk manager. "
                "Your job is to protect open positions and optimise trade outcomes. "
                "Be precise and reference specific numbers from the data provided."
            ),
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
        return result
    except Exception as e:
        logger.error(f"Job 2 parse failed: {e}\nRaw: {raw[:500]}")
        return None


# ── signal builders ───────────────────────────────────────────────────────────

def _build_signal(pos: dict, rec: dict) -> dict[str, Any]:
    action = rec.get("action", "Hold")
    symbol = pos.get("symbol", config.MT5_SYMBOL)
    return {
        "signal":        action,
        "confidence":    rec.get("confidence", "Low"),
        "time_horizon":  "Intraday",
        "rationale":     rec.get("rationale", ""),
        "key_levels":    {
            "support":    rec.get("suggested_tp") if pos.get("type") == "sell" else rec.get("suggested_sl"),
            "resistance": rec.get("suggested_sl") if pos.get("type") == "sell" else rec.get("suggested_tp"),
        },
        "invalidation":  "",
        "risk_note":     rec.get("risk_note", ""),
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "_symbol":       symbol,
        "_ticket":       pos.get("ticket"),
        "_source":       "job2",
        "_analysis_tier":           rec.get("_analysis_tier", "routine"),
        "_escalated_from_routine":  rec.get("_escalated_from_routine", False),
        "sl":            rec.get("suggested_sl"),
        "tp":            rec.get("suggested_tp"),
        "suggested_trim_pct": rec.get("suggested_trim_pct"),
    }


def _build_trigger(pos: dict, rec: dict, phase_info: dict) -> dict[str, Any]:
    direction  = pos.get("type", "").upper()
    profit     = pos.get("profit", 0)
    profit_str = f"+${profit:.2f}" if profit >= 0 else f"-${abs(profit):.2f}"
    action     = rec.get("action", "Hold")
    r_str      = f" | {phase_info['current_r']:.2f}R" if phase_info["current_r"] is not None else ""
    emoji_map  = {"Exit": "🚨", "Trim": "✂️", "Hold": "✅", "SetSL": "🔒", "SetTP": "🎯", "SetSLTP": "🔒🎯"}
    if rec.get("_theory_override"):
        emoji_map["Exit"] = "💡🚨"  # theory-driven exit distinct from drawdown exit
    return {
        "headline": (
            f"{emoji_map.get(action, '')} Position Monitor — #{pos.get('ticket')} "
            f"{direction} {pos.get('volume')}L @ {pos.get('open_price')} | "
            f"P&L: {profit_str}{r_str} | Phase: {phase_info['phase'].upper()}"
        ),
        "score": (
            8 if phase_info.get("phase") == PHASE_LOSS_CRITICAL or action == "Exit"
            else 7 if phase_info.get("phase") == PHASE_LOSS_DEEP
            else 6 if action in ("Trim", "SetSL")
            else 4
        ),
        "tag":   "position_monitor",
    }


# ── auto-action notification ──────────────────────────────────────────────────

def _notify_auto_action(pos: dict, action_type: str, old_sl: float, new_sl: float) -> None:
    """Send a Slack notification for auto-executed SL moves."""
    symbol = pos.get("symbol", config.MT5_SYMBOL)
    logger.info(f"Auto {action_type} executed for #{pos.get('ticket')}: SL {old_sl} → {new_sl}")
    try:
        import os, requests, json as _json
        webhook_url = os.getenv("SLACK_WEBHOOK_URL")
        if not webhook_url:
            return
        msg = (
            f":lock: *Auto {action_type}* — #{pos.get('ticket')} "
            f"{pos.get('type', '').upper()} {symbol}\n"
            f">SL moved: `{old_sl}` → `{new_sl}`"
        )
        requests.post(
            webhook_url,
            data=_json.dumps({"text": msg}),
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
    except Exception as e:
        logger.debug(f"Auto-action Slack notify failed: {e}")


# ── main analysis cycle ───────────────────────────────────────────────────────

def analyze_position(pos: dict, portfolio: dict) -> dict[str, Any] | None:
    """
    Full enriched analysis for a single open position.
    Returns the LLM recommendation dict, or None on failure.
    Side-effects: may auto-execute breakeven SL or trail SL.
    """
    symbol = pos.get("symbol", config.MT5_SYMBOL)
    ticket = pos.get("ticket")

    # 1. Original signal context
    original = _get_original_signal(ticket)

    # 2. Phase engine
    phase_info = _compute_phase(pos, original)
    logger.info(
        f"#{ticket} phase={phase_info['phase']} "
        f"R={phase_info['current_r']} "
        f"pips={phase_info['pips_profit']:+.1f} "
        f"overdue={phase_info['is_overdue']}"
    )

    # 3. Operational safeguards
    spread_good, current_spread, normal_spread = _spread_ok(symbol)
    in_blackout, blackout_event                = _in_news_blackout(symbol)
    mods_allowed = spread_good and not in_blackout

    if not spread_good:
        logger.warning(
            f"#{ticket} spread {current_spread}p > {normal_spread * config.JOB2_MAX_SPREAD_MULT}p "
            f"— SL/TP modifications paused"
        )
    if in_blackout:
        logger.warning(f"#{ticket} news blackout ({blackout_event}) — SL/TP modifications paused")

    # 4. Auto-execute protective actions (before LLM, if safeguards pass)
    if mods_allowed:
        if phase_info["phase"] == PHASE_BREAKEVEN:
            old_sl = pos.get("sl", 0.0)
            result = _auto_breakeven(pos, phase_info)
            if result and result.get("ok"):
                new_sl = result.get("sl", pos.get("open_price", 0))
                _notify_auto_action(pos, "Breakeven", old_sl, new_sl)

        elif phase_info["phase"] == PHASE_TRAIL:
            indicators_for_trail = {}
            try:
                from pipeline.price_agent import get_indicators
                indicators_for_trail = get_indicators(symbol)
            except Exception:
                pass
            old_sl = pos.get("sl", 0.0)
            result = _auto_trail(pos, indicators_for_trail)
            if result and result.get("ok"):
                new_sl = result.get("sl", pos.get("current_price", 0.0))
                _notify_auto_action(pos, "Trail SL", old_sl, new_sl)

    # 5. Fetch indicators, market context, account, tick for LLM
    indicators: dict = {}
    try:
        from pipeline.price_agent import get_indicators
        indicators = get_indicators(symbol)
    except Exception as e:
        logger.warning(f"get_indicators failed for {symbol}: {e}")

    account = get_account_summary()
    tick    = get_current_tick(symbol=symbol)

    try:
        market_context = _build_job2_news_context(symbol)
    except Exception as e:
        logger.warning(f"Job2 news context failed: {e}")
        market_context = "(market context unavailable)"

    # 6. Build phase-specific prompt and call LLM
    prompt = _build_prompt(
        pos=pos,
        phase_info=phase_info,
        account=account,
        tick=tick,
        indicators=indicators,
        original=original,
        portfolio=portfolio,
        spread_ok=spread_good,
        in_blackout=in_blackout,
        blackout_event=blackout_event,
        market_context=market_context,
    )

    rec = _call_llm(prompt, symbol)
    if rec is None:
        return None

    # Theory invalidation override — if thesis definitively broken, force Exit
    # regardless of the phase-based action (e.g., a trailing trade where news
    # just reversed the macro catalyst should exit even if technically in profit)
    if rec.get("theory_invalidated") is True and rec.get("action") != "Exit":
        original_action = rec.get("action", "Hold")
        reason = rec.get("theory_invalidation_reason") or "(see rationale)"
        logger.warning(
            f"#{ticket} [{phase_info['phase']}] Theory invalidated — "
            f"overriding {original_action} → Exit. Reason: {reason}"
        )
        rec["action"]           = "Exit"
        rec["_theory_override"] = True
        rec["rationale"]        = (
            f"[THESIS INVALIDATED — overriding {original_action}] "
            + rec.get("rationale", "")
        )

    rec["ticket"]         = ticket
    rec["source"]         = "job2"
    rec["phase"]          = phase_info["phase"]
    rec["phase_info"]     = phase_info
    rec["_analysis_tier"] = "priority"
    return rec


def analyze_position_routine(pos: dict, portfolio: dict) -> dict | None:
    """
    Routine 30-min position check using cheap model (Azure GPT / Haiku fallback).
    Escalates to full Sonnet analysis if cheap model returns Exit [High confidence].
    Returns rec with _analysis_tier="routine" or "priority".
    """
    symbol = pos.get("symbol", config.MT5_SYMBOL)
    ticket = pos.get("ticket")

    original   = _get_original_signal(ticket)
    phase_info = _compute_phase(pos, original)

    # Seed sl_cache on first sight (mirrors watcher logic)
    if ticket and ticket not in _sl_cache:
        sl         = pos.get("sl") or 0.0
        open_price = pos.get("open_price", 0.0)
        direction  = pos.get("type", "buy")
        pip        = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["pip"]
        if sl:
            raw_pips = (open_price - sl) / pip if direction == "buy" else (sl - open_price) / pip
            sl_pips  = max(0.0, raw_pips)
            if sl_pips > 0:
                _sl_cache[ticket] = sl_pips

    logger.info(
        f"#{ticket} routine check — phase={phase_info['phase']} "
        f"R={phase_info['current_r']} pips={phase_info['pips_profit']:+.1f}"
    )

    indicators: dict = {}
    try:
        from pipeline.price_agent import get_indicators
        indicators = get_indicators(symbol)
    except Exception as e:
        logger.warning(f"get_indicators failed for {symbol}: {e}")

    prompt = _build_routine_prompt(pos, phase_info, indicators, original)
    rec    = _call_routine_llm(prompt, symbol)
    if rec is None:
        return None

    # Escalate to full Sonnet if cheap model signals Exit with High confidence
    if rec.get("action") == "Exit" and rec.get("confidence") == "High":
        logger.info(
            f"#{ticket} Routine Exit [High] — escalating to full Sonnet analysis"
        )
        full_rec = analyze_position(pos, portfolio)
        if full_rec:
            full_rec["_escalated_from_routine"] = True
        return full_rec  # may be None if Sonnet call fails — caller handles gracefully

    rec["ticket"]         = ticket
    rec["source"]         = "job2"
    rec["phase"]          = phase_info["phase"]
    rec["phase_info"]     = phase_info
    rec["_analysis_tier"] = "routine"
    return rec


# ── auto-action notification ──────────────────────────────────────────────────


def _notify_routine_hold(pos: dict, rec: dict) -> None:
    """
    Compact one-line notification for routine Hold results.
    Avoids the full Slack alert format to reduce noise.
    """
    from notifications.notifier import notify_text
    symbol     = pos.get("symbol", config.MT5_SYMBOL)
    direction  = pos.get("type", "").upper()
    ticket     = pos.get("ticket")
    profit     = pos.get("profit", 0.0)
    profit_str = f"+${profit:.2f}" if profit >= 0 else f"-${abs(profit):.2f}"
    phase      = rec.get("phase", "")
    current_r  = (rec.get("phase_info") or {}).get("current_r")
    r_str      = f" | {current_r:.2f}R" if current_r is not None else ""
    rationale  = (rec.get("rationale") or "")[:120]
    confidence = rec.get("confidence", "?")

    notify_text(
        f"\U0001f554 Routine #{ticket} {direction} {symbol}"
        f" | {profit_str}{r_str} | {phase.upper()}"
        f" | Hold [{confidence}] — {rationale}"
    )
    logger.info(f"#{ticket} [{phase}] Routine Hold notified (compact)")


def run_position_check() -> int:
    """
    Check all open positions across ACTIVE_PAIRS and generate recommendations.
    Returns number of positions checked.
    """
    if not is_connected():
        logger.warning("MT5 not connected — skipping position check")
        return 0

    positions: list[dict] = []
    for sym in config.ACTIVE_PAIRS:
        positions.extend(get_open_positions(symbol=sym))
    if not positions:
        logger.info(f"No open positions for {config.ACTIVE_PAIRS}")
        return 0

    logger.info(f"=== Job 2 checking {len(positions)} position(s) ===")

    # Build portfolio summary once — shared across all position analyses
    account      = get_account_summary()
    equity       = account.get("equity") or 10000.0
    portfolio    = get_portfolio_summary(equity)

    for pos in positions:
        ticket = pos.get("ticket")
        try:
            # Routine check: cheap model, escalates to Sonnet if Exit+High
            rec = analyze_position_routine(pos, portfolio)
            if rec is None:
                continue

            action     = rec.get("action", "Hold")
            confidence = rec.get("confidence", "Low")
            phase      = rec.get("phase", PHASE_UNKNOWN)
            tier       = rec.get("_analysis_tier", "routine")
            logger.info(f"Position #{ticket} [{phase}][{tier}]: {action} [{confidence}]")

            signal  = _build_signal(pos, rec)
            trigger = _build_trigger(pos, rec, rec.get("phase_info", {}))

            # Route: save actionable signals to store for approval
            if action in ("Exit", "Trim"):
                if action == "Trim" and not _can_trim(pos):
                    logger.warning(
                        f"#{ticket} Trim recommended but volume too small — downgrading to Hold"
                    )
                    action = "Hold"
                else:
                    from pipeline.signal_store import save_pending_signal
                    signal_id = save_pending_signal(signal, source="job2")
                    signal["_signal_id"] = signal_id
                    logger.info(f"Pending signal saved: {signal_id} — awaiting approval")

            elif action in ("SetSL", "SetTP", "SetSLTP"):
                from pipeline.signal_store import save_pending_signal
                signal_id = save_pending_signal(signal, source="job2")
                signal["_signal_id"] = signal_id
                logger.info(f"SetSL/TP signal saved: {signal_id} — awaiting approval")

            # Notification routing:
            #   Routine Hold → compact one-liner (low noise)
            #   Everything else (priority, Exit, Trim, SetSL/TP) → full alert
            if tier == "routine" and action == "Hold":
                _notify_routine_hold(pos, rec)
            else:
                notify(signal, trigger_item=trigger)

        except Exception as e:
            logger.error(f"Position check failed for #{ticket}: {e}", exc_info=True)

    return len(positions)


# ── phase threshold watcher ───────────────────────────────────────────────────

def _r_to_phase_simple(current_r: float) -> str:
    """
    Map a raw R-multiple to a phase label using only R thresholds.
    Used by the watcher — does NOT check the overdue time condition.
    """
    if current_r >= config.JOB2_TRAIL_R:
        return PHASE_TRAIL
    if current_r >= config.JOB2_PARTIAL_TP_R:
        return PHASE_PROFIT
    if current_r >= config.JOB2_BREAKEVEN_R:
        return PHASE_BREAKEVEN
    if current_r >= 0:
        return PHASE_EARLY
    if current_r >= config.JOB2_LOSS_MILD_R:
        return PHASE_LOSS_MILD
    if current_r >= config.JOB2_LOSS_MODERATE_R:
        return PHASE_LOSS_MODERATE
    if current_r >= config.JOB2_LOSS_DEEP_R:
        return PHASE_LOSS_DEEP
    return PHASE_LOSS_CRITICAL


def _watcher_compute_r(pos: dict) -> float | None:
    """
    Compute a live R-multiple for a position using cached original_sl_pips.
    Populates _sl_cache on first sight (from current SL — best available proxy
    when no journal entry exists). Returns None if sl_pips cannot be determined.
    """
    ticket    = pos["ticket"]
    symbol    = pos["symbol"]
    pip       = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["pip"]
    direction = pos["type"]

    # Populate cache on first sight (set once — frozen afterwards)
    if ticket not in _sl_cache:
        sl         = pos.get("sl") or 0.0
        open_price = pos["open_price"]
        if sl:
            raw = (open_price - sl) / pip if direction == "buy" else (sl - open_price) / pip
            sl_pips = max(0.0, raw)
            if sl_pips > 0:
                _sl_cache[ticket] = sl_pips
                logger.debug(f"Watcher cached #{ticket} original_sl_pips={sl_pips:.1f}")

    sl_pips = _sl_cache.get(ticket)
    if not sl_pips:
        return None

    # Live R from current position price (already updated by MT5 in positions_get)
    current_price = pos["current_price"]
    open_price    = pos["open_price"]
    pips_profit   = (
        (current_price - open_price) / pip if direction == "buy"
        else (open_price - current_price) / pip
    )
    return pips_profit / sl_pips


def _run_phase_watch_cycle(portfolio_cache: list) -> None:
    """
    Single watcher cycle: check all open positions for phase transitions.
    portfolio_cache is a 1-element list used to share the last portfolio summary
    across cycles (avoids rebuilding it every 60 seconds unnecessarily).
    """
    if not is_connected():
        return
    if not is_forex_market_open(
        config.MARKET_HOURS_START, config.MARKET_HOURS_END,
        config.MARKET_CLOSE_HOUR_EST, config.MARKET_OPEN_HOUR_EST,
    ):
        return

    positions: list[dict] = []
    for sym in config.ACTIVE_PAIRS:
        positions.extend(get_open_positions(symbol=sym))
    if not positions:
        return

    now_ts       = time.time()
    open_tickets = {pos["ticket"] for pos in positions}

    # Evict closed positions from all caches
    for ticket in list(_phase_state):
        if ticket not in open_tickets:
            _phase_state.pop(ticket, None)
            _sl_cache.pop(ticket, None)
            _last_watcher_trigger.pop(ticket, None)

    for pos in positions:
        ticket = pos["ticket"]
        symbol = pos["symbol"]

        live_r = _watcher_compute_r(pos)
        if live_r is None:
            continue

        new_phase  = _r_to_phase_simple(live_r)
        last_phase = _phase_state.get(ticket)

        # Always update stored phase
        _phase_state[ticket] = new_phase

        # First time we see this ticket — initialise state, don't trigger
        if last_phase is None:
            logger.debug(f"Watcher: #{ticket} {symbol} initialised at {new_phase} (r={live_r:.2f})")
            continue

        # No phase change — nothing to do
        if new_phase == last_phase:
            continue

        # Phase changed — check cooldown before triggering
        last_trigger = _last_watcher_trigger.get(ticket, 0.0)
        if (now_ts - last_trigger) < config.JOB2_PHASE_WATCH_COOLDOWN_SEC:
            logger.debug(
                f"Watcher: #{ticket} phase {last_phase}→{new_phase} "
                f"(r={live_r:.2f}) suppressed — cooldown active"
            )
            continue

        logger.info(
            f"Watcher: #{ticket} {symbol} phase transition "
            f"{last_phase} → {new_phase}  (r={live_r:.2f}) — triggering analysis"
        )
        _last_watcher_trigger[ticket] = now_ts

        # Build portfolio summary (refresh at most once per cycle, reuse cached)
        if not portfolio_cache:
            account  = get_account_summary()
            equity   = account.get("equity") or 10000.0
            portfolio_cache.append(get_portfolio_summary(equity))
        portfolio = portfolio_cache[0]

        pos_snap = dict(pos)   # snapshot for the sub-thread

        def _run(p=pos_snap, pf=portfolio, ph=new_phase) -> None:
            try:
                rec = analyze_position(p, pf)
                if rec is None:
                    return
                action  = rec.get("action", "Hold")
                signal  = _build_signal(p, rec)
                trigger = _build_trigger(p, rec, rec.get("phase_info", {}))
                if action == "Exit" or (action == "Trim" and _can_trim(p)):
                    from pipeline.signal_store import save_pending_signal
                    sid = save_pending_signal(signal, source="job2_watcher")
                    signal["_signal_id"] = sid
                    logger.info(f"Watcher pending signal: {sid} — {action} #{p['ticket']}")
                notify(signal, trigger_item=trigger)
            except Exception as e:
                logger.error(f"Watcher analysis failed #{pos_snap['ticket']}: {e}", exc_info=True)

        threading.Thread(target=_run, daemon=True, name=f"watcher-{ticket}").start()


def run_phase_watcher_loop() -> None:
    """
    Background loop: polls MT5 every JOB2_PHASE_WATCH_INTERVAL_SEC seconds.
    Triggers immediate analysis when a position crosses a phase boundary.
    Runs alongside (not instead of) the main 30-min Job 2 loop.
    """
    logger.info(
        f"Phase watcher started — "
        f"interval: {config.JOB2_PHASE_WATCH_INTERVAL_SEC}s  "
        f"cooldown: {config.JOB2_PHASE_WATCH_COOLDOWN_SEC}s"
    )
    portfolio_cache: list = []   # refreshed each cycle, shared across positions
    while True:
        try:
            portfolio_cache.clear()
            _run_phase_watch_cycle(portfolio_cache)
        except Exception as e:
            logger.error(f"Phase watcher cycle error: {e}", exc_info=True)
        time.sleep(config.JOB2_PHASE_WATCH_INTERVAL_SEC)


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
    logger.info("EUR/USD Position Monitor — Job 2 (Phase Engine)")
    logger.info(f"  Analysis model    : {config.ANALYSIS_MODEL}")
    logger.info(f"  Check interval    : {config.JOB2_CHECK_INTERVAL_MINUTES} min")
    logger.info(f"  Loss stages       : mild>{config.JOB2_LOSS_MILD_R}R  moderate>{config.JOB2_LOSS_MODERATE_R}R  deep>{config.JOB2_LOSS_DEEP_R}R  critical=below")
    logger.info(f"  Loss-deep trim    : {config.JOB2_LOSS_DEEP_TRIM_PCT}%")
    logger.info(f"  Breakeven R       : {config.JOB2_BREAKEVEN_R}R  (buffer: {config.JOB2_BREAKEVEN_BUFFER_PIPS}p)")
    logger.info(f"  Partial TP R      : {config.JOB2_PARTIAL_TP_R}R  ({config.JOB2_PARTIAL_TP_PCT}% trim)")
    logger.info(f"  Trail R           : {config.JOB2_TRAIL_R}R  (ATR × {config.JOB2_TRAIL_ATR_MULT})")
    logger.info(f"  Auto breakeven    : {config.JOB2_AUTO_BREAKEVEN}")
    logger.info(f"  Auto trail        : {config.JOB2_AUTO_TRAIL}")
    logger.info(f"  Spread limit      : {config.JOB2_MAX_SPREAD_MULT}× normal")
    logger.info(f"  News blackout     : {config.JOB2_NEWS_BLACKOUT_MIN} min before event")
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
