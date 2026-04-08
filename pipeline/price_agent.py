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


def _get_pip(symbol: str) -> float:
    return config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["pip"]


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


def _adx(df: pd.DataFrame, period: int = 14) -> float | None:
    """Compute Average Directional Index (ADX-14)."""
    try:
        hi = df["High"].squeeze()
        lo = df["Low"].squeeze()
        pc = df["Close"].squeeze().shift(1)
        tr       = pd.concat([(hi - lo), (hi - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
        plus_dm  = hi.diff().clip(lower=0)
        minus_dm = (-lo.diff()).clip(lower=0)
        # Only count the dominant direction
        plus_dm  = plus_dm.where(plus_dm > minus_dm, 0.0)
        minus_dm = minus_dm.where(minus_dm > plus_dm, 0.0)
        atr_s    = tr.ewm(alpha=1 / period, adjust=False).mean()
        plus_di  = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_s
        minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_s
        dx  = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-9)
        adx = dx.ewm(alpha=1 / period, adjust=False).mean()
        return round(float(adx.iloc[-1]), 1)
    except Exception:
        return None


def _sma_slope(closes: pd.Series, period: int, pip: float, lookback: int = 5) -> str:
    """Rising / Falling / Flat based on SMA change over last `lookback` bars."""
    if len(closes) < period + lookback:
        return "—"
    sma_now  = closes.rolling(period).mean().iloc[-1]
    sma_prev = closes.rolling(period).mean().iloc[-1 - lookback]
    diff = float(sma_now - sma_prev) / pip  # in pips
    if diff > 2:
        return "Rising ↑"
    if diff < -2:
        return "Falling ↓"
    return "Flat →"


def _pips(price: float, ref: float, pip: float) -> str:
    diff = round((price - ref) / pip)
    return f"{diff:+d}p"


def _ema(closes: pd.Series, period: int) -> float | None:
    if len(closes) < period:
        return None
    return round(float(closes.ewm(span=period, adjust=False).mean().iloc[-1]), 5)


def _macd(closes: pd.Series) -> tuple[float, float, float] | None:
    """Returns (macd_line, signal_line, histogram). Standard 12/26/9 settings."""
    if len(closes) < 35:
        return None
    try:
        ema12   = closes.ewm(span=12, adjust=False).mean()
        ema26   = closes.ewm(span=26, adjust=False).mean()
        line    = ema12 - ema26
        signal  = line.ewm(span=9, adjust=False).mean()
        hist    = line - signal
        return (
            round(float(line.iloc[-1]),   6),
            round(float(signal.iloc[-1]), 6),
            round(float(hist.iloc[-1]),   6),
        )
    except Exception:
        return None


def _bollinger(closes: pd.Series, period: int = 20, num_std: float = 2.0) -> tuple[float, float, float] | None:
    """Returns (upper, mid, lower) for the current bar."""
    if len(closes) < period:
        return None
    try:
        mid   = closes.rolling(period).mean()
        std   = closes.rolling(period).std()
        upper = mid + num_std * std
        lower = mid - num_std * std
        return (
            round(float(upper.iloc[-1]), 5),
            round(float(mid.iloc[-1]),   5),
            round(float(lower.iloc[-1]), 5),
        )
    except Exception:
        return None


# ── data fetchers ─────────────────────────────────────────────────────────────

def _fetch_daily(ticker: str, period: str = "1y") -> pd.DataFrame | None:
    try:
        df = yf.download(ticker, period=period, auto_adjust=True, progress=False)
        return None if (df is None or df.empty) else df
    except Exception as e:
        logger.warning(f"yfinance {ticker}: {e}")
        return None


def _live_tick(symbol: str) -> dict[str, Any] | None:
    try:
        from mt5.connector import is_connected
        if not is_connected():
            return None
        from mt5.position_reader import get_current_tick
        return get_current_tick(symbol=symbol)
    except Exception:
        return None


def _m15_bars(symbol: str) -> pd.DataFrame | None:
    """Last 24 M15 bars (~6 hours) from MT5."""
    try:
        from mt5.connector import is_connected
        if not is_connected():
            return None
        from mt5.position_reader import get_ohlc_bars
        return get_ohlc_bars(symbol=symbol, count=24)
    except Exception:
        return None


def _h4_bars(symbol: str) -> pd.DataFrame | None:
    """Last 100 H4 bars (~17 months) from MT5."""
    try:
        from mt5.connector import is_connected
        if not is_connected():
            return None
        from mt5.position_reader import get_ohlc_bars
        return get_ohlc_bars(symbol=symbol, timeframe_str="H4", count=100)
    except Exception:
        return None


# ── section builders ──────────────────────────────────────────────────────────

def _section_live(tick: dict | None, pip: float) -> str:
    if tick:
        spread_pips = round(tick["spread"] / pip, 1)
        return (
            f"Live price  : Bid={tick['bid']}  Ask={tick['ask']}  "
            f"Spread={spread_pips}p  [{tick['time'][:16]} UTC]"
        )
    return "Live price  : MT5 not connected — using last daily close"


def _section_m15(bars: pd.DataFrame | None, pip: float, decimals: int = 5) -> str:
    if bars is None or bars.empty:
        return "Intraday M15: MT5 not connected — no bar data"

    fmt = f".{decimals}f"
    header = (
        "Intraday M15 (last 6h — each row is one 15-min bar):\n"
        "  Time(UTC) |  Open   |  Close  | Δpips\n"
        "  " + "-" * 40
    )
    rows = [header]
    for ts, row in bars.iterrows():
        t = pd.Timestamp(ts).strftime("%H:%M")
        o, c = row["open"], row["close"]
        delta = round((c - o) / pip, 1)
        sign = "+" if delta >= 0 else ""
        rows.append(f"  {t}      | {o:{fmt}} | {c:{fmt}} | {sign}{delta}p")

    o0 = float(bars["open"].iloc[0])
    hi = float(bars["high"].max())
    lo = float(bars["low"].min())
    cn = float(bars["close"].iloc[-1])
    pct = (cn - o0) / o0 * 100
    rows.append(
        f"\n  6h session  : Open={o0:{fmt}}  High={hi:{fmt}}  Low={lo:{fmt}}  "
        f"Now={cn:{fmt}}  ({pct:+.2f}%)"
    )
    return "\n".join(rows)


def _section_daily(df: pd.DataFrame | None, decimals: int = 5) -> str:
    if df is None or len(df) < 2:
        return "Daily OHLC  : unavailable"

    fmt = f".{decimals}f"
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
        rows.append(f"  {date_str} | {o:{fmt}} | {c:{fmt}} | {chg_str}")
    return "\n".join(rows)


def _section_trend(closes: pd.Series, df: pd.DataFrame, bars: pd.DataFrame | None, pip: float) -> str:
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
        slope = _sma_slope(closes, period, pip)
        rel   = "ABOVE ↑" if price > sma else "BELOW ↓"
        dist  = _pips(price, sma, pip)
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
        lines.append(f"  ATR 14 (daily)        : {atr:.5f}  (~{round(atr/pip)}p avg daily range)")

    return "\n".join(lines)


def _section_key_levels(closes: pd.Series, df: pd.DataFrame, price: float, pip: float, decimals: int = 5) -> str:
    fmt = f".{decimals}f"
    lines = ["Key levels:"]

    # 20-day swing
    recent20 = df.tail(20)
    swing_high = float(recent20["High"].squeeze().max())
    swing_low  = float(recent20["Low"].squeeze().min())
    sh_date = pd.Timestamp(recent20["High"].squeeze().idxmax()).date().isoformat()
    sl_date = pd.Timestamp(recent20["Low"].squeeze().idxmin()).date().isoformat()
    lines.append(f"  20-day high : {swing_high:{fmt}}  ({sh_date})  {_pips(price, swing_high, pip)} from current")
    lines.append(f"  20-day low  : {swing_low:{fmt}}  ({sl_date})  {_pips(price, swing_low, pip)} from current")

    # SMAs as dynamic support/resistance
    for period, label in [(20, "SMA20"), (50, "SMA50"), (200, "SMA200")]:
        sma = _sma(closes, period)
        if sma:
            role = "support" if price > sma else "resistance"
            lines.append(f"  {label:<7}     : {sma:{fmt}}  ({role})  {_pips(price, sma, pip)} from current")

    # Previous session high/low (last completed daily bar)
    if len(df) >= 2:
        prev = df.iloc[-2]
        ph = float(prev["High"].item() if hasattr(prev["High"], "item") else prev["High"])
        pl = float(prev["Low"].item()  if hasattr(prev["Low"],  "item") else prev["Low"])
        lines.append(f"  Prev day H  : {ph:{fmt}}  {_pips(price, ph, pip)} from current")
        lines.append(f"  Prev day L  : {pl:{fmt}}  {_pips(price, pl, pip)} from current")

    return "\n".join(lines)


def _section_correlated(symbol: str) -> str:
    pair = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])
    assets = pair["correlations"]

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


