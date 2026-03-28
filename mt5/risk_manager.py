"""Risk manager — lot sizing and SL/TP calculation for Job 3.

All calculations use account equity, not balance, to account for
floating P&L on open positions.
"""
from __future__ import annotations

import config
from utils.logger import get_logger

logger = get_logger(__name__)


def _pip(symbol: str) -> float:
    """Return pip size for the given symbol."""
    return config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["pip"]


def _pip_value(symbol: str) -> float:
    """Return pip value per standard lot (USD) for the given symbol."""
    return config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["pip_value_per_lot"]


def calculate_lot_size(equity: float, risk_pct: float, sl_pips: float, symbol: str | None = None) -> float:
    """
    Calculate position size based on account equity and SL distance.

    Formula: lot = (equity × risk_pct/100) / (sl_pips × pip_value_per_lot)
    Clamped to [JOB3_MIN_LOT, JOB3_MAX_LOT] and rounded to 2 decimal places.
    """
    sym = symbol or config.MT5_SYMBOL
    if sl_pips <= 0:
        logger.warning(f"Invalid sl_pips={sl_pips}, using default {config.JOB3_DEFAULT_SL_PIPS}")
        sl_pips = config.JOB3_DEFAULT_SL_PIPS

    risk_amount = equity * (risk_pct / 100.0)
    raw_lot = risk_amount / (sl_pips * _pip_value(sym))
    lot = round(max(config.JOB3_MIN_LOT, min(config.JOB3_MAX_LOT, raw_lot)), 2)
    logger.info(
        f"Lot sizing [{sym}]: equity=${equity:.2f} risk={risk_pct}% "
        f"sl={sl_pips}pips → {lot} lots (raw={raw_lot:.4f})"
    )
    return lot


def calculate_sl_tp(
    signal: dict,
    current_price: float,
    direction: str,
    symbol: str | None = None,
) -> tuple[float | None, float | None]:
    """
    Derive SL and TP prices from signal key_levels.
    Falls back to fixed pip distances from config if key_levels are missing.

    direction: "buy" or "sell"
    Returns (sl_price, tp_price) — either may be None if calculation fails.
    """
    levels = signal.get("key_levels") or {}
    support = levels.get("support")
    resistance = levels.get("resistance")

    sl_price: float | None = None
    tp_price: float | None = None

    if direction == "buy":
        # SL below support, TP at resistance
        if support is not None:
            try:
                sl_price = float(support)
            except (TypeError, ValueError):
                pass
        if resistance is not None:
            try:
                tp_price = float(resistance)
            except (TypeError, ValueError):
                pass
    else:  # sell
        # SL above resistance, TP at support
        if resistance is not None:
            try:
                sl_price = float(resistance)
            except (TypeError, ValueError):
                pass
        if support is not None:
            try:
                tp_price = float(support)
            except (TypeError, ValueError):
                pass

    sym = symbol or config.MT5_SYMBOL
    pip = _pip(sym)

    # Fallback to fixed pip distances
    if sl_price is None:
        offset = config.JOB3_DEFAULT_SL_PIPS * pip
        sl_price = round(
            current_price - offset if direction == "buy" else current_price + offset,
            5,
        )
        logger.info(f"SL fallback [{sym}]: {sl_price} ({config.JOB3_DEFAULT_SL_PIPS} pips from {current_price})")

    if tp_price is None:
        offset = config.JOB3_DEFAULT_TP_PIPS * pip
        tp_price = round(
            current_price + offset if direction == "buy" else current_price - offset,
            5,
        )
        logger.info(f"TP fallback [{sym}]: {tp_price} ({config.JOB3_DEFAULT_TP_PIPS} pips from {current_price})")

    return sl_price, tp_price


def sl_pips_from_price(sl_price: float, current_price: float, direction: str, symbol: str | None = None) -> float:
    """Return SL distance in pips given an SL price."""
    pip = _pip(symbol or config.MT5_SYMBOL)
    if direction == "buy":
        return max(0.0, (current_price - sl_price) / pip)
    else:
        return max(0.0, (sl_price - current_price) / pip)
