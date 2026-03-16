"""Intraday logger — appends scored articles to intraday.jsonl.

Each record written matches the intraday.jsonl schema from the spec.
Times are stored in EST (the user's timezone) for readability.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from utils.logger import get_logger
from utils.date_utils import today_str_utc, to_est

logger = get_logger(__name__)


def _intraday_path(date_str: str | None = None) -> Path:
    d = date_str or today_str_utc()
    return config.DATA_DIR / d / "intraday.jsonl"


def append_scored_articles(scored_items: list[dict[str, Any]]) -> None:
    """
    Append triage-scored articles to today's intraday.jsonl.
    Each item should contain news fields + triage fields merged together.
    """
    path = _intraday_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    now_est = to_est(datetime.now(timezone.utc))

    with path.open("a", encoding="utf-8") as f:
        for item in scored_items:
            record = {
                "date": now_est.strftime("%Y-%m-%d"),
                "time": now_est.strftime("%H:%M"),
                "source": item.get("source", ""),
                "headline": item.get("headline") or item.get("title", ""),
                "url": item.get("url", ""),
                "sentiment": None,
                "tags": [item.get("tag", "other")],
                "triage_score": item.get("score"),
                "triage_tag": item.get("tag"),
                "triage_reason": item.get("reason"),
                "triggered_full_analysis": item.get("triggered_full_analysis", False),
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    logger.debug(f"Appended {len(scored_items)} items to {path}")


def read_today_intraday(date_str: str | None = None) -> list[dict[str, Any]]:
    """Read all intraday records for a given date (defaults to today UTC)."""
    path = _intraday_path(date_str)
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
