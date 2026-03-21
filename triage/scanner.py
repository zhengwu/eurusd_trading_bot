"""Tier 1 — 30-minute scanner.

Orchestrates the full scan cycle:
  1. Fetch latest headlines
  2. Deduplicate against DedupCache
  3. Triage: score each headline 1-10
  4. Append all to intraday.jsonl
  5. If any score >= threshold AND cooldown not active: escalate to full analysis
"""
from __future__ import annotations

import threading
import time

import config
from pipeline.news_fetcher import fetch_news
from pipeline.dedup_cache import DedupCache
from triage.triage_prompt import triage_headlines
from triage.cooldown import is_cooling_down, set_cooldown
from triage.intraday_logger import append_scored_articles
from utils.logger import get_logger
from utils.date_utils import is_forex_market_open, today_str_utc

logger = get_logger(__name__)

_CACHE_PATH = config.DATA_DIR / ".dedup_cache"
_scan_lock = threading.Lock()


def _get_cache() -> DedupCache:
    return DedupCache(_CACHE_PATH)


def run_scan(force: bool = False) -> list[dict]:
    """
    Run one scan cycle. Returns list of items that triggered full analysis
    (empty list if no escalation occurred).

    force=True bypasses the market-hours check (used by Slack manual trigger).
    """
    if not force and not is_forex_market_open(
        config.MARKET_HOURS_START,
        config.MARKET_HOURS_END,
        config.MARKET_CLOSE_HOUR_EST,
        config.MARKET_OPEN_HOUR_EST,
    ):
        logger.info("Forex market closed — skipping scan")
        return []

    if not _scan_lock.acquire(blocking=False):
        logger.info("Scan already in progress — skipping")
        return []

    try:
        return _run_scan_locked()
    finally:
        _scan_lock.release()


def _run_scan_locked() -> list[dict]:
    """Inner scan logic — only called when _scan_lock is held."""
    logger.info("=== Tier 1 scan starting ===")

    # 1. Fetch
    try:
        raw_items = fetch_news()
        logger.info(f"Fetched {len(raw_items)} raw articles")
    except Exception as e:
        logger.error(f"News fetch failed: {e}")
        return []

    # 2. Deduplicate against already-processed articles
    cache = _get_cache()
    new_items = cache.filter_new(raw_items)
    if not new_items:
        logger.info("No new articles — scan complete")
        return []
    logger.info(f"{len(new_items)} new articles after dedup")

    # 3. Triage
    headlines = [item.get("title", "") for item in new_items]
    try:
        scored = triage_headlines(headlines)
    except Exception as e:
        logger.error(f"Triage failed: {e}")
        scored = [
            {"headline": h, "score": 5, "tag": "other", "reason": "triage error"}
            for h in headlines
        ]

    # Merge triage results back into the original items
    merged = []
    for item, score_result in zip(new_items, scored):
        merged.append({**item, **score_result, "triggered_full_analysis": False})

    # 4. Log all scored articles
    append_scored_articles(merged)

    # 5. Check for escalation
    high_score_items = [
        i for i in merged
        if (i.get("score") or 0) >= config.TRIAGE_SCORE_THRESHOLD
    ]
    if not high_score_items:
        logger.info(f"No articles scored >= {config.TRIAGE_SCORE_THRESHOLD} — no escalation")
        return []

    if is_cooling_down():
        logger.info(
            f"Cooldown active — suppressing escalation "
            f"({len(high_score_items)} high-score article(s) logged only)"
        )
        return []

    # 6. Escalate to full analysis
    set_cooldown()
    trigger = high_score_items[0]
    logger.info(
        f"Escalating to full analysis — trigger: [{trigger.get('score')}] "
        f"{trigger.get('headline') or trigger.get('title', '')}"
    )

    try:
        from analysis.context_builder import build_context
        from analysis.full_analysis_prompt import run_full_analysis
        from analysis.signal_formatter import format_signal
        from notifications.notifier import notify

        context = build_context(trigger_item=trigger)
        raw_signal = run_full_analysis(context)
        signal = format_signal(raw_signal)

        # Mark triggered items in the log
        for item in high_score_items:
            item["triggered_full_analysis"] = True

        # Save actionable signals to pending store for Job 3 approval
        if signal.get("signal") in ("Long", "Short"):
            from pipeline.signal_store import save_pending_signal, update_signal
            from agents.job3_executor import compute_order_preview
            preview = compute_order_preview(signal)
            if preview:
                signal["_order_preview"] = preview
            signal_id = save_pending_signal(signal, source="job1")
            signal["_signal_id"] = signal_id
            signal["_source"] = "job1"
            logger.info(f"Signal saved for approval: {signal_id}")

        notify(signal, trigger_item=trigger)
        logger.info(
            f"Full analysis complete: {signal.get('signal')} "
            f"[{signal.get('confidence')}] {signal.get('time_horizon')}"
        )
    except Exception as e:
        logger.error(f"Full analysis pipeline failed: {e}", exc_info=True)

    return high_score_items


def run_scanner_loop() -> None:
    """Run the Tier 1 scanner loop continuously during market hours."""
    logger.info(f"Scanner loop started — interval: {config.SCAN_INTERVAL_MINUTES} min")
    while True:
        try:
            run_scan()
        except Exception as e:
            logger.error(f"Scan cycle error: {e}", exc_info=True)
        sleep_secs = config.SCAN_INTERVAL_MINUTES * 60
        logger.info(f"Next scan in {config.SCAN_INTERVAL_MINUTES} min...")
        time.sleep(sleep_secs)
