"""Economic calendar fetcher — FRED (free) + Investing.com scraping.

Primary: Investing.com scraping for today's scheduled USD/EUR events with times.
Fallback/supplement: FRED for latest US macro series values.
Returns list of event dicts matching the events.json schema.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests

from utils.logger import get_logger

logger = get_logger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.investing.com/",
}

# FRED series relevant to EUR/USD context
_FRED_SERIES = {
    "CPIAUCSL": {"name": "US CPI",               "currency": "USD", "impact": "high"},
    "UNRATE":   {"name": "US Unemployment Rate",  "currency": "USD", "impact": "high"},
    "FEDFUNDS": {"name": "Federal Funds Rate",    "currency": "USD", "impact": "high"},
    "GDP":      {"name": "US Real GDP",           "currency": "USD", "impact": "high"},
    "PAYEMS":   {"name": "US Nonfarm Payrolls",   "currency": "USD", "impact": "high"},
}


# ── FRED ──────────────────────────────────────────────────────────────────────

def _fetch_fred_latest(series_id: str) -> dict[str, Any] | None:
    """Fetch the most recent observation for a FRED series (no API key needed)."""
    try:
        resp = requests.get(
            "https://fred.stlouisfed.org/graph/fredgraph.json",
            params={"id": series_id},
            headers=_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "date" in data:
            dates = data.get("date", [])
            values = data.get("value", [])
            if dates and values:
                try:
                    return {"date": dates[-1], "value": float(values[-1])}
                except (ValueError, TypeError):
                    return None
    except Exception as e:
        logger.debug(f"FRED {series_id} fetch failed: {e}")
    return None


def fetch_fred_events() -> list[dict[str, Any]]:
    """Return latest values for key US macro series from FRED."""
    events = []
    for series_id, meta in _FRED_SERIES.items():
        obs = _fetch_fred_latest(series_id)
        if obs is None:
            continue
        events.append({
            "time": "N/A",
            "event": meta["name"],
            "currency": meta["currency"],
            "actual": obs.get("value"),
            "forecast": None,
            "previous": None,
            "surprise": None,
            "surprise_magnitude": None,
            "impact": meta["impact"],
            "source": "FRED",
            "release_date": obs.get("date"),
        })
    return events


# ── ForexFactory calendar ─────────────────────────────────────────────────────

def fetch_forexfactory_calendar() -> list[dict[str, Any]]:
    """
    Fetch this week's economic calendar from ForexFactory's public JSON endpoint.
    Filters for medium/high-impact USD and EUR events only.
    """
    try:
        resp = requests.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            headers=_HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        raw = resp.json()

        today = datetime.now(timezone.utc).date().isoformat()
        events = []
        for item in raw:
            try:
                currency = item.get("country", "").upper()
                if currency not in ("USD", "EUR"):
                    continue

                impact_raw = item.get("impact", "").lower()
                if impact_raw not in ("high", "medium"):
                    continue
                impact = impact_raw

                # ForexFactory date format: "2026-03-15T13:30:00-0400"
                date_str = item.get("date", "")
                event_date = date_str[:10] if date_str else ""

                def _parse_val(val) -> float | None:
                    if val is None or val == "":
                        return None
                    try:
                        return float(str(val).replace("%", "").replace("K", "000")
                                     .replace("M", "000000").replace(",", ""))
                    except (ValueError, TypeError):
                        return None

                actual = _parse_val(item.get("actual"))
                forecast = _parse_val(item.get("forecast"))
                previous = _parse_val(item.get("previous"))

                surprise = None
                surprise_magnitude = None
                if actual is not None and forecast is not None:
                    surprise_magnitude = round(actual - forecast, 4)
                    surprise = "beat" if surprise_magnitude > 0 else (
                        "miss" if surprise_magnitude < 0 else "inline"
                    )

                events.append({
                    "time": date_str,
                    "event": item.get("title", ""),
                    "currency": currency,
                    "actual": actual,
                    "forecast": forecast,
                    "previous": previous,
                    "surprise": surprise,
                    "surprise_magnitude": surprise_magnitude,
                    "impact": impact,
                    "source": "ForexFactory",
                    "release_date": event_date,
                })
            except Exception:
                continue

        logger.info(f"ForexFactory: fetched {len(events)} USD/EUR events this week")
        return events
    except Exception as e:
        logger.warning(f"ForexFactory calendar fetch failed: {e}")
        return []


# ── combined fetcher ──────────────────────────────────────────────────────────

def fetch_events(date_str: str | None = None) -> list[dict[str, Any]]:
    """
    Fetch economic events from all sources.
    Investing.com provides scheduled events with times; FRED supplements with
    latest macro values not already covered.
    """
    all_events: list[dict[str, Any]] = []

    try:
        ff_events = fetch_forexfactory_calendar()
        all_events.extend(ff_events)
    except Exception as e:
        logger.error(f"ForexFactory fetch error: {e}")

    try:
        fred_events = fetch_fred_events()
        existing_names = {e["event"].lower() for e in all_events}
        for ev in fred_events:
            if ev["event"].lower() not in existing_names:
                all_events.append(ev)
    except Exception as e:
        logger.error(f"FRED fetch error: {e}")

    return all_events