# ── H4 / indicator / MTF helpers ─────────────────────────────────────────────

def _m15_direction(bars: pd.DataFrame | None) -> str:
    """Derive M15 bias string from bar structure."""
    if bars is None or len(bars) < 16:
        return "Unknown"
    last16 = bars.tail(16)
    highs = last16["high"].values
    lows  = last16["low"].values
    hh = highs[-1] > highs[:8].max()
    hl = lows[-1]  > lows[:8].min()
    if hh and hl:
        return "Bullish"
    elif (not hh) and (not hl):
        return "Bearish"
    up_bars = int((last16["close"] > last16["open"]).sum())
    if up_bars > 10:
        return "Bullish"
    if up_bars < 6:
        return "Bearish"
    return "Mixed"


def _section_h4(h4_bars: pd.DataFrame | None, pip: float, decimals: int = 5) -> tuple[str, str]:
    """
    Build H4 technical section.
    Returns (formatted_text, direction) where direction is Bullish/Bearish/Mixed/Unknown.
    """
    if h4_bars is None or len(h4_bars) < 55:
        return "H4 timeframe   : MT5 not connected — no H4 data", "Unknown"

    fmt      = f".{decimals}f"
    h4_close = h4_bars["close"]
    price    = float(h4_close.iloc[-1])

    rsi_h4   = _rsi(h4_close, 14)
    atr_h4   = _atr(h4_bars.rename(columns={"open": "Open", "high": "High",
                                             "low": "Low", "close": "Close"}), 14)
    ema20_h4 = _ema(h4_close, 20)
    ema50_h4 = _ema(h4_close, 50)

    lines = ["H4 timeframe (last 100 bars):"]

    if ema20_h4:
        rel20 = "ABOVE ↑" if price > ema20_h4 else "BELOW ↓"
        dist20 = _pips(price, ema20_h4, pip)
        lines.append(f"  EMA 20 (H4)  : {ema20_h4:{fmt}}  (price {rel20}  {dist20})")
    if ema50_h4:
        rel50 = "ABOVE ↑" if price > ema50_h4 else "BELOW ↓"
        dist50 = _pips(price, ema50_h4, pip)
        lines.append(f"  EMA 50 (H4)  : {ema50_h4:{fmt}}  (price {rel50}  {dist50})")
    if rsi_h4 is not None:
        rsi_lbl = (
            "Overbought ⚠" if rsi_h4 >= 70 else
            "Oversold ⚠"  if rsi_h4 <= 30 else
            "Bullish"      if rsi_h4 >= 55 else
            "Bearish"      if rsi_h4 <= 45 else
            "Neutral"
        )
        lines.append(f"  RSI 14 (H4)  : {rsi_h4}  — {rsi_lbl}")
    if atr_h4 is not None:
        lines.append(f"  ATR 14 (H4)  : {atr_h4:.5f}  (~{round(atr_h4/pip)}p avg H4 range)")

    # H4 direction
    if ema20_h4 and ema50_h4:
        if price > ema20_h4 and ema20_h4 > ema50_h4:
            direction = "Bullish"
            bias_str  = f"Bullish ↑ (price > EMA20 > EMA50"
        elif price < ema20_h4 and ema20_h4 < ema50_h4:
            direction = "Bearish"
            bias_str  = f"Bearish ↓ (price < EMA20 < EMA50"
        elif price > ema50_h4:
            direction = "Mixed"
            bias_str  = f"Mixed (price above EMA50 but EMA20/50 crossed"
        else:
            direction = "Mixed"
            bias_str  = f"Mixed (price below EMA50 but EMA20/50 crossed"
        rsi_part = f", RSI {rsi_h4})" if rsi_h4 else ")"
        lines.append(f"  H4 bias      : {bias_str}{rsi_part}")
    else:
        direction = "Unknown"

    return "\n".join(lines), direction


