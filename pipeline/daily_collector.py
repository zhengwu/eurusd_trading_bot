"""Daily collector — Tier 2 end-of-day pipeline.

Runs once at 22:00 UTC:
  1. Fetch and save prices.json
  2. Fetch and save events.json
  3. Merge intraday.jsonl → news.jsonl (dedup on URL)
  4. Generate summary.md via LLM
  5. Clear dedup cache for next day
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv

import config
from pipeline.price_fetcher import fetch_prices
from pipeline.event_fetcher import fetch_events
from pipeline.dedup_cache import DedupCache
from utils.logger import get_logger
from utils.date_utils import today_str_utc

load_dotenv()
logger = get_logger(__name__)


def _day_dir(date_str: str) -> Path:
    d = config.DATA_DIR / date_str
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                continue
    return records


# ── Step 1: prices ─────────────────────────────────────────────────────────────

def collect_prices(date_str: str) -> None:
    logger.info("Fetching closing prices...")
    try:
        prices = fetch_prices(date_str)
        _save_json(_day_dir(date_str) / "prices.json", prices)
        eurusd = prices.get("EURUSD", {}).get("close")
        logger.info(f"Prices saved — EURUSD close: {eurusd}")
    except Exception as e:
        logger.error(f"Price collection failed: {e}")


# ── Step 2: events ─────────────────────────────────────────────────────────────

def collect_events(date_str: str) -> None:
    logger.info("Fetching economic events...")
    try:
        events = fetch_events(date_str)
        _save_json(_day_dir(date_str) / "events.json", events)
        logger.info(f"Events saved: {len(events)} entries")
    except Exception as e:
        logger.error(f"Event collection failed: {e}")


# ── Step 3: merge intraday → news ──────────────────────────────────────────────

def merge_intraday_to_news(date_str: str) -> None:
    """Append intraday.jsonl records into news.jsonl, deduplicating by URL."""
    day_dir = _day_dir(date_str)
    intraday_path = day_dir / "intraday.jsonl"
    news_path = day_dir / "news.jsonl"

    intraday_records = _read_jsonl(intraday_path)
    if not intraday_records:
        logger.info("No intraday records to merge")
        return

    existing = _read_jsonl(news_path)
    seen_urls = {r.get("url") for r in existing if r.get("url")}

    new_records = [r for r in intraday_records if r.get("url") not in seen_urls]
    if not new_records:
        logger.info("All intraday records already in news.jsonl")
        return

    with news_path.open("a", encoding="utf-8") as f:
        for r in new_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    logger.info(f"Merged {len(new_records)} new records into news.jsonl")


# ── Step 4: generate summary ───────────────────────────────────────────────────

def generate_summary(date_str: str) -> None:
    """Generate end-of-day summary.md via Claude Sonnet."""
    day_dir = _day_dir(date_str)

    news_records = _read_jsonl(day_dir / "news.jsonl") or _read_jsonl(day_dir / "intraday.jsonl")

    try:
        prices = json.loads((day_dir / "prices.json").read_text(encoding="utf-8"))
    except Exception:
        prices = {}

    try:
        events = json.loads((day_dir / "events.json").read_text(encoding="utf-8"))
    except Exception:
        events = []

    headlines_text = "\n".join(
        f"- [{r.get('time', '')} EST] {r.get('headline', '')}"
        for r in news_records[:30]
    )
    events_text = "\n".join(
        f"- {e.get('event', '')} ({e.get('currency', '')}) "
        f"actual={e.get('actual')} forecast={e.get('forecast')} "
        f"[{e.get('surprise', '')}]"
        for e in events
    )

    eurusd = prices.get("EURUSD") or {}
    prompt = f"""\
Write a concise end-of-day market summary for EUR/USD traders.

Date: {date_str}

Key headlines today:
{headlines_text or '(none)'}

Economic events:
{events_text or '(none)'}

EUR/USD: close={eurusd.get('close')} pct_change={eurusd.get('pct_change')}%
DXY: {(prices.get('DXY') or {}).get('close')}  \
VIX: {(prices.get('VIX') or {}).get('close')}  \
Gold: {(prices.get('Gold') or {}).get('close')}  \
US10Y: {(prices.get('US10Y') or {}).get('close')}

Write 4-6 bullet points covering: dominant theme, key data releases/surprises, \
EUR/USD price range and close, risk sentiment, and what to watch tomorrow.
Format as markdown bullet points under the heading: ## Market Summary — {date_str}"""

    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        message = client.messages.create(
            model=config.ANALYSIS_MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        summary = message.content[0].text.strip()
        (day_dir / "summary.md").write_text(summary, encoding="utf-8")
        logger.info(f"summary.md generated for {date_str}")
    except Exception as e:
        logger.error(f"Summary generation failed: {e}")


# ── orchestrator ───────────────────────────────────────────────────────────────

def run_daily_collection(date_str: str | None = None) -> None:
    """Run the full end-of-day collection pipeline."""
    date_str = date_str or today_str_utc()
    logger.info(f"=== Daily collection starting for {date_str} ===")

    collect_prices(date_str)
    collect_events(date_str)
    merge_intraday_to_news(date_str)
    generate_summary(date_str)

    # Clear dedup cache so next day starts fresh
    cache = DedupCache(config.DATA_DIR / ".dedup_cache")
    cache.clear()
    logger.info("Dedup cache cleared for next day")
    logger.info("=== Daily collection complete ===")
