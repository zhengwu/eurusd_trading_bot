"""Price agent — live price, technical indicators, and trend summary.

Data sources:
  MT5 terminal  : live tick (bid/ask) + last 48 M15 bars (~12 hours)
  yfinance      : 1-year daily history for EURUSD + correlated assets

Public API:
  get_price_summary() -> str

Output sections:
  1. Live bid/ask (MT5)
  2. Full M15 bar table — last 12 hours at 15-min resolution
  3. Daily OHLC table — last 10 trading sessions
  4. Trend analysis — short / medium / long / very-long term + SMA alignment
  5. Key levels — swing high/low + SMAs with pip distances
  6. Correlated assets — day%, week%, position vs SMA20, trend label
"""
from __future__ import annotations

from datetime import timezone
from typing import Any

import pandas as pd
import yfinance as yf

import config
from utils.logger import get_logger

logger = get_logger(__name__)

_PIP = 0.0001  # 1 pip for EURUSD


# ── maths helpers ─────────────────────────────────────────────────────────────

def _rsi(closes: pd.Series, period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    try:
        delta = closes.diff()
        gain = delta.where(delta > 0, 0.0).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
        last_loss = float(loss.iloc[-1])
        if last_loss == 0:
            return 100.0
        return round(100 - 100 / (1 + float(gain.iloc[-1]) / last_loss), 1)
    except Exception:
        return None


def _atr(df: pd.DataFrame, period: int = 14) -> float | None:
    try:
        hi = df["High"].squeeze()
        lo = df["Low"].squeeze()
        pc = df["Close"].squeeze().shift(1)
        tr = pd.concat(
            [hi - lo, (hi - pc).abs(), (lo - pc).abs()], axis=1
        ).max(axis=1)
        return round(float(tr.rolling(period).mean().iloc[-1]), 5)
    except Exception:
        return None


def _sma(closes: pd.Series, period: int) -> float | None:
    if len(closes) < period:
        return None
    return round(float(closes.rolling(period).mean().iloc[-1]), 5)


def _sma_slope(closes: pd.Series, period: int, lookback: int = 5) -> str:
    """Rising / Falling / Flat based on SMA change over last `lookback` bars."""
    if len(closes) < period + lookback:
        return "—"
    sma_now  = closes.rolling(period).mean().iloc[-1]
    sma_prev = closes.rolling(period).mean().iloc[-1 - lookback]
    diff = float(sma_now - sma_prev) / _PIP  # in pips
    if diff > 2:
        return "Rising ↑"
    if diff < -2:
        return "Falling ↓"
    return "Flat →"


def _pips(price: float, ref: float) -> str:
    diff = round((price - ref) / _PIP)
    return f"{diff:+d}p"


# ── data fetchers ─────────────────────────────────────────────────────────────

def _fetch_daily(ticker: str, period: str = "1y") -> pd.DataFrame | None:
    try:
        df = yf.download(ticker, period=period, auto_adjust=True, progress=False)
        return None if (df is None or df.empty) else df
    except Exception as e:
        logger.warning(f"yfinance {ticker}: {e}")
        return None


def _live_tick() -> dict[str, Any] | None:
    try:
        from mt5.connector import is_connected
        if not is_connected():
            return None
        from mt5.position_reader import get_current_tick
        return get_current_tick()
    except Exception:
        return None


def _m15_bars() -> pd.DataFrame | None:
    """Last 24 M15 bars (~6 hours) from MT5."""
    try:
        from mt5.connector import is_connected
        if not is_connected():
            return None
        from mt5.position_reader import get_ohlc_bars
        return get_ohlc_bars(count=24)
    except Exception:
        return None


# ── section builders ──────────────────────────────────────────────────────────

def _section_live(tick: dict | None) -> str:
    if tick:
        spread_pips = round(tick["spread"] / _PIP, 1)
        return (
            f"Live price  : Bid={tick['bid']}  Ask={tick['ask']}  "
            f"Spread={spread_pips}p  [{tick['time'][:16]} UTC]"
        )
    return "Live price  : MT5 not connected — using last daily close"


def _section_m15(bars: pd.DataFrame | None) -> str:
    if bars is None or bars.empty:
        return "Intraday M15: MT5 not connected — no bar data"

    header = (
        "Intraday M15 (last 6h — each row is one 15-min bar):\n"
        "  Time(UTC) |  Open   |  Close  | Δpips\n"
        "  " + "-" * 40
    )
    rows = [header]
    for ts, row in bars.iterrows():
        t = pd.Timestamp(ts).strftime("%H:%M")
        o, c = row["open"], row["close"]
        delta = round((c - o) / _PIP, 1)
        sign = "+" if delta >= 0 else ""
        rows.append(f"  {t}      | {o:.5f} | {c:.5f} | {sign}{delta}p")

    o0 = float(bars["open"].iloc[0])
    hi = float(bars["high"].max())
    lo = float(bars["low"].min())
    cn = float(bars["close"].iloc[-1])
    pct = (cn - o0) / o0 * 100
    rows.append(
        f"\n  6h session  : Open={o0:.5f}  High={hi:.5f}  Low={lo:.5f}  "
        f"Now={cn:.5f}  ({pct:+.2f}%)"
    )
    return "\n".join(rows)


def _section_daily(df: pd.DataFrame | None) -> str:
    if df is None or len(df) < 2:
        return "Daily OHLC  : unavailable"

    header = (
        "Daily (last 10 sessions — open & close only):\n"
        "  Date       |  Open   |  Close  |  Chg%\n"
        "  " + "-" * 44
    )
    rows = [header]
    daily = df.tail(10).copy()

    for date_idx, row in daily.iterrows():
        date_str = pd.Timestamp(date_idx).date().isoformat()
        o = float(row["Open"].item()  if hasattr(row["Open"],  "item") else row["Open"])
        c = float(row["Close"].item() if hasattr(row["Close"], "item") else row["Close"])
        abs_pos = list(df.index).index(date_idx)
        if abs_pos > 0:
            prev_c = float(df["Close"].squeeze().iloc[abs_pos - 1])
            chg_str = f"{(c - prev_c) / prev_c * 100:+.2f}%"
        else:
            chg_str = "  —  "
        rows.append(f"  {date_str} | {o:.5f} | {c:.5f} | {chg_str}")
    return "\n".join(rows)


def _section_trend(closes: pd.Series, df: pd.DataFrame, bars: pd.DataFrame | None) -> str:
    price = float(closes.iloc[-1])
    lines = ["Trend analysis:"]

    # Short-term: M15 bars (last 4h = 16 bars)
    if bars is not None and len(bars) >= 16:
        last16 = bars.tail(16)
        up_bars = int((last16["close"] > last16["open"]).sum())
        dn_bars = 16 - up_bars
        # Higher highs / lower lows in last 4h
        highs = last16["high"].values
        lows  = last16["low"].values
        hh = int(highs[-1] > highs[:8].max())  # last bar high > first-half high
        hl = int(lows[-1]  > lows[:8].min())   # last bar low  > first-half low
        if hh and hl:
            st_bias = "Bullish ↑ (higher highs & higher lows)"
        elif (not hh) and (not hl):
            st_bias = "Bearish ↓ (lower highs & lower lows)"
        elif up_bars > dn_bars:
            st_bias = f"Mild bullish ({up_bars}/{16} up bars)"
        elif dn_bars > up_bars:
            st_bias = f"Mild bearish ({dn_bars}/{16} down bars)"
        else:
            st_bias = "Neutral / ranging"
        lines.append(f"  Short-term  (4h  M15) : {st_bias}")
    else:
        lines.append("  Short-term  (4h  M15) : no M15 data")

    # Medium / long / very-long: daily SMAs
    for label, period, desc in [
        ("Medium-term", 20,  "SMA20"),
        ("Long-term  ", 50,  "SMA50"),
        ("Very long  ", 200, "SMA200"),
    ]:
        sma = _sma(closes, period)
        if sma is None:
            lines.append(f"  {label} ({period}d {desc}) : insufficient data")
            continue
        slope = _sma_slope(closes, period)
        rel   = "ABOVE ↑" if price > sma else "BELOW ↓"
        dist  = _pips(price, sma)
        lines.append(
            f"  {label} ({period}d {desc}) : price {rel} {desc} {sma} ({dist})  |  {desc} {slope}"
        )

    # SMA alignment
    sma20  = _sma(closes, 20)
    sma50  = _sma(closes, 50)
    sma200 = _sma(closes, 200)
    if sma20 and sma50 and sma200:
        if sma20 > sma50 > sma200:
            alignment = "20 > 50 > 200 — fully bullish aligned"
        elif sma20 < sma50 < sma200:
            alignment = "20 < 50 < 200 — fully bearish aligned"
        else:
            alignment = "SMAs crossed / mixed"
        lines.append(f"  SMA alignment         : {alignment}")

    rsi = _rsi(closes)
    atr = _atr(df)
    if rsi is not None:
        rsi_label = (
            "Overbought ⚠" if rsi >= 70 else
            "Oversold ⚠"  if rsi <= 30 else
            "Bullish"      if rsi >= 55 else
            "Bearish"      if rsi <= 45 else
            "Neutral"
        )
        lines.append(f"  RSI 14 (daily)        : {rsi}  — {rsi_label}")
    if atr is not None:
        lines.append(f"  ATR 14 (daily)        : {atr:.5f}  (~{round(atr/_PIP)}p avg daily range)")

    return "\n".join(lines)


def _section_key_levels(closes: pd.Series, df: pd.DataFrame, price: float) -> str:
    lines = ["Key levels:"]

    # 20-day swing
    recent20 = df.tail(20)
    swing_high = float(recent20["High"].squeeze().max())
    swing_low  = float(recent20["Low"].squeeze().min())
    sh_date = pd.Timestamp(recent20["High"].squeeze().idxmax()).date().isoformat()
    sl_date = pd.Timestamp(recent20["Low"].squeeze().idxmin()).date().isoformat()
    lines.append(f"  20-day high : {swing_high:.5f}  ({sh_date})  {_pips(price, swing_high)} from current")
    lines.append(f"  20-day low  : {swing_low:.5f}  ({sl_date})  {_pips(price, swing_low)} from current")

    # SMAs as dynamic support/resistance
    for period, label in [(20, "SMA20"), (50, "SMA50"), (200, "SMA200")]:
        sma = _sma(closes, period)
        if sma:
            role = "support" if price > sma else "resistance"
            lines.append(f"  {label:<7}     : {sma:.5f}  ({role})  {_pips(price, sma)} from current")

    # Previous session high/low (last completed daily bar)
    if len(df) >= 2:
        prev = df.iloc[-2]
        ph = float(prev["High"].item() if hasattr(prev["High"], "item") else prev["High"])
        pl = float(prev["Low"].item()  if hasattr(prev["Low"],  "item") else prev["Low"])
        lines.append(f"  Prev day H  : {ph:.5f}  {_pips(price, ph)} from current")
        lines.append(f"  Prev day L  : {pl:.5f}  {_pips(price, pl)} from current")

    return "\n".join(lines)


def _section_correlated(price_eurusd: float) -> str:
    assets = [
        ("DXY",    "DX=F",  "",  "Inverse — DXY up = EUR/USD down"),
        ("Gold",   "GC=F",  "",  "Risk-on proxy — Gold up = mild EUR/USD support"),
        ("US10Y",  "^TNX",  "%", "Yield — rising = USD strength pressure"),
        ("VIX",    "^VIX",  "",  "Fear index — VIX up = risk-off, USD bid"),
        ("S&P500", "^GSPC", "",  "Risk appetite — S&P up = mild risk-on"),
    ]

    header = (
        "Correlated assets:\n"
        "  Asset   |  Last    |  Day%  | Week%  | vs SMA20 | 5d Trend  | Note\n"
        "  " + "-" * 80
    )
    rows = [header]

    for name, ticker, unit, note in assets:
        df = _fetch_daily(ticker, period="3mo")
        if df is None or len(df) < 6:
            rows.append(f"  {name:<7} | unavailable")
            continue

        closes = df["Close"].squeeze().dropna()
        last   = float(closes.iloc[-1])
        prev   = float(closes.iloc[-2])
        day_chg = (last - prev) / prev * 100

        # Week % (5 trading days ago)
        wk_prev  = float(closes.iloc[-6]) if len(closes) >= 6 else prev
        wk_chg   = (last - wk_prev) / wk_prev * 100

        # vs SMA20
        sma20 = _sma(closes, 20)
        vs_sma = "—"
        if sma20:
            vs_sma = "ABOVE ↑" if last > sma20 else "BELOW ↓"

        # 5-day price slope
        slope_pct = (last - wk_prev) / wk_prev * 100
        trend_5d = (
            "Bullish ↑"  if slope_pct >  0.5 else
            "Bearish ↓"  if slope_pct < -0.5 else
            "Flat →"
        )

        rows.append(
            f"  {name:<7} | {last:8.2f}{unit} | {day_chg:+5.2f}% | {wk_chg:+5.2f}% "
            f"| {vs_sma:<8} | {trend_5d:<10}| {note}"
        )

    return "\n".join(rows)


# ── public API ────────────────────────────────────────────────────────────────

def get_price_summary() -> str:
    """
    Build a detailed EUR/USD price summary for use in analysis prompts.
    Falls back gracefully if MT5 is disconnected or yfinance is unavailable.
    """
    parts: list[str] = ["=== EURUSD PRICE SUMMARY ===\n"]

    # Live tick
    tick = _live_tick()
    parts.append(_section_live(tick))

    # M15 bars
    bars = _m15_bars()
    parts.append("\n" + _section_m15(bars))

    # Daily data (backbone for all technical sections)
    df = _fetch_daily("EURUSD=X", period="1y")
    if df is not None and len(df) >= 20:
        closes = df["Close"].squeeze().dropna()
        price  = float(closes.iloc[-1])

        parts.append("\n" + _section_daily(df))
        parts.append("\n" + _section_trend(closes, df, bars))
        parts.append("\n" + _section_key_levels(closes, df, price))
        parts.append("\n" + _section_correlated(price))
    else:
        parts.append("\n(Insufficient daily history — yfinance unavailable)")

    return "\n".join(parts)
