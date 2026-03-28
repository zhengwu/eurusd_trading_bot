"""News fetcher — wraps three providers: NewsAPI, Alpha Vantage, EODHD.

Ported and refactored from the existing news_fetching.py.
Output is a list of normalized dicts ready for DedupCache + triage.
Deduplication across providers happens here (by URL); per-day dedup
against already-processed articles is handled by DedupCache in scanner.py.
"""
from __future__ import annotations

import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

import config
from utils.logger import get_logger

load_dotenv()
logger = get_logger(__name__)

# ── Per-pair news query config ─────────────────────────────────────────────────
_PAIR_NEWS: dict[str, dict] = {
    "EURUSD": {
        "newsapi_pair":    '"EUR/USD" OR "EURUSD" OR (euro AND dollar)',
        "newsapi_cb":      (
            'ECB OR Fed OR "European Central Bank" OR "Federal Reserve" OR '
            'Eurozone OR "euro area" OR Germany OR France OR "EU economy"'
        ),
        "av_patterns":     [
            r"\bEUR\b", r"\bUSD\b", r"\beuro\b", r"\bdollar\b",
            r"\bECB\b", r"\bFed\b", r"\bforex\b",
            r"\bEurozone\b", r"\bGermany\b", r"\bEuropean\b",
        ],
        "eodhd_ticker":    "EURUSD.FOREX",
    },
    "GBPUSD": {
        "newsapi_pair":    '"GBP/USD" OR "GBPUSD" OR "cable" OR (pound AND dollar)',
        "newsapi_cb":      (
            'BOE OR "Bank of England" OR Fed OR "Federal Reserve" OR '
            '"UK economy" OR "British economy" OR Brexit OR "UK inflation" OR "UK GDP"'
        ),
        "av_patterns":     [
            r"\bGBP\b", r"\bUSD\b", r"\bpound\b", r"\bdollar\b",
            r"\bBOE\b", r"\bFed\b", r"\bforex\b",
            r"\bUK\b", r"\bBritish\b", r"\bBrexit\b",
        ],
        "eodhd_ticker":    "GBPUSD.FOREX",
    },
    "USDJPY": {
        "newsapi_pair":    '"USD/JPY" OR "USDJPY" OR (dollar AND yen)',
        "newsapi_cb":      (
            'BOJ OR "Bank of Japan" OR Fed OR "Federal Reserve" OR YCC OR '
            '"Japan economy" OR "Japanese economy" OR "safe haven" OR Nikkei'
        ),
        "av_patterns":     [
            r"\bJPY\b", r"\bUSD\b", r"\byen\b", r"\bdollar\b",
            r"\bBOJ\b", r"\bFed\b", r"\bforex\b",
            r"\bJapan\b", r"\bJapanese\b", r"\bNikkei\b",
        ],
        "eodhd_ticker":    "USDJPY.FOREX",
    },
    "AUDUSD": {
        "newsapi_pair":    '"AUD/USD" OR "AUDUSD" OR (aussie AND dollar)',
        "newsapi_cb":      (
            'RBA OR "Reserve Bank of Australia" OR Fed OR "Federal Reserve" OR '
            '"Australian economy" OR "China GDP" OR "iron ore" OR commodity'
        ),
        "av_patterns":     [
            r"\bAUD\b", r"\bUSD\b", r"\baussie\b", r"\bdollar\b",
            r"\bRBA\b", r"\bFed\b", r"\bforex\b",
            r"\bAustralia\b", r"\bAustralian\b", r"\bChina\b",
        ],
        "eodhd_ticker":    "AUDUSD.FOREX",
    },
}


def _pair_news_cfg(symbol: str) -> dict:
    """Return news query config for a symbol, falling back to EURUSD defaults."""
    return _PAIR_NEWS.get(symbol, _PAIR_NEWS["EURUSD"])


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ── datetime helpers ──────────────────────────────────────────────────────────

