"""Bar replay utility — shared by trade_journal and outcome_tracker.

find_first_exit() iterates M15 bars from a signal's creation time and returns
which level was hit first (SL or TP), when it was hit, and the pip P&L.

Both sl_hit and tp_hit can technically be breached in the same M15 bar (a wide
candle that wicks through both levels).  We treat that as SL first — conservative
assumption that price touched the adverse side before reversing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import config
from utils.logger import get_logger

logger = get_logger(__name__)


def find_first_exit(
    symbol:      str,
    direction:   str,           # "Long" or "Short"
    from_dt:     datetime,      # signal creation time (UTC, tz-aware)
    entry:       float | None,  # entry price for pip P&L calculation
    sl:          float | None,
    tp:          float | None,
    window_hours: int = 48,
) -> dict[str, Any] | None:
    """
    Replay M15 bars from from_dt to min(now, from_dt + window_hours) and find
    the first bar where price crossed SL or TP.

    Returns a dict:
        {
            "result":     "sl" | "tp" | "open",
            "hit_time":   ISO string (bar open time) | None,
            "hit_price":  float | None,   # the level that was touched
            "pnl_pips":   float | None,   # pip P&L vs entry (negative for loss)
        }

    Returns None if MT5 is unavailable or bar data cannot be fetched.
    "open" means the window elapsed without price reaching either level.
    """
    if direction not in ("Long", "Short"):
        return None
    if sl is None and tp is None:
        return None

    try:
        import MetaTrader5 as mt5
        from mt5.connector import get_timeframe, is_connected
        if not is_connected():
            return None
    except Exception as e:
        logger.warning(f"bar_replay: MT5 import/connect failed: {e}")
        return None

    try:
        pip    = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["pip"]
        tf     = get_timeframe("M15")
        now    = datetime.now(timezone.utc)
        to_dt  = min(from_dt + timedelta(hours=window_hours), now)

        # copy_rates_range requires naive UTC datetimes
        rates = mt5.copy_rates_range(
            symbol,
            tf,
            from_dt.replace(tzinfo=None),
            to_dt.replace(tzinfo=None),
        )
    except Exception as e:
        logger.warning(f"bar_replay: copy_rates_range failed for {symbol}: {e}")
        return None

    if rates is None or len(rates) == 0:
        logger.debug(f"bar_replay: no bars for {symbol} in window")
        return None

    is_long = direction == "Long"

    for bar in rates:
        h = bar["high"]
        l = bar["low"]
        bar_time = datetime.fromtimestamp(bar["time"], tz=timezone.utc).isoformat()

        if is_long:
            hit_sl = sl is not None and l <= sl
            hit_tp = tp is not None and h >= tp
            # Both in same bar → SL first (conservative)
            if hit_sl:
                return {
                    "result":    "sl",
                    "hit_time":  bar_time,
                    "hit_price": sl,
                    "pnl_pips":  round((sl - entry) / pip, 1) if entry else None,
                }
            if hit_tp:
                return {
                    "result":    "tp",
                    "hit_time":  bar_time,
                    "hit_price": tp,
                    "pnl_pips":  round((tp - entry) / pip, 1) if entry else None,
                }
        else:  # Short
            hit_sl = sl is not None and h >= sl
            hit_tp = tp is not None and l <= tp
            if hit_sl:
                return {
                    "result":    "sl",
                    "hit_time":  bar_time,
                    "hit_price": sl,
                    "pnl_pips":  round((entry - sl) / pip, 1) if entry else None,
                }
            if hit_tp:
                return {
                    "result":    "tp",
                    "hit_time":  bar_time,
                    "hit_price": tp,
                    "pnl_pips":  round((entry - tp) / pip, 1) if entry else None,
                }

    # Window elapsed, price never reached either level
    return {"result": "open", "hit_time": None, "hit_price": None, "pnl_pips": None}
