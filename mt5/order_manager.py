"""MT5 order manager — open, close, and modify positions for Job 3.

All functions return a result dict:
  {"ok": True,  "ticket": int, ...}   on success
  {"ok": False, "error": str}         on failure
"""
from __future__ import annotations

from typing import Any

import MetaTrader5 as mt5

import config
from utils.logger import get_logger

logger = get_logger(__name__)

_DEVIATION = 20  # max price deviation in points


def _filling_mode(symbol: str) -> int:
    """Return the first supported filling mode for the symbol."""
    info = mt5.symbol_info(symbol)
    if info is None:
        return mt5.ORDER_FILLING_FOK
    mode = info.filling_mode
    if mode & 1:   # FOK supported
        return mt5.ORDER_FILLING_FOK
    if mode & 2:   # IOC supported
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def _last_error_str() -> str:
    err = mt5.last_error()
    return f"MT5 error {err[0]}: {err[1]}" if err else "unknown MT5 error"


def open_position(
    symbol: str,
    direction: str,          # "buy" or "sell"
    lot: float,
    sl: float | None = None,
    tp: float | None = None,
    comment: str = "eurusd_agent",
) -> dict[str, Any]:
    """
    Place a market order and return the result dict.
    """
    order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return {"ok": False, "error": f"No tick data for {symbol}"}

    price = tick.ask if direction == "buy" else tick.bid

    request = {
        "action":    mt5.TRADE_ACTION_DEAL,
        "symbol":    symbol,
        "volume":    lot,
        "type":      order_type,
        "price":     price,
        "deviation": _DEVIATION,
        "magic":     config.MT5_MAGIC_NUMBER,
        "comment":   comment,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": _filling_mode(symbol),
    }
    if sl is not None:
        request["sl"] = sl
    if tp is not None:
        request["tp"] = tp

    logger.info(
        f"Sending order: {direction.upper()} {lot} {symbol} "
        f"@ {price} SL={sl} TP={tp}"
    )

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        error = _last_error_str() if result is None else f"retcode={result.retcode}"
        logger.error(f"Order failed: {error}")
        return {"ok": False, "error": error}

    logger.info(
        f"Order executed: ticket={result.order} "
        f"{direction.upper()} {lot} {symbol} @ {result.price}"
    )
    return {
        "ok":     True,
        "ticket": result.order,
        "price":  result.price,
        "volume": lot,
        "direction": direction,
    }


def close_position(ticket: int, lot: float | None = None) -> dict[str, Any]:
    """
    Close a position fully (lot=None) or partially (lot=specific size).
    """
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return {"ok": False, "error": f"Position #{ticket} not found"}

    pos = positions[0]
    close_lot = lot if lot is not None else pos.volume
    close_lot = round(min(close_lot, pos.volume), 2)

    # Closing direction is opposite to open direction
    if pos.type == mt5.ORDER_TYPE_BUY:
        order_type = mt5.ORDER_TYPE_SELL
        tick = mt5.symbol_info_tick(pos.symbol)
        price = tick.bid if tick else pos.price_current
    else:
        order_type = mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(pos.symbol)
        price = tick.ask if tick else pos.price_current

    request = {
        "action":    mt5.TRADE_ACTION_DEAL,
        "symbol":    pos.symbol,
        "volume":    close_lot,
        "type":      order_type,
        "position":  ticket,
        "price":     price,
        "deviation": _DEVIATION,
        "magic":     config.MT5_MAGIC_NUMBER,
        "comment":   "eurusd_agent_close",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": _filling_mode(pos.symbol),
    }

    partial = close_lot < pos.volume
    logger.info(
        f"Closing position #{ticket} {'(partial ' + str(close_lot) + ' lots)' if partial else '(full)'}"
    )

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        error = _last_error_str() if result is None else f"retcode={result.retcode}"
        logger.error(f"Close failed: {error}")
        return {"ok": False, "error": error}

    logger.info(f"Position #{ticket} closed @ {result.price} — {close_lot} lots")
    return {
        "ok":      True,
        "ticket":  ticket,
        "price":   result.price,
        "volume":  close_lot,
        "partial": partial,
    }


def place_limit_order(
    symbol: str,
    direction: str,          # "buy" or "sell"
    lot: float,
    price: float,            # limit price — fills only when market reaches this level
    sl: float | None = None,
    tp: float | None = None,
    comment: str = "eurusd_agent",
) -> dict[str, Any]:
    """
    Place a pending limit order (Buy Limit / Sell Limit).
    The order sits in the MT5 order book until the market reaches `price`.
    Returns the same result dict shape as open_position().
    """
    order_type = mt5.ORDER_TYPE_BUY_LIMIT if direction == "buy" else mt5.ORDER_TYPE_SELL_LIMIT

    request = {
        "action":       mt5.TRADE_ACTION_PENDING,
        "symbol":       symbol,
        "volume":       lot,
        "type":         order_type,
        "price":        price,
        "magic":        config.MT5_MAGIC_NUMBER,
        "comment":      comment,
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": _filling_mode(symbol),
    }
    if sl is not None:
        request["sl"] = sl
    if tp is not None:
        request["tp"] = tp

    logger.info(
        f"Sending limit order: {direction.upper()} {lot} {symbol} "
        f"@ limit {price} SL={sl} TP={tp}"
    )

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        error = _last_error_str() if result is None else f"retcode={result.retcode}"
        logger.error(f"Limit order failed: {error}")
        return {"ok": False, "error": error}

    logger.info(
        f"Limit order placed: ticket={result.order} "
        f"{direction.upper()} {lot} {symbol} @ limit {price}"
    )
    return {
        "ok":         True,
        "ticket":     result.order,
        "price":      price,
        "volume":     lot,
        "direction":  direction,
        "order_type": "limit",
    }


def modify_sl_tp(
    ticket: int,
    sl: float | None = None,
    tp: float | None = None,
) -> dict[str, Any]:
    """Modify SL and/or TP on an existing position."""
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return {"ok": False, "error": f"Position #{ticket} not found"}

    pos = positions[0]
    request = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "symbol":   pos.symbol,
        "position": ticket,
        "sl":       sl if sl is not None else pos.sl,
        "tp":       tp if tp is not None else pos.tp,
        "magic":    config.MT5_MAGIC_NUMBER,
    }

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        error = _last_error_str() if result is None else f"retcode={result.retcode}"
        logger.error(f"Modify SL/TP failed: {error}")
        return {"ok": False, "error": error}

    logger.info(f"Position #{ticket} modified — SL={sl} TP={tp}")
    return {"ok": True, "ticket": ticket, "sl": sl, "tp": tp}
