"""Tier 1 — 30-minute scanner.

Orchestrates the full scan cycle for every pair in config.ACTIVE_PAIRS:
  1. Fetch latest headlines
  2. Deduplicate against per-pair DedupCache
  3. Triage: score each headline 1-10 for the given pair
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

_scan_lock = threading.Lock()


def _cache_path(symbol: str):
    return config.DATA_DIR / f".dedup_cache_{symbol}"


def _refresh_events() -> None:
    """
    Refresh today's events.json with the latest actuals from ForexFactory.

    ForexFactory updates actual values as releases happen throughout the day,
    so refreshing each scan cycle ensures Claude sees real-time beat/miss data
    rather than stale forecasts from the 22:00 UTC daily collection.
    """
    try:
        import json
        from pipeline.event_fetcher import fetch_forexfactory_calendar
        today = today_str_utc()
        day_dir = config.DATA_DIR / today
        day_dir.mkdir(parents=True, exist_ok=True)
        events = fetch_forexfactory_calendar()
        (day_dir / "events.json").write_text(
            json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.debug(f"Events refreshed: {len(events)} entries")
    except Exception as e:
        logger.warning(f"Event refresh failed: {e}")


def run_scan(force: bool = False) -> list[dict]:
    """
    Run one scan cycle across all ACTIVE_PAIRS.
    Returns list of items that triggered full analysis.
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
        # Refresh economic calendar so actuals are current for this cycle
        _refresh_events()

        # Run pre-event checks (briefs + post-event triggers) for each pair
        for symbol in config.ACTIVE_PAIRS:
            try:
                from pipeline.pre_event_agent import run_pre_event_check
                run_pre_event_check(symbol)
            except Exception as e:
                logger.error(f"Pre-event check failed for {symbol}: {e}", exc_info=True)

        all_triggered = []
        for symbol in config.ACTIVE_PAIRS:
            try:
                triggered = _run_pair_scan(symbol)
                all_triggered.extend(triggered)
            except Exception as e:
                logger.error(f"Scan failed for {symbol}: {e}", exc_info=True)
        return all_triggered
    finally:
        _scan_lock.release()


def run_scan_pair(symbol: str, force: bool = False) -> list[dict]:
    """
    Run one scan cycle for a single pair.
    Used by Slack bot for manual per-pair scans.
    """
    if not force and not is_forex_market_open(
        config.MARKET_HOURS_START,
        config.MARKET_HOURS_END,
        config.MARKET_CLOSE_HOUR_EST,
        config.MARKET_OPEN_HOUR_EST,
    ):
        logger.info("Forex market closed — skipping scan")
        return []
    return _run_pair_scan(symbol)


def _handle_cb_updates(merged: list[dict]) -> None:
    """
    For every CB-tagged item in the current scan batch, trigger a policy
    update for the identified central bank. Runs in a background thread
    so it does not block the scan cycle.
    Dedup inside cb_policy_updater prevents re-fetching the same bank twice per day.
    """
    banks_seen: set[str] = set()
    for item in merged:
        tag     = item.get("tag", "")
        cb_bank = item.get("cb_bank")
        if tag in ("cb_decision", "CB_speech") and cb_bank and cb_bank not in banks_seen:
            banks_seen.add(cb_bank)
            logger.info(f"CB headline detected [{tag}] -> scheduling policy update for {cb_bank}")
            t = threading.Thread(
                target=_run_cb_update,
                args=(cb_bank,),
                daemon=True,
            )
            t.start()


def _auto_approve_signal(signal_id: str, symbol: str, uncertainty: int) -> None:
    """
    Background thread: connect MT5 and auto-execute a low-uncertainty signal.
    Sends a Slack follow-up with the execution result.
    """
    import os, requests
    from agents.job3_executor import approve_and_execute
    from mt5.connector import connect as mt5_connect, is_connected

    logger.info(f"[{symbol}] Auto-approving signal {signal_id} (uncertainty={uncertainty})")

    if not is_connected() and not mt5_connect():
        logger.warning(f"[{symbol}] Auto-approve: MT5 not connected — {signal_id} approved but not executed")
        _slack_auto(f":warning: *Auto-approve failed* — MT5 not connected\nSignal `{signal_id}` approved but awaiting manual execution.")
        return

    result = approve_and_execute(signal_id)

    if result.get("ok"):
        ticket = result.get("ticket")
        price  = result.get("price")
        logger.info(f"[{symbol}] Auto-executed {signal_id}: ticket={ticket} price={price}")
        _slack_auto(
            f":robot_face: *Auto-executed* [{symbol}] `{signal_id}`\n"
            f"Ticket: `{ticket}` | Entry: `{price}` | Uncertainty: `{uncertainty}/100`"
        )
    else:
        err = result.get("error", "unknown error")
        logger.warning(f"[{symbol}] Auto-approve execution failed: {err}")
        _slack_auto(
            f":x: *Auto-execute failed* [{symbol}] `{signal_id}`\n"
            f"Reason: {err}\nSignal remains pending — approve manually."
        )


