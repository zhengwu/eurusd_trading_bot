"""MT5 position reader — open positions, account summary, tick, OHLC bars.

Implemented using patterns from the user's trend_ma_EA.py:
  - update_position()        → get_open_positions()
  - check_account_protection() → get_account_summary()
  - get_current_tick()       → get_current_tick()
  - get_latest_bars()        → get_ohlc_bars()

All times returned as UTC ISO strings. Timezone conversion to EST is
handled upstream in the notifier / context builder.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import MetaTrader5 as mt5
import pandas as pd

import config
from utils.logger import get_logger

logger = get_logger(__name__)


def get_open_positions(symbol: str | None = None) -> list[dict[str, Any]]:
    """
    Return all open positions, optionally filtered by symbol.
    Each dict matches the schema expected by Job 2 and Job 3.
    """
    try:
        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if positions is None:
            return []

        result = []
        for pos in positions:
            result.append({
                "ticket":        pos.ticket,
                "symbol":        pos.symbol,
                "type":          "buy" if pos.type == mt5.ORDER_TYPE_BUY else "sell",
                "volume":        pos.volume,
                "open_price":    pos.price_open,
                "current_price": pos.price_current,
                "sl":            pos.sl,
                "tp":            pos.tp,
                "profit":        pos.profit,
                "swap":          pos.swap,
                "open_time":     datetime.fromtimestamp(pos.time, tz=timezone.utc).isoformat(),
                "magic":         pos.magic,
                "comment":       pos.comment,
            })
        return result
    except Exception as e:
        logger.error(f"get_open_positions failed: {e}")
        return []


def get_account_summary() -> dict[str, Any]:
    """Return account summary from MT5."""
    try:
        info = mt5.account_info()
        if info is None:
            logger.warning("MT5 account_info() returned None")
            return {"equity": None, "balance": None, "margin": None,
                    "free_margin": None, "margin_level": None}
        return {
            "equity":       info.equity,
            "balance":      info.balance,
            "margin":       info.margin,
            "free_margin":  info.margin_free,
            "margin_level": info.margin_level,
            "currency":     info.currency,
            "server":       info.server,
            "login":        info.login,
        }
    except Exception as e:
        logger.error(f"get_account_summary failed: {e}")
        return {"equity": None, "balance": None, "margin": None,
                "free_margin": None, "margin_level": None}


def get_current_tick(symbol: str | None = None) -> dict[str, Any] | None:
    """Return current bid/ask/spread for a symbol."""
    sym = symbol or config.MT5_SYMBOL
    try:
        tick = mt5.symbol_info_tick(sym)
        if tick is None:
            return None
        return {
            "bid":    tick.bid,
            "ask":    tick.ask,
            "spread": round(tick.ask - tick.bid, 5),
            "time":   datetime.fromtimestamp(tick.time, tz=timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"get_current_tick failed: {e}")
        return None


def get_ohlc_bars(
    symbol: str | None = None,
    timeframe_str: str | None = None,
    count: int = 100,
) -> pd.DataFrame | None:
    """
    Return recent OHLC bars as a UTC-indexed DataFrame.
    Columns: open, high, low, close, tick_volume, spread, real_volume
    """
    sym = symbol or config.MT5_SYMBOL
    tf_str = timeframe_str or config.MT5_TIMEFRAME
    try:
        from mt5.connector import get_timeframe
        tf = get_timeframe(tf_str)
        rates = mt5.copy_rates_from_pos(sym, tf, 0, count)
        if rates is None or len(rates) == 0:
            logger.warning(f"No OHLC data returned for {sym} {tf_str}")
            return None
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.set_index("time", inplace=True)
        return df
    except Exception as e:
        logger.error(f"get_ohlc_bars failed: {e}")
        return None
