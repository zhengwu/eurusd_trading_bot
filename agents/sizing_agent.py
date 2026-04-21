"""Position Sizing Agent.

Computes technical SL, TP, and uncertainty-scaled lot size for a debated
Long/Short signal. Uses confirmed H4 pivot highs/lows for structurally-grounded
SL/TP placement instead of relying on the LLM or debate judge.

Flow:
  triage/scanner.py calls compute_sizing() after the ensemble debate.
  The agent writes signal["sl"], signal["tp"], signal["_recommended_risk_pct"],
  and signal["_size_reason"] in-place, then returns a sizing summary dict.

  Lot is NOT stored on the signal (_lot_override removed). Instead, execute_signal()
  recomputes lot fresh at execution time using the stored uncertainty_score against
  the live price — ensuring lot and SL distance are always consistent.

Fallback: if MT5 data is unavailable, the function returns None and the caller
keeps whatever SL/TP the LLM/debate judge wrote.
"""
from __future__ import annotations

import config
from mt5.risk_manager import recommend_position_size, sl_pips_from_price
from utils.logger import get_logger

logger = get_logger(__name__)


# ── MT5 data helpers ──────────────────────────────────────────────────────────

def _fetch_h4_bars(symbol: str) -> list[dict] | None:
    """Fetch the last SIZING_H4_BARS H4 bars. Returns list of OHLC dicts or None."""
    try:
        import MetaTrader5 as mt5
        bars = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H4, 0, config.SIZING_H4_BARS)
        if bars is None or len(bars) == 0:
            return None
        return [
            {"open": float(b["open"]), "high": float(b["high"]),
             "low":  float(b["low"]),  "close": float(b["close"])}
            for b in bars
        ]
    except Exception as e:
        logger.warning(f"[{symbol}] H4 bars fetch failed: {e}")
        return None


def _compute_atr(bars: list[dict], period: int = 14) -> float | None:
    """ATR-{period} using simple average of true ranges."""
    if len(bars) < period + 1:
        return None
    trs = [
        max(
            bars[i]["high"] - bars[i]["low"],
            abs(bars[i]["high"] - bars[i - 1]["close"]),
            abs(bars[i]["low"]  - bars[i - 1]["close"]),
        )
        for i in range(1, len(bars))
    ]
    return sum(trs[-period:]) / period


# ── Pivot detection ───────────────────────────────────────────────────────────

def _pivot_lows(bars: list[dict], n: int) -> list[float]:
    """
    Return confirmed pivot low prices, most recent first.

    A pivot low at index i requires bars[i].low to be strictly lower than
    every bar in the window [i-n, i+n] (excluding i itself). The last n bars
    are excluded since they have no right-side confirmation yet.

    Bars are assumed oldest-first (index 0 = oldest, index -1 = newest).
    """
    limit  = len(bars) - n
    pivots = []
    for i in range(n, limit):
        lo = bars[i]["low"]
        if all(bars[i + k]["low"] > lo for k in range(-n, n + 1) if k != 0):
            pivots.append((i, lo))
    pivots.sort(key=lambda x: x[0], reverse=True)   # most recent first
    return [p for _, p in pivots]


def _pivot_highs(bars: list[dict], n: int) -> list[float]:
    """
    Return confirmed pivot high prices, most recent first.

    A pivot high at index i requires bars[i].high to be strictly higher than
    every bar in the window [i-n, i+n] (excluding i itself).
    """
    limit  = len(bars) - n
    pivots = []
    for i in range(n, limit):
        hi = bars[i]["high"]
        if all(bars[i + k]["high"] < hi for k in range(-n, n + 1) if k != 0):
            pivots.append((i, hi))
    pivots.sort(key=lambda x: x[0], reverse=True)   # most recent first
    return [p for _, p in pivots]


# ── SL / TP derivation ────────────────────────────────────────────────────────

