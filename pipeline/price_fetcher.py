"""Price fetcher — yfinance for daily closing prices.

Fetches all assets in config.PRICE_ASSETS and returns a dict
matching the prices.json schema. Falls back gracefully per-asset.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import yfinance as yf

import config
from utils.logger import get_logger

logger = get_logger(__name__)


def fetch_prices(date_str: str | None = None) -> dict[str, Any]:
    """
    Fetch closing prices for all configured assets.
    Returns a dict matching the prices.json schema.
    """
    today = datetime.now(timezone.utc).date()
    target_date = date_str or today.isoformat()

    # Fetch a 5-day window to ensure we always have at least 2 closes
    # (handles weekends, holidays where markets are closed)
    start = (today - timedelta(days=5)).isoformat()
    end = today.isoformat()

    result: dict[str, Any] = {"date": target_date}

    for name, ticker in config.PRICE_ASSETS.items():
        try:
            data = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
            if data.empty:
                logger.warning(f"No price data for {name} ({ticker})")
                result[name] = {"close": None, "pct_change": None}
                continue

            closes = data["Close"].dropna()
            if len(closes) < 1:
                result[name] = {"close": None, "pct_change": None}
                continue

            latest_close = float(closes.iloc[-1].item())
            pct_change = None
            if len(closes) >= 2:
                prev_close = float(closes.iloc[-2].item())
                if prev_close != 0:
                    pct_change = round((latest_close - prev_close) / prev_close * 100, 4)

            result[name] = {
                "close": round(latest_close, 4),
                "pct_change": pct_change,
            }
            pct_str = f"{pct_change:+.2f}%" if pct_change is not None else "N/A"
            logger.debug(f"{name}: {latest_close:.4f} ({pct_str})")
        except Exception as e:
            logger.error(f"Price fetch failed for {name} ({ticker}): {e}")
            result[name] = {"close": None, "pct_change": None}

    return result
