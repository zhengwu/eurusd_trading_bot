"""Market regime engine - deterministic, indicator-driven regime classification.

Improvements over v1:
  1. ADX replaced with SMA stack (daily) + M15 bar momentum - more reliable in forex
  2. Fuzzy boundary detection - flags readings near regime thresholds
  3. Macro Bias staleness check - warns if config.REGIME_MACRO_BIAS_UPDATED is stale
  4. Regime conflict detection - surfaces contradictions with recommended action
  5. Pair-aware risk context - supplements VIX with pair realized volatility from M15 bars

Regimes:
  Risk Sentiment  - VIX level + pair realized vol
  Technical Trend - SMA20/50/200 stack alignment + M15 momentum
  Volatility      - ATR-14 vs ATR-30 ratio
  Macro Bias      - string from config (central bank stance / yield spread)
"""
from __future__ import annotations

from datetime import date

from utils.logger import get_logger

logger = get_logger(__name__)

# Boundary buffer: flag as "transitioning" when within this % of a threshold
_BOUNDARY_PCT = 0.10


def _near(value: float, threshold: float) -> bool:
    """True if value is within _BOUNDARY_PCT of threshold."""
    return abs(value - threshold) / max(abs(threshold), 1e-9) < _BOUNDARY_PCT


def get_market_regimes(
    vix:                   float | None = None,
    price:                 float | None = None,
    sma20:                 float | None = None,
    sma50:                 float | None = None,
    sma200:                float | None = None,
    atr_current:           float | None = None,
    atr_30d_avg:           float | None = None,
    m15_up_bars:           int   | None = None,
    m15_total_bars:        int          = 16,
    pair_realized_vol_pct: float | None = None,
    macro_bias:            str          = "Neutral",
    macro_bias_days_old:   int   | None = None,
) -> str:
    """
    Classify market conditions into four regimes and return a plain-English summary.

    Parameters
    ----------
    vix                   : Latest VIX close
    price                 : Current asset price
    sma20/sma50/sma200    : Daily SMAs
    atr_current           : ATR-14 (recent volatility)
    atr_30d_avg           : ATR-30 (30-day baseline volatility)
    m15_up_bars           : Count of bullish M15 bars in last 16 (4h)
    m15_total_bars        : Total M15 bars in the window (default 16)
    pair_realized_vol_pct : Annualized realized vol from M15 returns (%)
    macro_bias            : Central bank stance string (from config)
    macro_bias_days_old   : Days since macro_bias was last updated
    """
    lines = ["=== CURRENT MARKET REGIME ==="]

    # track high-level labels for conflict detection
    risk_label  = "neutral"   # "risk_off" | "neutral" | "risk_on"
    trend_label = "neutral"   # "bull" | "neutral" | "bear"
    vol_label   = "normal"    # "high" | "normal" | "low"

    # ── 1. Risk Sentiment ─────────────────────────────────────────────────────
    if vix is not None:
        near_off = _near(vix, 25)
        near_on  = _near(vix, 15)

        if vix > 25:
            risk_label = "risk_off"
            note = "Elevated fear - safe-haven bid active, widen stops, avoid aggressive longs"
            flag = "  [!] Approaching Risk-On boundary" if near_off else ""
            risk_str = f"Risk-Off  [VIX={vix:.1f} > 25]  {note}{flag}"
        elif vix < 15:
            risk_label = "risk_on"
            note = "Low fear / complacency - carry trades and risk assets favoured"
            flag = "  [!] Approaching Risk-Off boundary" if near_on else ""
            risk_str = f"Risk-On   [VIX={vix:.1f} < 15]  {note}{flag}"
        else:
            if near_off:
                risk_str = f"Neutral->Risk-Off  [VIX={vix:.1f}, approaching 25]  Caution - fear creeping in"
            elif near_on:
                risk_str = f"Neutral->Risk-On   [VIX={vix:.1f}, approaching 15]  Complacency building"
            else:
                risk_str = f"Neutral  [VIX={vix:.1f}]  No strong risk signal - let technicals lead"

        # Supplement with pair realized vol if available
        if pair_realized_vol_pct is not None:
            risk_str += f"  |  Pair realized vol: {pair_realized_vol_pct:.1f}% ann."
    else:
        risk_str = "Unknown  (VIX data unavailable)"

    lines.append(f"  Risk Sentiment  : {risk_str}")

    # ── 2. Technical Trend ────────────────────────────────────────────────────
    sma_bias = "neutral"
    sma_desc = "unavailable"

    if price is not None and sma50 is not None and sma200 is not None:
        if sma20 is not None:
            if price > sma20 > sma50 > sma200:
                sma_bias = "bull"
                sma_desc = f"Fully Bullish stack (price > SMA20={sma20:.5f} > SMA50={sma50:.5f} > SMA200={sma200:.5f})"
            elif price < sma20 < sma50 < sma200:
                sma_bias = "bear"
                sma_desc = f"Fully Bearish stack (price < SMA20={sma20:.5f} < SMA50={sma50:.5f} < SMA200={sma200:.5f})"
            elif price > sma50 > sma200:
                sma_bias = "bull"
                sma_desc = f"Bullish (price > SMA50/200; SMA20={sma20:.5f} - minor pullback in uptrend)"
            elif price < sma50 < sma200:
                sma_bias = "bear"
                sma_desc = f"Bearish (price < SMA50/200; SMA20={sma20:.5f} - minor bounce in downtrend)"
            elif price > sma200:
                sma_desc = f"Mixed (above SMA200={sma200:.5f} but below shorter MAs - pullback zone)"
            else:
                sma_desc = f"Mixed (below SMA200={sma200:.5f}, crossed shorter MAs - bounce zone)"
        else:
            if price > sma50 > sma200:
                sma_bias = "bull"
                sma_desc = f"Bullish (price > SMA50={sma50:.5f} > SMA200={sma200:.5f})"
            elif price < sma50 < sma200:
                sma_bias = "bear"
                sma_desc = f"Bearish (price < SMA50={sma50:.5f} < SMA200={sma200:.5f})"
            else:
                sma_desc = f"Mixed SMA stack (SMA50={sma50:.5f}, SMA200={sma200:.5f})"

    # M15 momentum
    m15_bias = "neutral"
    m15_desc = "unavailable (MT5 not connected)"
    if m15_up_bars is not None and m15_total_bars > 0:
        ratio = m15_up_bars / m15_total_bars
        pct   = round(ratio * 100)
        if ratio >= 0.65:
            m15_bias = "bull"
            m15_desc = f"Bullish momentum ({pct}% up bars in last {m15_total_bars} M15 bars)"
        elif ratio <= 0.35:
            m15_bias = "bear"
            m15_desc = f"Bearish momentum ({pct}% up bars in last {m15_total_bars} M15 bars)"
        else:
            m15_desc = f"Neutral momentum ({pct}% up bars in last {m15_total_bars} M15 bars)"

    # Combined trend
    if sma_bias == "bull" and m15_bias == "bull":
        trend_label = "bull"
        trend_str = f"Strong Bullish - daily SMA stack and M15 momentum aligned\n    Daily: {sma_desc}\n    M15  : {m15_desc}"
    elif sma_bias == "bear" and m15_bias == "bear":
        trend_label = "bear"
        trend_str = f"Strong Bearish - daily SMA stack and M15 momentum aligned\n    Daily: {sma_desc}\n    M15  : {m15_desc}"
    elif sma_bias == "bull" and m15_bias == "bear":
        trend_str = f"Pullback in Uptrend - daily bullish but M15 weakening (wait for bounce)\n    Daily: {sma_desc}\n    M15  : {m15_desc}"
    elif sma_bias == "bear" and m15_bias == "bull":
        trend_str = f"Bounce in Downtrend - daily bearish but M15 recovering (fade the bounce)\n    Daily: {sma_desc}\n    M15  : {m15_desc}"
    elif sma_bias == "neutral":
        trend_str = f"Consolidating/Transitioning - SMA stack mixed\n    Daily: {sma_desc}\n    M15  : {m15_desc}"
    else:
        trend_str = f"Choppy - SMA bias present but M15 neutral\n    Daily: {sma_desc}\n    M15  : {m15_desc}"

    lines.append(f"  Technical Trend : {trend_str}")

    # ── 3. Volatility ─────────────────────────────────────────────────────────
    if atr_current is not None and atr_30d_avg is not None and atr_30d_avg > 0:
        ratio = atr_current / atr_30d_avg
        near_high = _near(ratio, 1.2)
        near_low  = _near(ratio, 0.8)

        if ratio > 1.2:
            vol_label = "high"
            vol_str = (
                f"High  [ATR-14={atr_current:.5f}, {ratio:.2f}x above 30d avg={atr_30d_avg:.5f}]  "
                "Widen stops well beyond key levels - reduce size to compensate"
            )
        elif ratio < 0.8:
            vol_label = "low"
            vol_str = (
                f"Low   [ATR-14={atr_current:.5f}, {ratio:.2f}x below 30d avg={atr_30d_avg:.5f}]  "
                "Compression - breakout risk elevated, avoid ultra-tight stops"
            )
        else:
            suffix = "  [!] Approaching High-Vol threshold" if near_high else (
                     "  [!] Approaching Low-Vol threshold"  if near_low  else "")
            vol_str = (
                f"Normal [ATR-14={atr_current:.5f}, {ratio:.2f}x of 30d avg={atr_30d_avg:.5f}]  "
                f"Standard stop distances (1.0-1.5x ATR beyond key level){suffix}"
            )
    else:
        vol_str = "Unknown  (ATR data unavailable)"

    lines.append(f"  Volatility      : {vol_str}")

    # ── 4. Macro Bias ─────────────────────────────────────────────────────────
    if macro_bias_days_old is not None and macro_bias_days_old > 14:
        macro_str = f"{macro_bias}  [!] STALE ({macro_bias_days_old}d old) - update config.REGIME_MACRO_BIAS before trusting this"
    elif macro_bias_days_old is not None and macro_bias_days_old > 7:
        macro_str = f"{macro_bias}  (updated {macro_bias_days_old}d ago - verify still current)"
    elif macro_bias_days_old is not None:
        macro_str = f"{macro_bias}  (updated {macro_bias_days_old}d ago)"
    else:
        macro_str = macro_bias

    lines.append(f"  Macro Bias      : {macro_str}")

    # ── 5. Conflict Detection ─────────────────────────────────────────────────
    conflicts: list[str] = []

    if risk_label == "risk_off" and trend_label == "bull":
        conflicts.append(
            "Risk-Off sentiment vs Bullish trend - safe-haven demand contradicts technical setup. "
            "Prefer Wait or cut size by 50%."
        )
    if risk_label == "risk_on" and trend_label == "bear":
        conflicts.append(
            "Risk-On sentiment vs Bearish trend - complacency contradicts technical breakdown. "
            "Be cautious of counter-trend longs."
        )
    if vol_label == "high" and trend_label in ("bull", "bear"):
        conflicts.append(
            "High volatility during a trending market - stops set at normal ATR distances "
            "will be swept. Use 1.5-2.0x ATR padding minimum."
        )

    if conflicts:
        lines.append("  [!] Regime Conflicts:")
        for c in conflicts:
            lines.append(f"    - {c}")

    return "\n".join(lines)