def _derive_sl_tp(
    direction: str,
    entry: float,
    bars: list[dict],
    atr: float,
    symbol: str,
) -> tuple[float, float, str]:
    """
    Derive SL and TP prices from H4 pivot structure.

    SL logic:
      Long  — nearest confirmed pivot low below entry, padded by SIZING_SL_ATR_BUFFER × ATR
      Short — nearest confirmed pivot high above entry, padded by SIZING_SL_ATR_BUFFER × ATR
      "Nearest" means most recent by bar time, not closest in price — the most recent
      swing represents the level the market has been actively respecting.

    TP logic:
      Long  — closest (by price) confirmed pivot high above entry + SL × SIZING_MIN_RR
      Short — closest (by price) confirmed pivot low  below entry − SL × SIZING_MIN_RR
      TP is placed SIZING_TP_ATR_BUFFER × ATR inside the pivot (just before the level)
      to improve fill probability. Falls back to SL × SIZING_MIN_RR if no pivot qualifies.

    Returns (sl_price, tp_price, one-line rationale).
    """
    pip      = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["pip"]
    decimals = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["price_decimals"]
    n        = config.SIZING_PIVOT_N
    sl_buf   = config.SIZING_SL_ATR_BUFFER * atr
    tp_buf   = config.SIZING_TP_ATR_BUFFER * atr
    min_rr   = config.SIZING_MIN_RR

    if direction == "buy":
        # ── SL: nearest (most recent) pivot low below entry ────────────────
        lows_below = [p for p in _pivot_lows(bars, n) if p < entry]
        if lows_below:
            pivot_sl = lows_below[0]   # most recent below entry
            sl_price = round(pivot_sl - sl_buf, decimals)
            sl_note  = f"pivot low {round(pivot_sl, decimals)} − {config.SIZING_SL_ATR_BUFFER}×ATR"
        else:
            sl_price = round(entry - config.JOB3_DEFAULT_SL_PIPS * pip, decimals)
            sl_note  = f"no pivot below entry — {config.JOB3_DEFAULT_SL_PIPS}p fallback"

        sl_pips     = max(1.0, (entry - sl_price) / pip)
        min_tp_dist = entry + sl_pips * min_rr * pip

        # ── TP: closest (by price) pivot high above minimum R:R threshold ──
        highs_above = [p for p in _pivot_highs(bars, n) if p >= min_tp_dist]
        if highs_above:
            pivot_tp = min(highs_above)   # closest price above min_tp_dist
            tp_price = round(pivot_tp - tp_buf, decimals)
            tp_note  = f"pivot high {round(pivot_tp, decimals)} − {config.SIZING_TP_ATR_BUFFER}×ATR"
        else:
            tp_price = round(entry + sl_pips * min_rr * pip, decimals)
            tp_note  = f"no pivot above {min_rr}:1 — R:R fallback"

    else:  # sell
        # ── SL: nearest (most recent) pivot high above entry ───────────────
        highs_above = [p for p in _pivot_highs(bars, n) if p > entry]
        if highs_above:
            pivot_sl = highs_above[0]   # most recent above entry
            sl_price = round(pivot_sl + sl_buf, decimals)
            sl_note  = f"pivot high {round(pivot_sl, decimals)} + {config.SIZING_SL_ATR_BUFFER}×ATR"
        else:
            sl_price = round(entry + config.JOB3_DEFAULT_SL_PIPS * pip, decimals)
            sl_note  = f"no pivot above entry — {config.JOB3_DEFAULT_SL_PIPS}p fallback"

        sl_pips     = max(1.0, (sl_price - entry) / pip)
        min_tp_dist = entry - sl_pips * min_rr * pip

        # ── TP: closest (by price) pivot low below minimum R:R threshold ───
        lows_below = [p for p in _pivot_lows(bars, n) if p <= min_tp_dist]
        if lows_below:
            pivot_tp = max(lows_below)   # closest price below min_tp_dist
            tp_price = round(pivot_tp + tp_buf, decimals)
            tp_note  = f"pivot low {round(pivot_tp, decimals)} + {config.SIZING_TP_ATR_BUFFER}×ATR"
        else:
            tp_price = round(entry - sl_pips * min_rr * pip, decimals)
            tp_note  = f"no pivot below {min_rr}:1 — R:R fallback"

    return sl_price, tp_price, f"SL: {sl_note} | TP: {tp_note}"


# ── Public API ────────────────────────────────────────────────────────────────

