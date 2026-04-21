"""Portfolio-level risk calculator.

Used by:
  Job 3 execute_signal()   — gate that blocks new Long/Short when aggregate risk is too high
  Job 2 analyze_position() — provides correlated exposure context to the LLM

Correlation buckets (simplified USD-direction grouping):
  "usd_short" — profits from USD weakness
      EURUSD Long | GBPUSD Long | AUDUSD Long | USDJPY Short
  "usd_long"  — profits from USD strength
      EURUSD Short | GBPUSD Short | AUDUSD Short | USDJPY Long

Three limits enforced on new entries:
  JOB3_MAX_OPEN_TRADES         — hard position count cap
  JOB3_MAX_PORTFOLIO_RISK_PCT  — total risk across all open + new trade (% of equity)
  JOB3_MAX_CORRELATED_RISK_PCT — risk within one USD-direction bucket (% of equity)

Risk per open position is estimated as: sl_pips × lot × pip_value_per_lot.
If no SL is set, JOB3_DEFAULT_SL_PIPS is used as a conservative fallback.
"""
from __future__ import annotations

import config
from mt5.position_reader import get_open_positions
from utils.logger import get_logger

logger = get_logger(__name__)

# Pairs where USD is the base currency (not the quote).
# For these, a "buy" order profits from USD strength → usd_long bucket.
_USD_BASE_PAIRS = {"USDJPY", "USDCAD", "USDCHF"}


# ── helpers ───────────────────────────────────────────────────────────────────

def _usd_bucket(symbol: str, direction: str) -> str:
    """
    Map a symbol + order direction to a USD-correlation bucket.

    USD-quote pairs (EURUSD, GBPUSD, AUDUSD):
      buy  → long base / short USD → "usd_short"
      sell → short base / long USD → "usd_long"

    USD-base pairs (USDJPY, USDCAD, USDCHF):
      buy  → long USD             → "usd_long"
      sell → short USD            → "usd_short"
    """
    if symbol in _USD_BASE_PAIRS:
        return "usd_long" if direction == "buy" else "usd_short"
    return "usd_short" if direction == "buy" else "usd_long"


def _position_risk_usd(pos: dict) -> float:
    """
    Estimate the USD amount at risk to the SL for one open position.
    Falls back to JOB3_DEFAULT_SL_PIPS if no SL is set.
    """
    symbol    = pos["symbol"]
    pair      = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])
    pip       = pair["pip"]
    pip_value = pair["pip_value_per_lot"]

    sl         = pos.get("sl") or 0.0
    open_price = pos["open_price"]
    lot        = pos["volume"]
    direction  = pos["type"]  # "buy" or "sell"

    if sl:
        sl_pips = (
            max(0.0, (open_price - sl) / pip) if direction == "buy"
            else max(0.0, (sl - open_price) / pip)
        )
    else:
        sl_pips = config.JOB3_DEFAULT_SL_PIPS
        logger.debug(f"No SL on #{pos.get('ticket')} — using default {sl_pips}p for risk estimate")

    return sl_pips * lot * pip_value


# ── public API ────────────────────────────────────────────────────────────────

def get_portfolio_summary(equity: float) -> dict:
    """
    Aggregate risk across all currently open positions.

    Returns:
      total_risk_usd   — total USD at risk to SLs
      total_risk_pct   — total_risk_usd as % of equity
      bucket_risk_pct  — {"usd_short": x.x, "usd_long": x.x}
      open_count       — number of open positions
      positions        — list of per-position dicts with risk breakdown
    """
    positions = get_open_positions()
    summary: dict = {
        "total_risk_usd":  0.0,
        "total_risk_pct":  0.0,
        "bucket_risk_pct": {"usd_short": 0.0, "usd_long": 0.0},
        "open_count":      len(positions),
        "positions":       [],
    }

    for pos in positions:
        risk_usd  = _position_risk_usd(pos)
        risk_pct  = (risk_usd / equity * 100) if equity else 0.0
        bucket    = _usd_bucket(pos["symbol"], pos["type"])

        summary["total_risk_usd"]            += risk_usd
        summary["total_risk_pct"]            += risk_pct
        summary["bucket_risk_pct"][bucket]   += risk_pct
        summary["positions"].append({
            "ticket":    pos["ticket"],
            "symbol":    pos["symbol"],
            "direction": pos["type"],
            "lot":       pos["volume"],
            "risk_usd":  round(risk_usd, 2),
            "risk_pct":  round(risk_pct, 2),
            "bucket":    bucket,
            "sl_set":    bool(pos.get("sl")),
        })

    summary["total_risk_usd"]                   = round(summary["total_risk_usd"], 2)
    summary["total_risk_pct"]                   = round(summary["total_risk_pct"], 2)
    summary["bucket_risk_pct"]["usd_short"]     = round(summary["bucket_risk_pct"]["usd_short"], 2)
    summary["bucket_risk_pct"]["usd_long"]      = round(summary["bucket_risk_pct"]["usd_long"], 2)
    return summary


def check_portfolio_risk(
    direction: str,
    symbol: str,
    equity: float,
    new_risk_pct: float | None = None,
) -> dict:
    """
    Gate for new Long/Short entries. Checks three limits in order:
      1. Max open trade count
      2. Max total portfolio risk %
      3. Max correlated (same USD-bucket) risk %

    Args:
      direction    — "buy" or "sell"
      symbol       — e.g. "EURUSD"
      equity       — current account equity in USD
      new_risk_pct — actual uncertainty-scaled risk % for the new trade.
                     Falls back to JOB3_RISK_PCT (base) when not provided.

    Returns:
      {"ok": True,  "summary": {...}}
      {"ok": False, "reason": "...", "summary": {...}}
    """
    summary      = get_portfolio_summary(equity)
    new_bucket   = _usd_bucket(symbol, direction)
    new_risk_pct = new_risk_pct if new_risk_pct is not None else config.JOB3_RISK_PCT

    # 1. Hard position count cap
    if summary["open_count"] >= config.JOB3_MAX_OPEN_TRADES:
        return {
            "ok":     False,
            "reason": (
                f"Max open trades reached "
                f"({summary['open_count']}/{config.JOB3_MAX_OPEN_TRADES})"
            ),
            "summary": summary,
        }

    # 2. Total portfolio risk
    projected_total = summary["total_risk_pct"] + new_risk_pct
    if projected_total > config.JOB3_MAX_PORTFOLIO_RISK_PCT:
        return {
            "ok":     False,
            "reason": (
                f"Total portfolio risk {summary['total_risk_pct']:.1f}% "
                f"+ new {new_risk_pct:.1f}% = {projected_total:.1f}% "
                f"(max {config.JOB3_MAX_PORTFOLIO_RISK_PCT}%)"
            ),
            "summary": summary,
        }

    # 3. Correlated bucket risk
    projected_bucket = summary["bucket_risk_pct"][new_bucket] + new_risk_pct
    if projected_bucket > config.JOB3_MAX_CORRELATED_RISK_PCT:
        return {
            "ok":     False,
            "reason": (
                f"Correlated ({new_bucket}) risk "
                f"{summary['bucket_risk_pct'][new_bucket]:.1f}% "
                f"+ new {new_risk_pct:.1f}% = {projected_bucket:.1f}% "
                f"(max {config.JOB3_MAX_CORRELATED_RISK_PCT}%)"
            ),
            "summary": summary,
        }

    return {"ok": True, "summary": summary}