def _slack_auto(text: str) -> None:
    """Send a plain Slack webhook message for auto-approval notifications."""
    import os, requests
    url = os.getenv("SLACK_WEBHOOK_URL", "")
    if not url:
        return
    try:
        requests.post(url, json={"text": text}, timeout=10)
    except Exception as e:
        logger.warning(f"Auto-approve Slack notification failed: {e}")


def _run_cb_update(bank: str) -> None:
    try:
        from pipeline.cb_policy_updater import update_bank_policy
        update_bank_policy(bank)
    except Exception as e:
        logger.error(f"CB policy update failed for {bank}: {e}", exc_info=True)


def _run_pair_scan(symbol: str) -> list[dict]:
    """Run a full scan cycle for one pair. Lock must be held by caller."""
    display = config.PAIRS.get(symbol, {}).get("display", symbol)
    logger.info(f"=== Tier 1 scan starting [{display}] ===")

    # 1. Fetch
    try:
        raw_items = fetch_news(symbol=symbol)
        logger.info(f"[{symbol}] Fetched {len(raw_items)} raw articles")
    except Exception as e:
        logger.error(f"[{symbol}] News fetch failed: {e}")
        return []

    # 2. Deduplicate (per-pair cache)
    cache = DedupCache(_cache_path(symbol))
    new_items = cache.filter_new(raw_items)
    if not new_items:
        logger.info(f"[{symbol}] No new articles — scan complete")
        return []
    logger.info(f"[{symbol}] {len(new_items)} new articles after dedup")

    # 3. Triage
    headlines = [item.get("title", "") for item in new_items]
    try:
        scored = triage_headlines(headlines, symbol=symbol)
    except Exception as e:
        logger.error(f"[{symbol}] Triage failed: {e}")
        scored = [
            {"headline": h, "score": 5, "tag": "other", "reason": "triage error"}
            for h in headlines
        ]

    # Merge triage results back into original items
    merged = []
    for item, score_result in zip(new_items, scored):
        merged.append({**item, **score_result, "symbol": symbol, "triggered_full_analysis": False})

    # 4. Log all scored articles
    append_scored_articles(merged)

    # 4b. CB policy update — trigger on any CB headline regardless of score
    _handle_cb_updates(merged)

    # 5. Check for escalation
    high_score_items = [
        i for i in merged
        if (i.get("score") or 0) >= config.TRIAGE_SCORE_THRESHOLD
    ]
    if not high_score_items:
        logger.info(f"[{symbol}] No articles scored >= {config.TRIAGE_SCORE_THRESHOLD} — no escalation")
        return []

    if is_cooling_down(symbol):
        logger.info(
            f"[{symbol}] Cooldown active — suppressing escalation "
            f"({len(high_score_items)} high-score article(s) logged only)"
        )
        return []

    # 6. Escalate to full analysis
    set_cooldown(symbol)
    trigger = high_score_items[0]
    logger.info(
        f"[{symbol}] Escalating to full analysis — trigger: [{trigger.get('score')}] "
        f"{trigger.get('headline') or trigger.get('title', '')}"
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

        # Mark triggered items in the log
        for item in high_score_items:
            item["triggered_full_analysis"] = True

        # Save actionable signals to pending store for Job 3 approval
        if signal.get("signal") in ("Long", "Short"):
            from pipeline.signal_store import save_pending_signal, update_signal
            from agents.job3_executor import compute_order_preview

            # Compute preview before debate so judge can see entry/SL/TP
            preview = compute_order_preview(signal, symbol=symbol)
            if preview:
                signal["_order_preview"] = preview

            # Snapshot Sonnet's decision before the debate may override it
            _pre_debate_signal     = signal.get("signal")
            _pre_debate_confidence = signal.get("confidence")

            # Run ensemble debate (mutates signal in-place)
            if config.USE_MULTI_AGENT_DEBATE:
                try:
                    from analysis.ensemble_agent import run_ensemble_debate
                    run_ensemble_debate(context, signal, symbol=symbol)
                except Exception as e:
                    logger.error(f"[{symbol}] Ensemble debate failed: {e}", exc_info=True)

            # Recommend position size based on remaining portfolio risk + uncertainty
            try:
                from mt5.risk_manager import recommend_position_size
                from mt5.position_reader import get_account_summary
                from mt5.connector import is_connected
                if is_connected():
                    acct    = get_account_summary()
                    equity  = acct.get("equity") or 0.0
                    sl_pips = (preview or {}).get("sl_pips") or config.JOB3_DEFAULT_SL_PIPS
                    unc     = signal.get("uncertainty_score") or signal.get("_uncertainty_score")
                    sizing  = recommend_position_size(unc, equity, sl_pips, symbol=symbol)
                    signal["_lot_override"]         = sizing["lot"]
                    signal["_recommended_risk_pct"] = sizing["risk_pct"]
                    signal["_size_reason"]          = sizing["reason"]
            except Exception as e:
                logger.warning(f"[{symbol}] Position sizing recommendation failed (non-fatal): {e}")

            # Recompute preview with recommended lot (and any judge-adjusted SL/TP)
            updated_preview = compute_order_preview(signal, symbol=symbol)
            if updated_preview:
                signal["_order_preview"] = updated_preview

            signal_id = save_pending_signal(signal, source="job1")
            signal["_signal_id"] = signal_id
            signal["_source"] = "job1"
            logger.info(f"[{symbol}] Signal saved for approval: {signal_id}")

            # Record signal + debate metadata for outcome tracking
            if config.DEBATE_OUTCOME_TRACKING:
                try:
                    from analysis.outcome_tracker import record_signal
                    entry_price = (signal.get("_order_preview") or {}).get("entry_price")
                    record_signal(
                        signal=signal,
                        symbol=symbol,
                        entry_price=entry_price,
                        pre_debate_signal=_pre_debate_signal,
                        pre_debate_confidence=_pre_debate_confidence,
                    )
                except Exception as e:
                    logger.warning(f"[{symbol}] Outcome tracking failed (non-fatal): {e}")

        # Record Wait signals for outcome tracking (no debate runs for Wait)
        # Guard: skip if _signal_id already set — means the Long/Short block already
        # recorded this signal and debate downgraded it to Wait (overwrite bug prevention)
        if config.DEBATE_OUTCOME_TRACKING and signal.get("signal") == "Wait" and not signal.get("_signal_id"):
            try:
                from analysis.outcome_tracker import record_signal
                from mt5.connector import is_connected
                import MetaTrader5 as _mt5
                if is_connected():
                    _tick = _mt5.symbol_info_tick(symbol)
                    entry_price = round((_tick.bid + _tick.ask) / 2, 6) if _tick else None
                else:
                    entry_price = None
                record_signal(
                    signal=signal,
                    symbol=symbol,
                    entry_price=entry_price,
                    pre_debate_signal="Wait",
                    pre_debate_confidence=signal.get("confidence"),
                )
            except Exception as e:
                logger.warning(f"[{symbol}] Outcome tracking (Wait) failed (non-fatal): {e}")

        notify(signal, trigger_item=trigger)
        wait_reason = signal.get("wait_reason", "")
        logger.info(
            f"[{symbol}] Full analysis complete: {signal.get('signal')} "
            f"[{signal.get('confidence')}] {signal.get('time_horizon')}"
            + (f" | wait_reason={wait_reason}" if wait_reason else "")
        )

        # Auto-approve high-conviction Long/Short signals
        _auto_threshold = config.JOB3_AUTO_APPROVE_MAX_UNCERTAINTY
        if (
            _auto_threshold is not None
            and signal.get("signal") in ("Long", "Short")
            and signal.get("_signal_id")
        ):
            unc = signal.get("uncertainty_score")
            if unc is not None and unc <= _auto_threshold:
                logger.info(
                    f"[{symbol}] Uncertainty {unc} <= {_auto_threshold} — "
                    f"auto-approving signal {signal['_signal_id']}"
                )
                t = threading.Thread(
                    target=_auto_approve_signal,
                    args=(signal["_signal_id"], symbol, unc),
                    daemon=True,
                    name=f"auto-approve-{signal['_signal_id']}",
                )
                t.start()
    except Exception as e:
        logger.error(f"[{symbol}] Full analysis pipeline failed: {e}", exc_info=True)

    return high_score_items


def run_scanner_loop() -> None:
    """Run the Tier 1 scanner loop continuously during market hours."""
    logger.info(
        f"Scanner loop started — pairs: {config.ACTIVE_PAIRS}, "
        f"interval: {config.SCAN_INTERVAL_MINUTES} min"
    )
    while True:
        try:
            run_scan()
        except Exception as e:
            logger.error(f"Scan cycle error: {e}", exc_info=True)
        sleep_secs = config.SCAN_INTERVAL_MINUTES * 60
        logger.info(f"Next scan in {config.SCAN_INTERVAL_MINUTES} min...")
        time.sleep(sleep_secs)