def compute_sizing(signal: dict, symbol: str, equity: float) -> dict | None:
    """
    Derive technical SL/TP and uncertainty-scaled lot size for a Long/Short signal.

    Writes sl, tp, _recommended_risk_pct, _size_reason onto signal in-place.
    Does NOT write _lot_override — lot is recomputed fresh at execution time
    via recommend_position_size() in execute_signal(), keeping lot and SL pips
    consistent with the live price at the moment the order is placed.

    Args:
        signal  — debated signal dict; must have signal="Long" or "Short"
        symbol  — e.g. "EURUSD"
        equity  — current account equity in USD

    Returns the sizing summary dict, or None if MT5 data is unavailable
    (caller retains whatever SL/TP the LLM/debate judge wrote).
    """
    action = signal.get("signal")
    if action not in ("Long", "Short"):
        return None

    direction = "buy" if action == "Long" else "sell"

    # ── Fetch H4 bars ─────────────────────────────────────────────────────
    bars = _fetch_h4_bars(symbol)
    min_bars = config.SIZING_PIVOT_N * 2 + config.SIZING_H4_BARS // 10
    if not bars or len(bars) < min_bars:
        logger.warning(f"[{symbol}] Sizing agent: insufficient H4 bars ({len(bars) if bars else 0}) — skipping")
        return None

    # ── ATR ───────────────────────────────────────────────────────────────
    atr = _compute_atr(bars)
    if not atr:
        logger.warning(f"[{symbol}] Sizing agent: ATR unavailable — skipping")
        return None

    # ── Live entry price ──────────────────────────────────────────────────
    try:
        from mt5.position_reader import get_current_tick
        tick = get_current_tick(symbol=symbol)
        if tick is None:
            logger.warning(f"[{symbol}] Sizing agent: no tick data — skipping")
            return None
        entry = tick["ask"] if direction == "buy" else tick["bid"]
    except Exception as e:
        logger.warning(f"[{symbol}] Sizing agent: tick fetch failed — {e}")
        return None

    # ── Technical SL / TP ─────────────────────────────────────────────────
    sl_price, tp_price, sl_tp_note = _derive_sl_tp(direction, entry, bars, atr, symbol)

    pip     = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["pip"]
    decimals = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["price_decimals"]
    sl_pips = sl_pips_from_price(sl_price, entry, direction, symbol=symbol)
    if sl_pips < 1:
        logger.warning(f"[{symbol}] Sizing agent: SL too tight ({sl_pips:.1f}p) — using default {config.JOB3_DEFAULT_SL_PIPS}p")
        sl_pips  = float(config.JOB3_DEFAULT_SL_PIPS)
        sl_price = round(
            entry - sl_pips * pip if direction == "buy" else entry + sl_pips * pip,
            decimals,
        )

    # ── Lot sizing ────────────────────────────────────────────────────────
    unc    = signal.get("uncertainty_score") or signal.get("_uncertainty_score")
    sizing = recommend_position_size(unc, equity, sl_pips, symbol=symbol)

    tp_pips = abs(tp_price - entry) / pip
    rr      = round(tp_pips / sl_pips, 2) if sl_pips > 0 else None

    result = {
        "sl":        sl_price,
        "tp":        tp_price,
        "sl_pips":   round(sl_pips, 1),
        "tp_pips":   round(tp_pips, 1),
        "lot":       sizing["lot"],
        "risk_pct":  sizing["risk_pct"],
        "rr":        rr,
        "atr_h4":    round(atr, 6),
        "rationale": f"{sl_tp_note} | {sizing['reason']}",
    }

    # ── Write back onto signal ────────────────────────────────────────────
    signal["sl"]                    = sl_price
    signal["tp"]                    = tp_price
    signal["_recommended_risk_pct"] = sizing["risk_pct"]
    signal["_size_reason"]          = result["rationale"]

    logger.info(
        f"[{symbol}] Sizing: {action} entry≈{round(entry, decimals)} "
        f"SL={sl_price} ({sl_pips:.1f}p)  TP={tp_price} ({tp_pips:.1f}p)  "
        f"RR={rr}  lot={sizing['lot']}  risk={sizing['risk_pct']}%"
    )
    logger.info(f"[{symbol}]   {sl_tp_note}")
    return result