def _section_indicators_d1(closes: pd.Series, df: pd.DataFrame, pip: float, decimals: int = 5) -> str:
    """D1 MACD, Bollinger Bands, and ADX section."""
    fmt   = f".{decimals}f"
    price = float(closes.iloc[-1])
    lines = ["D1 technical indicators:"]

    # MACD
    macd_vals = _macd(closes)
    if macd_vals:
        ml, sl, hl = macd_vals
        cross_lbl = (
            "Bullish crossover ↑" if ml > sl and hl > 0 else
            "Bearish crossover ↓" if ml < sl and hl < 0 else
            "Bullish (line > signal)" if ml > sl else
            "Bearish (line < signal)"
        )
        lines.append(
            f"  MACD (12,26,9): Line={ml:+.5f}  Signal={sl:+.5f}  Hist={hl:+.5f}"
            f"  → {cross_lbl}"
        )

    # Bollinger Bands
    bb = _bollinger(closes, period=20, num_std=2.0)
    if bb:
        upper, mid, lower = bb
        bb_width = round(upper - lower, 5)
        if upper > lower:
            pct_pos = round((price - lower) / (upper - lower) * 100, 1)
            if pct_pos >= 80:
                pos_lbl = f"{pct_pos}% — near upper band (extended)"
            elif pct_pos <= 20:
                pos_lbl = f"{pct_pos}% — near lower band (extended)"
            else:
                pos_lbl = f"{pct_pos}% of band range"
        else:
            pos_lbl = "—"
        lines.append(
            f"  BB (20,2)     : Upper={upper:{fmt}}  Mid={mid:{fmt}}  Lower={lower:{fmt}}"
        )
        lines.append(
            f"  BB position   : {pos_lbl}  |  Width={bb_width:{fmt}} (~{round(bb_width/pip)}p)"
        )

    # ADX
    adx_val = _adx(df)
    if adx_val is not None:
        adx_lbl = (
            "strong trend (>40)"  if adx_val > 40 else
            "trend developing"    if adx_val > 25 else
            "weak / ranging"
        )
        lines.append(f"  ADX 14 (D1)  : {adx_val}  — {adx_lbl}")

    return "\n".join(lines)


