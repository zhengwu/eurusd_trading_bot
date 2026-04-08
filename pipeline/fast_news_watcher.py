"""Fast news watcher — polls RSS feeds every 2 minutes for breaking headlines.

Runs as a background thread alongside the 30-min scanner. Only uses the two
lowest-latency sources (ForexLive + FXStreet RSS) since speed is the priority.

When a high-score article is found:
  1. Immediately triggers full analysis (bypasses 30-min scan wait)
  2. Uses a separate short cooldown (FAST_COOLDOWN_MINUTES) per pair
  3. Marks the article in intraday.jsonl as triggered

The 30-min scanner continues to run unchanged and handles slower sources.
DedupCache is shared — articles processed here won't re-appear in the main scan.
"""
from __future__ import annotations

import time
import threading
from datetime import datetime, timezone

import config
from pipeline.news_fetcher import fetch_rss
from pipeline.dedup_cache import DedupCache
from triage.triage_prompt import triage_headlines
from triage.intraday_logger import append_scored_articles
from utils.logger import get_logger
from utils.date_utils import is_forex_market_open, today_str_utc

logger = get_logger(__name__)

# Separate cooldown files so fast-watcher and main scanner don't block each other
_FAST_COOLDOWN_KEY = "fast_{symbol}"


def _fast_cooldown_path(symbol: str):
    return config.DATA_DIR / f".cooldown_fast_{symbol}"


def _is_fast_cooling(symbol: str) -> bool:
    lock = _fast_cooldown_path(symbol)
    if not lock.exists():
        return False
    try:
        from datetime import timedelta
        ts = datetime.fromisoformat(lock.read_text(encoding="utf-8").strip())
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        expiry = ts + timedelta(minutes=config.FAST_COOLDOWN_MINUTES)
        if datetime.now(timezone.utc) < expiry:
            remaining = int((expiry - datetime.now(timezone.utc)).total_seconds() / 60)
            logger.debug(f"Fast cooldown active [{symbol}] — {remaining} min remaining")
            return True
        return False
    except Exception:
        return False


def _set_fast_cooldown(symbol: str) -> None:
    lock = _fast_cooldown_path(symbol)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")


def _dedup_cache_path(symbol: str):
    # Share the same dedup cache as the main scanner so no article is processed twice
    return config.DATA_DIR / f".dedup_cache_{symbol}"


def _run_fast_cycle() -> None:
    """One poll cycle across all active pairs."""
    if not is_forex_market_open(
        config.MARKET_HOURS_START,
        config.MARKET_HOURS_END,
        config.MARKET_CLOSE_HOUR_EST,
        config.MARKET_OPEN_HOUR_EST,
    ):
        return

    today = datetime.now(timezone.utc).date()
    from datetime import timedelta
    start = today - timedelta(days=0)  # today only — RSS is real-time, no need for yesterday

    for symbol in config.ACTIVE_PAIRS:
        try:
            _run_fast_pair(symbol, start, today)
        except Exception as e:
            logger.error(f"Fast watcher cycle failed [{symbol}]: {e}", exc_info=True)


def _run_fast_pair(symbol: str, start, today) -> None:
    # Fetch RSS only (fastest source)
    try:
        raw_items = fetch_rss(start, today, max_items=20, symbol=symbol)
    except Exception as e:
        logger.debug(f"Fast watcher RSS fetch failed [{symbol}]: {e}")
        return

    if not raw_items:
        return

    # Deduplicate against shared cache (same cache as main scanner)
    cache = DedupCache(_dedup_cache_path(symbol))
    new_items = cache.filter_new(raw_items)
    if not new_items:
        return

    logger.info(f"[FastWatch:{symbol}] {len(new_items)} new RSS articles")

    # Triage
    headlines = [item.get("title", "") for item in new_items]
    try:
        scored = triage_headlines(headlines, symbol=symbol)
    except Exception as e:
        logger.error(f"[FastWatch:{symbol}] Triage failed: {e}")
        scored = [
            {"headline": h, "score": 5, "tag": "other", "reason": "triage error"}
            for h in headlines
        ]

    merged = []
    for item, score_result in zip(new_items, scored):
        merged.append({**item, **score_result, "symbol": symbol, "triggered_full_analysis": False})

    # Log all scored articles to intraday.jsonl
    append_scored_articles(merged)

    # Check for high-score articles
    high_score = [i for i in merged if (i.get("score") or 0) >= config.TRIAGE_SCORE_THRESHOLD]
    if not high_score:
        return

    if _is_fast_cooling(symbol):
        logger.info(
            f"[FastWatch:{symbol}] Fast cooldown active — "
            f"{len(high_score)} high-score article(s) logged only"
        )
        return

    # Fire full analysis immediately
    _set_fast_cooldown(symbol)
    trigger = high_score[0]
    logger.info(
        f"[FastWatch:{symbol}] BREAKING — immediate escalation: "
        f"[{trigger.get('score')}] {trigger.get('headline') or trigger.get('title', '')}"
    )

    try:
        from analysis.context_builder import build_context
        from analysis.full_analysis_prompt import run_full_analysis
        from analysis.signal_formatter import format_signal
        from notifications.notifier import notify

        context = build_context(symbol=symbol, trigger_item=trigger)
        raw_signal = run_full_analysis(context, symbol=symbol)
        signal = format_signal(raw_signal)
        signal["_symbol"] = symbol
        signal["_source"] = "fast_watch"

        for item in high_score:
            item["triggered_full_analysis"] = True

        if signal.get("signal") in ("Long", "Short"):
            from pipeline.signal_store import save_pending_signal
            from agents.job3_executor import compute_order_preview

            preview = compute_order_preview(signal, symbol=symbol)
            if preview:
                signal["_order_preview"] = preview

            if config.USE_MULTI_AGENT_DEBATE:
                try:
                    from analysis.ensemble_agent import run_ensemble_debate
                    run_ensemble_debate(context, signal, symbol=symbol)
                    if signal.get("sl") or signal.get("tp"):
                        updated = compute_order_preview(signal, symbol=symbol)
                        if updated:
                            signal["_order_preview"] = updated
                except Exception as e:
                    logger.error(f"[FastWatch:{symbol}] Debate failed: {e}", exc_info=True)

            signal_id = save_pending_signal(signal, source="fast_watch")
            signal["_signal_id"] = signal_id

        notify(signal, trigger_item=trigger)
        logger.info(
            f"[FastWatch:{symbol}] Analysis complete: {signal.get('signal')} "
            f"[{signal.get('confidence')}]"
        )
    except Exception as e:
        logger.error(f"[FastWatch:{symbol}] Full analysis failed: {e}", exc_info=True)


def run_fast_watcher_loop() -> None:
    """Blocking loop — run in a background daemon thread."""
    interval = config.FAST_WATCH_INTERVAL_MINUTES * 60
    logger.info(
        f"Fast news watcher started — pairs: {config.ACTIVE_PAIRS}, "
        f"interval: {config.FAST_WATCH_INTERVAL_MINUTES} min (RSS only)"
    )
    while True:
        try:
            _run_fast_cycle()
        except Exception as e:
            logger.error(f"Fast watcher loop error: {e}", exc_info=True)
        time.sleep(interval)
