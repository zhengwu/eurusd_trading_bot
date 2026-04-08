"""News fetcher — wraps four providers: NewsAPI, Alpha Vantage, EODHD, Yahoo Finance.

Ported and refactored from the existing news_fetching.py.
Output is a list of normalized dicts ready for DedupCache + triage.
Deduplication across providers happens here (by URL); per-day dedup
against already-processed articles is handled by DedupCache in scanner.py.
"""
from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
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


# ── FMP (Financial Modeling Prep) ────────────────────────────────────────────

def fetch_fmp(start: date, end: date, max_items: int, symbol: str = "EURUSD") -> list[dict[str, Any]]:
    """
    Fetch forex news from FMP /stable/news/forex-latest.
    FMP indexes news with low latency (~5 min) vs hours for NewsAPI/EODHD.
    Paginates via 'page' param until we have enough items or exhaust results.
    """
    key = os.getenv("FMP_API_KEY")
    if not key:
        logger.warning("FMP_API_KEY not set — skipping FMP")
        return []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    page = 0

    while len(out) < max_items:
        params = {
            "page": page,
            "limit": min(max_items * 2, 50),
            "apikey": key,
        }
        try:
            resp = requests.get(
                "https://financialmodelingprep.com/stable/news/forex-latest",
                params=params,
                headers=_HEADERS,
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"FMP fetch failed (page {page}): {e}")
            break

        if not isinstance(data, list) or not data:
            break

        added = 0
        for a in data:
            title = (a.get("title") or "").strip()
            url = (a.get("url") or "").strip()
            if not title or not url or url in seen:
                continue
            dt = _parse_dt(a.get("publishedDate") or "")
            if not _in_range(dt, start, end):
                continue
            seen.add(url)
            host = _hostname(url)
            src_site = a.get("site") or host or "FMP"
            out.append({
                "source": f"FMP:{src_site}",
                "title": title,
                "url": url,
                "published_dt": dt,
                "snippet": (a.get("text") or "")[:600],
            })
            added += 1
            if len(out) >= max_items:
                break

        # If this page returned nothing new, stop paginating
        if added == 0:
            break
        page += 1

    return out


# ── Finnhub ───────────────────────────────────────────────────────────────────

# Track last seen article ID per symbol so we only fetch new articles each poll
_finnhub_last_id: dict[str, int] = {}


def fetch_finnhub(start: date, end: date, max_items: int, symbol: str = "EURUSD") -> list[dict[str, Any]]:
    """
    Fetch forex news from Finnhub GET /news?category=forex.
    Uses minId pagination to only retrieve articles newer than the last fetch.
    No webhook required — REST polling is sufficient for news.
    Free tier supports 60 calls/min.
    """
    key = os.getenv("FINNHUB_API_KEY")
    if not key:
        logger.warning("FINNHUB_API_KEY not set — skipping Finnhub")
        return []

    params: dict[str, Any] = {
        "category": "forex",
        "token": key,
    }
    min_id = _finnhub_last_id.get(symbol, 0)
    if min_id:
        params["minId"] = min_id

    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/news",
            params=params,
            headers=_HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"Finnhub fetch failed: {e}")
        return []

    if not isinstance(data, list):
        return []

    start_ts = datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp()
    end_ts   = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc).timestamp()

    out: list[dict[str, Any]] = []
    max_id_seen = min_id

    for a in data:
        title = (a.get("headline") or "").strip()
        url = (a.get("url") or "").strip()
        if not title or not url:
            continue

        pub_ts = a.get("datetime")
        if pub_ts:
            dt = datetime.fromtimestamp(float(pub_ts), tz=timezone.utc)
            if dt.timestamp() < start_ts or dt.timestamp() > end_ts:
                continue
        else:
            dt = None

        article_id = a.get("id") or 0
        if article_id > max_id_seen:
            max_id_seen = article_id

        out.append({
            "source": f"Finnhub:{a.get('source') or 'finnhub'}",
            "title": title,
            "url": url,
            "published_dt": dt,
            "snippet": (a.get("summary") or "")[:600],
        })
        if len(out) >= max_items:
            break

    # Advance the cursor so next poll only fetches newer articles
    if max_id_seen > min_id:
        _finnhub_last_id[symbol] = max_id_seen

    return out


# ── RSS (ForexLive + FXStreet) ────────────────────────────────────────────────
#
# No API key required. Both feeds update within 2–5 minutes of publication.
# ForexLive is especially valuable for breaking macro/geopolitical headlines.