def _section_mtf_confluence(
    d1_dir: str,
    h4_dir: str,
    m15_dir: str,
    d1_adx: float | None = None,
) -> str:
    """Multi-timeframe confluence summary."""
    dirs = [d1_dir, h4_dir, m15_dir]
    bullish_count = dirs.count("Bullish")
    bearish_count = dirs.count("Bearish")

    if bullish_count == 3:
        verdict = "BULLISH ALIGNED — all 3 timeframes agree → elevated confidence long bias"
    elif bearish_count == 3:
        verdict = "BEARISH ALIGNED — all 3 timeframes agree → elevated confidence short bias"
    elif bullish_count == 2:
        verdict = "BULLISH LEANING — 2 of 3 timeframes agree → moderate long bias"
    elif bearish_count == 2:
        verdict = "BEARISH LEANING — 2 of 3 timeframes agree → moderate short bias"
    else:
        verdict = "CONFLICTED — timeframes disagree → wait for alignment or reduce size"

    adx_note = ""
    if d1_adx is not None:
        if d1_adx > 40:
            adx_note = f"  ADX {d1_adx} confirms strong trending conditions"
        elif d1_adx < 20:
            adx_note = f"  ADX {d1_adx} — weak trend; range conditions possible"

    lines = [
        "Multi-timeframe confluence:",
        f"  D1 trend      : {d1_dir}",
        f"  H4 trend      : {h4_dir}",
        f"  M15 trend     : {m15_dir}",
        f"  ── {verdict}",
    ]
    if adx_note:
        lines.append(adx_note)
    return "\n".join(lines)


# ── yield spread ──────────────────────────────────────────────────────────────
#
# Data sources (all free, no API key):
#   US10Y  : yfinance ^TNX        (reliable daily)
#   DE10Y  : ECB Data Portal      (B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y)
#   UK10Y  : Bank of England API  (series IUDMNPY)
#   JP10Y  : Japan Ministry of Finance CSV (column 10 of jgbcm.csv)
#
# Note: FRED (fredgraph.json) is not used — it times out on this machine.

_YIELD_NOTE: dict[str, tuple[str, str, str]] = {
    "EURUSD": ("US10Y", "DE10Y", "Positive spread = USD yield premium -> bearish EUR/USD"),
    "GBPUSD": ("US10Y", "UK10Y", "Negative spread = GBP yield premium -> bullish GBP/USD"),
    "USDJPY": ("US10Y", "JP10Y", "Positive spread = USD yield premium -> bullish USD/JPY carry"),
}


def _fetch_yield_yfinance(ticker: str, n: int = 25) -> list[float] | None:
    """Last n daily closes from yfinance (used for US10Y via ^TNX)."""
    df = _fetch_daily(ticker, period="3mo")
    if df is None or df.empty:
        return None
    vals = [round(float(v), 3) for v in df["Close"].squeeze().dropna().tail(n)]
    return vals if len(vals) >= 2 else None


