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


def uncertainty_risk_pct(base_pct: float, uncertainty: int | float | None) -> float:
    """
    Scale the base risk % down based on the ensemble debate uncertainty score.

    Uses the tier table in config.JOB3_UNCERTAINTY_TIERS (list of (threshold, multiplier)
    sorted ascending by threshold). The first tier whose threshold >= uncertainty wins.
    Falls back to base_pct when uncertainty is None (no debate ran).

    Example with base_pct=2.0:
      uncertainty=20  → 2.0 × 1.00 = 2.0%  (high conviction)
      uncertainty=45  → 2.0 × 0.75 = 1.5%  (medium conviction)
      uncertainty=65  → 2.0 × 0.50 = 1.0%  (low conviction, passed veto)
      uncertainty=None→ 2.0%               (no debate data)
    """
    if uncertainty is None:
        return base_pct
    for threshold, multiplier in sorted(config.JOB3_UNCERTAINTY_TIERS, key=lambda t: t[0]):
        if uncertainty <= threshold:
            scaled = round(base_pct * multiplier, 4)
            logger.debug(
                f"uncertainty_risk_pct: uncertainty={uncertainty} → tier <={threshold} "
                f"({multiplier:.0%}) → {scaled}% (base={base_pct}%)"
            )
            return scaled
    # uncertainty exceeds all tiers — should be vetoed before reaching here
    return round(base_pct * 0.50, 4)


def recommend_position_size(
    uncertainty: int | float | None,
    equity: float,
    sl_pips: float,
    symbol: str | None = None,
) -> dict:
    """
    Compute recommended lot size based on remaining portfolio risk budget
    and the signal's uncertainty score.

    Logic:
      remaining_pct = JOB3_MAX_PORTFOLIO_RISK_PCT - currently_used_pct
      allocated_pct = remaining_pct × uncertainty_multiplier
      final_pct     = min(allocated_pct, JOB3_RISK_PCT)   # cap at base per-trade limit
      lot           = calculate_lot_size(equity, final_pct, sl_pips, symbol)

    Falls back to JOB3_RISK_PCT flat when uncertainty is None (no debate ran)
    or when portfolio data is unavailable.

    Returns:
      {
        "risk_pct":  float,   # effective risk % used for lot sizing
        "lot":       float,   # recommended lot size
        "reason":    str,     # human-readable sizing rationale
      }
    """
    sym = symbol or config.MT5_SYMBOL

    # ── Get portfolio risk already in use ─────────────────────────────────────
    used_pct = 0.0
    max_pct  = config.JOB3_MAX_PORTFOLIO_RISK_PCT
    try:
        from mt5.portfolio_risk import get_portfolio_summary
        summary  = get_portfolio_summary(equity)
        used_pct = summary.get("total_risk_pct", 0.0)
    except Exception as e:
        logger.warning(f"recommend_position_size: portfolio summary unavailable ({e}), using base risk")
        lot = calculate_lot_size(equity, config.JOB3_RISK_PCT, sl_pips, sym)
        return {
            "risk_pct": config.JOB3_RISK_PCT,
            "lot":      lot,
            "reason":   f"Portfolio data unavailable — using base {config.JOB3_RISK_PCT}% → {lot} lots",
        }

    remaining_pct = max(0.0, round(max_pct - used_pct, 4))

    # ── No debate ran — fall back to flat base risk ───────────────────────────
    if uncertainty is None:
        lot = calculate_lot_size(equity, config.JOB3_RISK_PCT, sl_pips, sym)
        return {
            "risk_pct": config.JOB3_RISK_PCT,
            "lot":      lot,
            "reason":   (
                f"No debate data — base {config.JOB3_RISK_PCT}% risk "
                f"({used_pct:.1f}% of {max_pct:.1f}% cap in use) → {lot} lots"
            ),
        }

    # ── Resolve uncertainty multiplier and conviction label ───────────────────
    multiplier   = 0.50   # default (beyond all tiers — should be vetoed)
    conviction   = "Very low"
    for threshold, mult in sorted(config.JOB3_UNCERTAINTY_TIERS, key=lambda t: t[0]):
        if uncertainty <= threshold:
            multiplier = mult
            if threshold <= 30:
                conviction = "High"
            elif threshold <= 55:
                conviction = "Medium"
            else:
                conviction = "Low"
            break

    # ── Compute final risk % ──────────────────────────────────────────────────
    allocated_pct = round(remaining_pct * multiplier, 4)
    final_pct     = round(min(allocated_pct, config.JOB3_RISK_PCT), 4)

    lot = calculate_lot_size(equity, final_pct, sl_pips, sym)

    reason = (
        f"{used_pct:.1f}% of {max_pct:.1f}% portfolio risk in use "
        f"({remaining_pct:.1f}% remaining). "
        f"{conviction} conviction (uncertainty={uncertainty}) "
        f"→ {multiplier:.0%} of remaining = {allocated_pct:.1f}%, "
        f"capped at {config.JOB3_RISK_PCT:.1f}% → {final_pct:.1f}% → {lot} lots"
    )
    logger.info(f"[{sym}] recommend_position_size: {reason}")

    return {"risk_pct": final_pct, "lot": lot, "reason": reason}


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
    sl_price: float | None = None
    tp_price: float | None = None

    # Prefer debate-adjusted SL/TP written directly onto the signal by the judge.
    # Fall back to key_levels (original LLM output) if not present.
    try:
        if signal.get("sl") is not None:
            sl_price = float(signal["sl"])
    except (TypeError, ValueError):
        pass
    try:
        if signal.get("tp") is not None:
            tp_price = float(signal["tp"])
    except (TypeError, ValueError):
        pass

    # key_levels fallback for any value not already set by the judge
    if sl_price is None or tp_price is None:
        levels = signal.get("key_levels") or {}
        support    = levels.get("support")
        resistance = levels.get("resistance")

        if direction == "buy":
            if sl_price is None and support is not None:
                try:
                    sl_price = float(support)
                except (TypeError, ValueError):
                    pass
            if tp_price is None and resistance is not None:
                try:
                    tp_price = float(resistance)
                except (TypeError, ValueError):
                    pass
        else:  # sell
            if sl_price is None and resistance is not None:
                try:
                    sl_price = float(resistance)
                except (TypeError, ValueError):
                    pass
            if tp_price is None and support is not None:
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