def _parse_dt(value: str) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        try:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_av_time(value: str) -> Optional[datetime]:
    """Parse Alpha Vantage time_published format: 20250313T143200."""
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _in_range(dt: Optional[datetime], start: Optional[date], end: Optional[date]) -> bool:
    if dt is None:
        return True
    d = dt.date()
    if start and d < start:
        return False
    if end and d > end:
        return False
    return True


def _hostname(url: str) -> str:
    try:
        h = urlparse(url).netloc.lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


# ── article full-text extraction ──────────────────────────────────────────────

def fetch_article_text(url: str, max_chars: int = 3000) -> str:
    """Best-effort extraction of readable article text via BeautifulSoup."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        root = soup.find("article") or soup.find("main") or soup.body or soup
        paras = [
            p.get_text(" ", strip=True)
            for p in root.find_all("p")
            if len(p.get_text(" ", strip=True)) >= 40
        ]
        text = "\n\n".join(paras).strip()
        return (text[:max_chars] + "…") if len(text) > max_chars else text
    except Exception:
        return ""


# ── NewsAPI ───────────────────────────────────────────────────────────────────

def _newsapi_query(symbol: str = "EURUSD") -> str:
    cfg = _pair_news_cfg(symbol)
    intent_block = (
        'inflation OR CPI OR GDP OR NFP OR payrolls OR '
        '"rate cut" OR "rate hike" OR "interest rate" OR '
        '"central bank" OR geopolitical OR "risk off"'
    )
    return f"({cfg['newsapi_pair']}) AND ({cfg['newsapi_cb']} OR {intent_block})"


def fetch_newsapi(start: date, end: date, max_items: int, symbol: str = "EURUSD") -> list[dict[str, Any]]:
    key = os.getenv("NEWS_API_KEY")
    if not key:
        logger.warning("NEWS_API_KEY not set — skipping NewsAPI")
        return []
    params = {
        "q": _newsapi_query(symbol),
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": min(max_items, 100),
        "from": start.isoformat(),
        "to": end.isoformat(),
        "apiKey": key,
    }
    try:
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params=params,
            headers=_HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        articles = resp.json().get("articles", [])
    except Exception as e:
        logger.warning(f"NewsAPI fetch failed: {e}")
        return []

    out = []
    for a in articles:
        title = (a.get("title") or "").strip()
        url = (a.get("url") or "").strip()
        if not title or not url:
            continue
        dt = _parse_dt(a.get("publishedAt") or "")
        if not _in_range(dt, start, end):
            continue
        src = (a.get("source") or {}).get("name") or "NewsAPI"
        snippet = ((a.get("description") or "") + " " + (a.get("content") or "")).strip()
        out.append({
            "source": f"NewsAPI:{src}",
            "title": title,
            "url": url,
            "published_dt": dt,
            "snippet": snippet[:600],
        })
    return out[:max_items]


# ── Alpha Vantage NEWS_SENTIMENT ──────────────────────────────────────────────

def fetch_alphavantage(start: date, end: date, max_items: int, symbol: str = "EURUSD") -> list[dict[str, Any]]:
    key = os.getenv("ALPHA_VANTAGE_API_KEY")
    if not key:
        logger.warning("ALPHA_VANTAGE_API_KEY not set — skipping Alpha Vantage")
        return []

    today = datetime.now(timezone.utc).date()
    eff_end = end if end <= today else None

    params: dict[str, Any] = {
        "function": "NEWS_SENTIMENT",
        "sort": "LATEST",
        "limit": min(max_items * 5, 200),  # fetch more, then filter for EUR/USD relevance
        "topics": "economy_monetary,economy_macro,financial_markets",
        "apikey": key,
    }
    if start:
        params["time_from"] = start.strftime("%Y%m%dT0000")
    if eff_end:
        params["time_to"] = eff_end.strftime("%Y%m%dT2359")

    def _request():
        try:
            r = requests.get(
                "https://www.alphavantage.co/query",
                params=params,
                headers=_HEADERS,
                timeout=25,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.warning(f"Alpha Vantage fetch failed: {e}")
            return None

    data = _request()
    if not data:
        return []

    info = data.get("Information") or data.get("Note") or data.get("Error Message")
    if info and "request per second" in str(info).lower():
        time.sleep(1.1)
        data = _request()
        if not data:
            return []

    feed = data.get("feed") if isinstance(data, dict) else None
    if not isinstance(feed, list):
        return []

    # Filter for pair relevance by keyword matching
    patterns = _pair_news_cfg(symbol)["av_patterns"]
    out = []
    seen: set[str] = set()
    for a in feed:
        title = (a.get("title") or "").strip()
        url = (a.get("url") or "").strip()
        if not title or not url or url in seen:
            continue
        dt = _parse_av_time(a.get("time_published") or "")
        if not _in_range(dt, start, eff_end):
            continue
        text = (title + " " + (a.get("summary") or "")).lower()
        if not any(re.search(p, text, re.IGNORECASE) for p in patterns):
            continue
        src = str(a.get("source") or a.get("source_domain") or "AlphaVantage")
        summary = (a.get("summary") or "").strip()
        out.append({
            "source": f"AlphaVantage:{src}",
            "title": title,
            "url": url,
            "published_dt": dt,
            "snippet": summary[:600],
        })
        seen.add(url)
        if len(out) >= max_items:
            break
    return out


# ── EODHD ─────────────────────────────────────────────────────────────────────

def fetch_eodhd(start: date, end: date, max_items: int, symbol: str = "EURUSD") -> list[dict[str, Any]]:
    key = os.getenv("EODHD_API_KEY")
    if not key:
        logger.warning("EODHD_API_KEY not set — skipping EODHD")
        return []
    params = {
        "s": _pair_news_cfg(symbol)["eodhd_ticker"],
        "offset": 0,
        "limit": max_items,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "api_token": key,
        "fmt": "json",
    }
    try:
        resp = requests.get(
            "https://eodhd.com/api/news",
            params=params,
            headers=_HEADERS,
            timeout=25,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"EODHD fetch failed: {e}")
        return []

    if not isinstance(data, list):
        return []

    out = []
    for a in data:
        title = (a.get("title") or "").strip()
        url = (a.get("link") or "").strip()
        if not title or not url:
            continue
        dt = _parse_dt(a.get("date") or "")
        if not _in_range(dt, start, end):
            continue
        content = (a.get("content") or "").strip()
        host = _hostname(url)
        out.append({
            "source": f"EODHD:{host}" if host else "EODHD",
            "title": title,
            "url": url,
            "published_dt": dt,
            "snippet": content[:600],
        })
        if len(out) >= max_items:
            break
    return out


# ── combined fetcher ──────────────────────────────────────────────────────────

def fetch_news(
    days_back: int | None = None,
    max_per_provider: int | None = None,
    symbol: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch forex news from all configured providers for the given symbol.
    Returns normalized list deduplicated by URL within this batch.
    Per-day dedup against already-processed articles is done by DedupCache.
    """
    sym = symbol or config.MT5_SYMBOL
    days_back = days_back or config.NEWS_WINDOW_DAYS
    max_per_provider = max_per_provider or config.NEWS_PER_PROVIDER_MAX_ITEMS
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days_back)

    all_items: list[dict[str, Any]] = []
    for fetch_fn in [fetch_newsapi, fetch_alphavantage, fetch_eodhd]:
        try:
            items = fetch_fn(start, today, max_per_provider, sym)
            all_items.extend(items)
            logger.debug(f"{fetch_fn.__name__} [{sym}]: {len(items)} items")
        except Exception as e:
            logger.error(f"{fetch_fn.__name__} failed: {e}")

    # Deduplicate by URL within this combined batch
    seen: set[str] = set()
    deduped = []
    for item in all_items:
        url = item.get("url", "")
        if url and url not in seen:
            seen.add(url)
            deduped.append(item)

    logger.info(f"fetch_news: {len(deduped)} unique articles from {len(all_items)} total")
    return deduped