def _fetch_yield_ecb(n: int = 25) -> list[float] | None:
    """Last n daily DE10Y observations from the ECB Data Portal (no auth required)."""
    import requests
    try:
        r = requests.get(
            "https://data-api.ecb.europa.eu/service/data/YC/"
            "B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y",
            params={"format": "jsondata", "lastNObservations": str(n)},
            timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        obs = list(d["dataSets"][0]["series"].values())[0]["observations"]
        vals = [round(v[0], 3) for v in obs.values() if v and v[0] is not None]
        return vals[-n:] if len(vals) >= 2 else None
    except Exception as e:
        logger.debug(f"ECB DE10Y fetch failed: {e}")
        return None


def _fetch_yield_boe(n: int = 25) -> list[float] | None:
    """Last n daily UK 10Y gilt yield from the Bank of England API."""
    import requests
    from datetime import datetime, timedelta
    try:
        start = (datetime.now() - timedelta(days=50)).strftime("%d/%b/%Y")
        r = requests.get(
            "https://www.bankofengland.co.uk/boeapps/database/"
            "_iadb-FromShowColumns.asp",
            params={
                "csv.x": "yes", "Datefrom": start, "Dateto": "now",
                "SeriesCodes": "IUDMNPY", "CSVF": "TT", "UsingCodes": "Y",
            },
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        r.raise_for_status()
        vals = []
        for line in r.text.strip().split("\n"):
            parts = line.strip().replace("\r", "").split(",")
            if len(parts) >= 2:
                try:
                    vals.append(round(float(parts[1]), 3))
                except ValueError:
                    pass
        return vals[-n:] if len(vals) >= 2 else None
    except Exception as e:
        logger.debug(f"BOE UK10Y fetch failed: {e}")
        return None


def _fetch_yield_mof_jp(n: int = 25) -> list[float] | None:
    """
    Last n daily Japan 10Y JGB yield from Japan Ministry of Finance CSV.
    Dates use Reiwa imperial era (R8.3.24 = March 24, 2026).
    Column 10 (0-indexed) is the 10-year yield.
    """
    import requests
    try:
        r = requests.get(
            "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        r.raise_for_status()
        lines = r.content.decode("latin-1").strip().split("\n")
        vals = []
        for line in lines[2:]:  # Skip 2 header rows
            parts = line.strip().replace("\r", "").split(",")
            if len(parts) > 10 and parts[0].startswith("R"):
                try:
                    vals.append(round(float(parts[10]), 3))
                except (ValueError, IndexError):
                    pass
        return vals[-n:] if len(vals) >= 2 else None
    except Exception as e:
        logger.debug(f"MOF JP10Y fetch failed: {e}")
        return None


def _get_pair_yields(symbol: str, n: int = 25) -> tuple[
    list[float] | None, list[float] | None, str, str
]:
    """
    Fetch n daily 10Y yield observations for the USD leg and paired-currency leg.
    Returns (us_yields, fx_yields, us_name, fx_name).
    """
    us_vals = _fetch_yield_yfinance("^TNX", n=n)
    if symbol == "EURUSD":
        return us_vals, _fetch_yield_ecb(n=n),     "US10Y", "DE10Y"
    elif symbol == "GBPUSD":
        return us_vals, _fetch_yield_boe(n=n),     "US10Y", "UK10Y"
    elif symbol == "USDJPY":
        return us_vals, _fetch_yield_mof_jp(n=n),  "US10Y", "JP10Y"
    return us_vals, None, "US10Y", "?"


def _section_yield_spread(symbol: str) -> str:
    """10Y government bond yield spread section for the price summary."""
    cfg = _YIELD_NOTE.get(symbol)
    if not cfg:
        return ""

    name_a, name_b, note = cfg
    us_vals, fx_vals, _, _ = _get_pair_yields(symbol, n=25)
    if not us_vals or not fx_vals:
        logger.debug(f"Yield spread unavailable for {symbol}")
        return ""

    # Align to same length
    n       = min(len(us_vals), len(fx_vals))
    us_vals = us_vals[-n:]
    fx_vals = fx_vals[-n:]
    spread  = [round(a - b, 3) for a, b in zip(us_vals, fx_vals)]

    current = spread[-1]
    prev5   = spread[-6]  if n >= 6  else current
    prev20  = spread[-21] if n >= 21 else current

    direction = (
        "widening (USD gaining yield advantage)"  if current > prev5 + 0.02 else
        "narrowing (USD losing yield advantage)"  if current < prev5 - 0.02 else
        "flat"
    )

    return "\n".join([
        f"10Y Yield Spread ({name_a} - {name_b}):",
        f"  {name_a}: {us_vals[-1]:+.2f}%  |  {name_b}: {fx_vals[-1]:+.2f}%",
        f"  Spread now: {current:+.2f}%  |  5d ago: {prev5:+.2f}%  |  20d ago: {prev20:+.2f}%",
        f"  Trend     : {direction}",
        f"  Note      : {note}",
    ])


# ── public API ────────────────────────────────────────────────────────────────

def get_price_summary(symbol: str | None = None) -> str:
    """
    Build a detailed price summary for the given symbol.
    Falls back gracefully if MT5 is disconnected or yfinance is unavailable.
    """
    sym  = symbol or config.MT5_SYMBOL
    pair = config.PAIRS.get(sym, config.PAIRS[config.MT5_SYMBOL])
    pip      = pair["pip"]
    decimals = pair["price_decimals"]
    yf_tick  = pair["yf_ticker"]
    display  = pair["display"]

    parts: list[str] = [f"=== {sym} PRICE SUMMARY ({display}) ===\n"]

    # Live tick
    tick = _live_tick(sym)
    parts.append(_section_live(tick, pip))

    # M15 bars
    bars = _m15_bars(sym)
    parts.append("\n" + _section_m15(bars, pip, decimals))

    # H4 bars
    h4_bars = _h4_bars(sym)

    # Daily data (backbone for all technical sections)
    df = _fetch_daily(yf_tick, period="1y")
    if df is not None and len(df) >= 20:
        closes = df["Close"].squeeze().dropna()
        price  = float(closes.iloc[-1])

        parts.append("\n" + _section_daily(df, decimals))
        parts.append("\n" + _section_trend(closes, df, bars, pip))
        parts.append("\n" + _section_key_levels(closes, df, price, pip, decimals))

        # H4 indicators
        h4_text, h4_dir = _section_h4(h4_bars, pip, decimals)
        parts.append("\n" + h4_text)

        # D1 MACD + Bollinger + ADX
        parts.append("\n" + _section_indicators_d1(closes, df, pip, decimals))

        # Multi-timeframe confluence summary
        sma50  = _sma(closes, 50)
        sma200 = _sma(closes, 200)
        sma20  = _sma(closes, 20)
        if sma50 and sma200:
            if price > sma50 and sma50 > sma200:
                d1_dir = "Bullish"
            elif price < sma50 and sma50 < sma200:
                d1_dir = "Bearish"
            elif sma20 and sma20 > sma50:
                d1_dir = "Mixed (bullish lean)"
            else:
                d1_dir = "Mixed"
        else:
            d1_dir = "Unknown"
        m15_dir = _m15_direction(bars)
        d1_adx  = _adx(df)
        parts.append("\n" + _section_mtf_confluence(d1_dir, h4_dir, m15_dir, d1_adx))

        parts.append("\n" + _section_correlated(sym))
        ys = _section_yield_spread(sym)
        if ys:
            parts.append("\n" + ys)
    else:
        parts.append("\n(Insufficient daily history — yfinance unavailable)")

    return "\n".join(parts)


def get_regime_inputs(symbol: str | None = None) -> dict:
    """
    Compute raw indicator values needed by the regime engine.

    Returns a dict with keys:
      vix, price, sma20, sma50, sma200,
      atr_current, atr_30d_avg,
      m15_up_bars, m15_total_bars, pair_realized_vol_pct
    Any value may be None if data is unavailable.
    """
    sym     = symbol or config.MT5_SYMBOL
    pair    = config.PAIRS.get(sym, config.PAIRS[config.MT5_SYMBOL])
    pip     = pair["pip"]
    yf_tick = pair["yf_ticker"]
    result: dict = {}

    # VIX — equity fear gauge (risk sentiment proxy)
    df_vix = _fetch_daily("^VIX", period="3mo")
    if df_vix is not None and len(df_vix) >= 2:
        result["vix"] = round(float(df_vix["Close"].squeeze().iloc[-1]), 2)

    # Pair daily data — backbone for SMAs and ATR
    df = _fetch_daily(yf_tick, period="1y")
    if df is None or len(df) < 50:
        logger.warning(f"Regime inputs: insufficient daily history for {sym}")
    else:
        closes = df["Close"].squeeze().dropna()
        result["price"]       = round(float(closes.iloc[-1]), 5)
        result["sma20"]       = _sma(closes, 20)
        result["sma50"]       = _sma(closes, 50)
        result["sma200"]      = _sma(closes, 200)
        result["atr_current"] = _atr(df, period=14)
        result["atr_30d_avg"] = _atr(df, period=30)

    # M15 bars — intraday momentum + pair-specific realized volatility
    bars = _m15_bars(sym)
    if bars is not None and len(bars) >= 8:
        last = bars.tail(16)
        up   = int((last["close"] > last["open"]).sum())
        result["m15_up_bars"]   = up
        result["m15_total_bars"] = len(last)

        # Pair realized vol: annualized std dev of M15 log-returns (last 24 bars = 6h)
        try:
            import numpy as np
            last24 = bars.tail(24)
            log_ret = (last24["close"] / last24["close"].shift(1)).apply(
                lambda x: __import__("math").log(x) if x > 0 else 0
            ).dropna()
            # Annualize: 4 bars/h * 24h * 252 trading days
            realized_vol = float(log_ret.std() * (4 * 24 * 252) ** 0.5 * 100)
            result["pair_realized_vol_pct"] = round(realized_vol, 1)
        except Exception:
            pass

    return result


# Which central banks govern each pair (USD leg first, foreign leg second)
_PAIR_BANKS: dict[str, tuple[str, str]] = {
    "EURUSD": ("Fed", "ECB"),
    "GBPUSD": ("Fed", "BOE"),
    "USDJPY": ("Fed", "BOJ"),
}


def _rate_cycle_lines(bank_a: str, bank_b: str) -> list[str]:
    """
    Build rate cycle summary lines for the two central banks.
    Warns if an entry is stale (> 14 days since last update).
    """
    from datetime import date as _date
    from pipeline.cb_policy_updater import get_rate_cycles
    cycles = get_rate_cycles()
    lines  = []

    for bank in (bank_a, bank_b):
        entry = cycles.get(bank)
        if not entry:
            lines.append(f"  {bank}: (no rate cycle data in config.RATE_CYCLES)")
            continue

        stance   = entry.get("stance",   "Unknown")
        guidance = entry.get("guidance", "")
        expected = entry.get("expected", "")
        updated  = entry.get("updated",  "")

        # Staleness check
        stale_flag = ""
        if updated:
            try:
                days_old = (_date.today() - _date.fromisoformat(updated)).days
                if days_old > 14:
                    stale_flag = f"  [!] STALE ({days_old}d old - update config.RATE_CYCLES)"
                elif days_old > 7:
                    stale_flag = f"  (updated {days_old}d ago - verify)"
            except Exception:
                pass

        line = f"  {bank} ({stance}): {guidance}"
        if expected:
            line += f"  |  Mkt expects: {expected}"
        line += stale_flag
        lines.append(line)

    return lines


def _divergence_note(bank_a: str, bank_b: str, sym: str, spread: float) -> str:
    """
    One-line assessment of where the spread is likely to move based on
    the relative rate cycle stances of the two central banks.
    """
    from pipeline.cb_policy_updater import get_rate_cycles
    cycles   = get_rate_cycles()
    stance_a = cycles.get(bank_a, {}).get("stance", "Unknown")
    stance_b = cycles.get(bank_b, {}).get("stance", "Unknown")

    # Determine likely spread direction
    # Spread = US10Y - FX10Y. If bank_a (Fed) cuts faster → spread narrows.
    # If bank_b (ECB/BOE/BOJ) cuts faster → spread widens.
    if stance_a == stance_b:
        direction = f"Both {stance_a} - spread direction driven by relative pace of moves"
    elif stance_a == "Cutting" and stance_b == "Pausing":
        direction = f"{bank_a} cutting vs {bank_b} pausing - spread likely to NARROW"
    elif stance_a == "Cutting" and stance_b == "Hiking":
        direction = f"{bank_a} cutting vs {bank_b} hiking - spread likely to NARROW sharply"
    elif stance_a == "Pausing" and stance_b == "Cutting":
        direction = f"{bank_a} pausing vs {bank_b} cutting - spread likely to WIDEN gradually"
    elif stance_a == "Hiking" and stance_b == "Cutting":
        direction = f"{bank_a} hiking vs {bank_b} cutting - spread likely to WIDEN sharply"
    elif stance_a == "Pausing" and stance_b == "Hiking":
        direction = f"{bank_a} pausing vs {bank_b} hiking - spread likely to NARROW gradually"
    elif stance_a == "Hiking" and stance_b == "Pausing":
        direction = f"{bank_a} hiking vs {bank_b} pausing - spread likely to WIDEN gradually"
    else:
        direction = f"{bank_a}: {stance_a} | {bank_b}: {stance_b}"

    # Translate spread movement into pair implication
    # For USD-quote pairs (EURUSD, GBPUSD): spread widening = USD gaining = bearish pair
    # For USD-base pairs (USDJPY):          spread widening = USD gaining = bullish pair
    if sym == "USDJPY":
        if "WIDEN" in direction:
            implication = "-> sustained bullish USD/JPY pressure"
        elif "NARROW" in direction:
            implication = "-> carry trade at risk, watch for JPY squeeze"
        else:
            implication = "-> mixed; monitor pace of moves"
    else:
        pair_display = config.PAIRS.get(sym, {}).get("display", sym)
        if "WIDEN" in direction:
            implication = f"-> sustained bearish {pair_display} pressure"
        elif "NARROW" in direction:
            implication = f"-> {pair_display} medium-term tailwind building"
        else:
            implication = "-> mixed; monitor pace of moves"

    return f"  Cycle divergence: {direction} {implication}"


def get_macro_bias(symbol: str | None = None) -> str:
    """
    Derive macro bias from live 10Y yield spreads + central bank rate cycles.

    Output format:
      <advantage label> [spread data, trend]
        Fed (Pausing): <guidance> | Mkt expects: <expected>
        ECB (Cutting): <guidance> | Mkt expects: <expected>
        Cycle divergence: <direction> -> <pair implication>

    Data sources (all free, no API key):
      US10Y  : yfinance ^TNX
      DE10Y  : ECB Data Portal
      UK10Y  : Bank of England API
      JP10Y  : Japan Ministry of Finance CSV
    """
    sym          = symbol or config.MT5_SYMBOL
    pair_display = config.PAIRS.get(sym, {}).get("display", sym)
    ccy_base     = pair_display.split("/")[0]
    bank_a, bank_b = _PAIR_BANKS.get(sym, ("Fed", "?"))

    us_vals, fx_vals, name_a, name_b = _get_pair_yields(sym, n=25)

    # Rate cycle lines (always shown, even if yield data fails)
    cycle_lines = _rate_cycle_lines(bank_a, bank_b)

    if not us_vals or not fx_vals:
        logger.debug(f"get_macro_bias: yield data unavailable for {sym}")
        header = f"Yield spread unavailable (data fetch failed)"
        div    = _divergence_note(bank_a, bank_b, sym, 0.0)
        return "\n".join([header] + cycle_lines + [div])

    n       = min(len(us_vals), len(fx_vals))
    us_vals = us_vals[-n:]
    fx_vals = fx_vals[-n:]
    spread  = [round(a - b, 3) for a, b in zip(us_vals, fx_vals)]

    current = spread[-1]
    prev5d  = spread[-6]  if n >= 6  else current
    prev20d = spread[-21] if n >= 21 else current

    delta_5d  = current - prev5d
    delta_20d = current - prev20d

    trend_5d  = "widening" if delta_5d  >  0.03 else ("narrowing" if delta_5d  < -0.03 else "stable")
    trend_20d = "up 20d"   if delta_20d >  0.10 else ("down 20d"  if delta_20d < -0.10 else "flat 20d")

    threshold = 0.30

    # Build header line
    if sym == "USDJPY":
        if current > threshold:
            header = (
                f"USD Yield Advantage [{name_a}={us_vals[-1]:.2f}% / {name_b}={fx_vals[-1]:.2f}%"
                f", spread {current:+.2f}%, {trend_5d}, {trend_20d}]"
            )
        elif current < -threshold:
            header = (
                f"JPY Yield Advantage [{name_b}={fx_vals[-1]:.2f}% / {name_a}={us_vals[-1]:.2f}%"
                f", spread {current:+.2f}%, {trend_5d}, {trend_20d}]"
            )
        else:
            header = (
                f"Neutral Yield Spread [{name_a}/{name_b} {current:+.2f}%,"
                f" {trend_5d}, {trend_20d}]"
            )
    else:
        if current > threshold:
            header = (
                f"USD Yield Advantage [{name_a}={us_vals[-1]:.2f}% / {name_b}={fx_vals[-1]:.2f}%"
                f", spread {current:+.2f}%, {trend_5d}, {trend_20d}]"
            )
        elif current < -threshold:
            header = (
                f"{ccy_base} Yield Advantage [{name_b}={fx_vals[-1]:.2f}% / {name_a}={us_vals[-1]:.2f}%"
                f", spread {current:+.2f}%, {trend_5d}, {trend_20d}]"
            )
        else:
            header = (
                f"Neutral Yield Spread [{name_a}/{name_b} {current:+.2f}%,"
                f" {trend_5d}, {trend_20d}]"
            )

    div = _divergence_note(bank_a, bank_b, sym, current)
    return "\n".join([header] + cycle_lines + [div])