_RSS_FEEDS = [
    ("https://www.forexlive.com/feed/news",  "ForexLive"),
    ("https://www.fxstreet.com/rss/news",    "FXStreet"),
]

# Per-pair keyword filter — avoid RSS flooding the log with irrelevant cross-pair TA
_RSS_PAIR_KEYWORDS: dict[str, list[str]] = {
    "EURUSD": [
        r"\bEUR\b", r"\beuro\b", r"\bECB\b", r"\beuropean central bank\b",
        r"\bEurozone\b", r"\bGermany\b", r"\bFrance\b",
        r"\bUSD\b", r"\bdollar\b", r"\bFed\b", r"\bfederal reserve\b",
        r"\binflation\b", r"\bCPI\b", r"\bGDP\b", r"\bNFP\b", r"\btariff",
        r"\bgeopolit", r"\brisk.off\b", r"\brisk.on\b",
    ],
    "GBPUSD": [
        r"\bGBP\b", r"\bpound\b", r"\bsterling\b", r"\bBOE\b", r"\bbank of england\b",
        r"\bUK\b", r"\bBritish\b", r"\bBrexit\b",
        r"\bUSD\b", r"\bdollar\b", r"\bFed\b", r"\bfederal reserve\b",
        r"\binflation\b", r"\bCPI\b", r"\bGDP\b", r"\btariff",
        r"\bgeopolit", r"\brisk.off\b", r"\brisk.on\b",
    ],
    "USDJPY": [
        r"\bJPY\b", r"\byen\b", r"\bBOJ\b", r"\bbank of japan\b",
        r"\bJapan\b", r"\bJapanese\b", r"\bNikkei\b", r"\bYCC\b",
        r"\bUSD\b", r"\bdollar\b", r"\bFed\b", r"\bfederal reserve\b",
        r"\bsafe.haven\b", r"\brisk.off\b", r"\brisk.on\b",
        r"\binflation\b", r"\bCPI\b", r"\btariff",
    ],
    "AUDUSD": [
        r"\bAUD\b", r"\baussie\b", r"\bRBA\b", r"\bReserve Bank of Australia\b",
        r"\bAustrali", r"\bChina\b", r"\biron ore\b", r"\bcommodit",
        r"\bUSD\b", r"\bdollar\b", r"\bFed\b", r"\bfederal reserve\b",
        r"\binflation\b", r"\bCPI\b", r"\btariff",
    ],
}


def fetch_rss(start: date, end: date, max_items: int, symbol: str = "EURUSD") -> list[dict[str, Any]]:
    """
    Fetch recent forex news from ForexLive and FXStreet RSS feeds.
    No API key required. Latency ~2-5 minutes from publication.
    Filters articles by pair-relevant keywords to avoid irrelevant cross-pair TA noise.
    """
    patterns = _RSS_PAIR_KEYWORDS.get(symbol, _RSS_PAIR_KEYWORDS["EURUSD"])
    start_ts = datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp()
    end_ts   = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc).timestamp()

    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for feed_url, feed_name in _RSS_FEEDS:
        if len(out) >= max_items:
            break
        try:
            resp = requests.get(feed_url, headers=_HEADERS, timeout=15)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            channel = root.find("channel")
            if channel is None:
                continue
            items = channel.findall("item")
        except Exception as e:
            logger.warning(f"RSS fetch failed [{feed_name}]: {e}")
            continue

        for item in items:
            if len(out) >= max_items:
                break

            title = (item.findtext("title") or "").strip()
            url   = (item.findtext("link")  or "").strip()
            if not title or not url or url in seen:
                continue

            pubdate_str = item.findtext("pubDate") or ""
            dt: Optional[datetime] = None
            if pubdate_str:
                try:
                    dt = parsedate_to_datetime(pubdate_str).astimezone(timezone.utc)
                except Exception:
                    pass

            if dt and (dt.timestamp() < start_ts or dt.timestamp() > end_ts):
                continue

            # Keyword filter — skip articles with no pair-relevant terms
            text = title.lower()
            desc = (item.findtext("description") or "").lower()
            combined = text + " " + desc
            if not any(re.search(p, combined, re.IGNORECASE) for p in patterns):
                continue

            seen.add(url)
            snippet = (item.findtext("description") or "").strip()
            out.append({
                "source": f"RSS:{feed_name}",
                "title": title,
                "url": url,
                "published_dt": dt,
                "snippet": snippet[:600],
            })

    logger.debug(f"RSS [{symbol}]: {len(out)} items from {[f[1] for f in _RSS_FEEDS]}")
    return out


# ── Yahoo Finance ─────────────────────────────────────────────────────────────
#
# Uses the yfinance Ticker.news property — already a project dependency (price_agent.py).
# No API key required. Fetches from multiple tickers per pair to maximise coverage:
# the pair ticker itself plus correlated assets (DXY, Gold, etc.) that drive the pair.

# Per-pair tickers to pull Yahoo Finance news from
_YF_NEWS_TICKERS: dict[str, list[str]] = {
    "EURUSD": ["EURUSD=X", "DX-Y.NYB", "^TNX", "GC=F"],
    "GBPUSD": ["GBPUSD=X", "DX-Y.NYB", "^TNX"],
    "USDJPY": ["USDJPY=X", "DX-Y.NYB", "^TNX", "^N225"],
    "AUDUSD": ["AUDUSD=X", "DX-Y.NYB", "GC=F"],
}


def fetch_yahoo_finance(start: date, end: date, max_items: int, symbol: str = "EURUSD") -> list[dict[str, Any]]:
    """
    Fetch recent news from Yahoo Finance via yfinance (no API key required).
    Pulls from the pair ticker and its key correlated tickers, then deduplicates by URL.
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance not installed — skipping Yahoo Finance news")
        return []

    tickers = _YF_NEWS_TICKERS.get(symbol, _YF_NEWS_TICKERS["EURUSD"])
    start_ts = datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp()
    end_ts   = datetime(end.year,   end.month,   end.day,   23, 59, 59, tzinfo=timezone.utc).timestamp()

    seen_urls: set[str] = set()
    out: list[dict[str, Any]] = []

    for ticker_sym in tickers:
        if len(out) >= max_items:
            break
        try:
            ticker = yf.Ticker(ticker_sym)
            raw_news = ticker.news or []
        except Exception as e:
            logger.debug(f"Yahoo Finance {ticker_sym}: {e}")
            continue

        for item in raw_news:
            if len(out) >= max_items:
                break

            # yfinance news schema: title, link, publisher, providerPublishTime (unix ts), type
            content_type = item.get("type", "")
            if content_type and content_type.upper() not in ("STORY", ""):
                continue  # skip VIDEO, THUMBNAIL-only items

            title = (item.get("title") or "").strip()
            url   = (item.get("link")  or "").strip()
            if not title or not url or url in seen_urls:
                continue

            pub_ts = item.get("providerPublishTime")
            if pub_ts:
                dt = datetime.fromtimestamp(float(pub_ts), tz=timezone.utc)
                if dt.timestamp() < start_ts or dt.timestamp() > end_ts:
                    continue
            else:
                dt = None

            publisher = item.get("publisher") or ticker_sym
            seen_urls.add(url)
            out.append({
                "source": f"YahooFinance:{publisher}",
                "title": title,
                "url": url,
                "published_dt": dt,
                "snippet": "",   # Yahoo Finance doesn't return body text in this endpoint
            })

    logger.debug(f"Yahoo Finance [{symbol}]: {len(out)} items from {tickers}")
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
    # Low-latency sources first — they win dedup when same article appears in multiple providers
    # Order: RSS (~2-5 min) → FMP (~5 min) → Finnhub (~5 min) → slower batch APIs
    for fetch_fn in [fetch_rss, fetch_fmp, fetch_finnhub, fetch_newsapi, fetch_alphavantage, fetch_eodhd, fetch_yahoo_finance]:
        try:
            items = fetch_fn(start, today, max_per_provider, sym)
            all_items.extend(items)
            logger.debug(f"{fetch_fn.__name__} [{sym}]: {len(items)} items")
        except Exception as e:
            logger.error(f"{fetch_fn.__name__} failed: {e}")

    # Drop articles older than NEWS_MAX_AGE_HOURS to filter stale delayed articles
    max_age_hours = getattr(config, "NEWS_MAX_AGE_HOURS", None)
    if max_age_hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        before = len(all_items)
        all_items = [
            item for item in all_items
            if item.get("published_dt") is None or item["published_dt"] >= cutoff
        ]
        dropped = before - len(all_items)
        if dropped:
            logger.debug(f"Dropped {dropped} articles older than {max_age_hours}h")

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
