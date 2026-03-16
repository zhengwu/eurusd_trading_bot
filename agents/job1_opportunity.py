"""Job 1 — Opportunity Scanner agent runner.

Entry point for the full system:
  - Runs the Tier 1 30-minute scanner loop (blocking, main thread)
  - Runs the Tier 2 end-of-day collector at 22:00 UTC (background thread)
  - Runs the Job 2 position monitor loop (background thread, requires MT5)
  - Runs the Slack Socket Mode bot (background thread, optional)

Usage:
    conda activate mt5_env
    cd C:\\Users\\zz10c\\Documents\\eurusd_agent
    python -m agents.job1_opportunity
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

import config
from triage.scanner import run_scanner_loop
from pipeline.daily_collector import run_daily_collection
from utils.logger import get_logger
from utils.date_utils import today_str_utc
from mt5.connector import connect as mt5_connect, disconnect as mt5_disconnect

logger = get_logger(__name__)


def _daily_collection_thread() -> None:
    """
    Background thread: checks every minute if it's time for end-of-day
    collection (config.DAILY_COLLECTION_TIME_UTC). Runs at most once per day.
    """
    target_hour, target_min = map(int, config.DAILY_COLLECTION_TIME_UTC.split(":"))
    last_run_date: str | None = None

    while True:
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        if (
            now.hour == target_hour
            and now.minute == target_min
            and last_run_date != today
        ):
            logger.info("Triggering end-of-day collection...")
            try:
                run_daily_collection(today)
                last_run_date = today
                from pipeline.signal_store import cleanup_old_signals
                cleanup_old_signals()
            except Exception as e:
                logger.error(f"Daily collection failed: {e}", exc_info=True)
        time.sleep(60)


def _job2_thread() -> None:
    """Background thread: connect to MT5 and run Job 2 position monitor loop."""
    from agents.job2_position import run_job2_loop

    logger.info("Job 2: connecting to MT5...")
    if not mt5_connect():
        logger.warning("Job 2: MT5 connection failed — position monitor will not run")
        return
    try:
        run_job2_loop()
    except Exception as e:
        logger.error(f"Job 2 thread crashed: {e}", exc_info=True)
    finally:
        mt5_disconnect()


def main() -> None:
    logger.info("=" * 60)
    logger.info("EUR/USD Opportunity Scanner — Job 1")
    logger.info(f"  Triage model  : {config.TRIAGE_MODEL}")
    logger.info(f"  Analysis model: {config.ANALYSIS_MODEL}")
    logger.info(f"  Scan interval : {config.SCAN_INTERVAL_MINUTES} min")
    logger.info(
        f"  Market hours  : {config.MARKET_HOURS_START}:00 – "
        f"{config.MARKET_HOURS_END}:00 UTC"
    )
    logger.info(f"  Daily collect : {config.DAILY_COLLECTION_TIME_UTC} UTC")
    logger.info(f"  Job 2 interval: {config.JOB2_CHECK_INTERVAL_MINUTES} min")
    logger.info(f"  Notifications : {config.NOTIFICATION_CHANNELS}")
    logger.info(f"  Alert email   : {config.NOTIFICATION_EMAIL_TO}")
    logger.info("=" * 60)

    # Start daily collection scheduler in background thread
    dc_thread = threading.Thread(
        target=_daily_collection_thread,
        daemon=True,
        name="daily-collector",
    )
    dc_thread.start()
    logger.info("Daily collection scheduler running in background")

    # Start Job 2 position monitor in background thread
    j2_thread = threading.Thread(
        target=_job2_thread,
        daemon=True,
        name="job2-position",
    )
    j2_thread.start()
    logger.info("Job 2 position monitor starting in background")

    # Start Slack bot in background thread (no-ops if tokens not set)
    from agents.slack_bot import run_slack_bot
    bot_thread = threading.Thread(
        target=run_slack_bot,
        daemon=True,
        name="slack-bot",
    )
    bot_thread.start()
    logger.info("Slack bot starting in background")

    # Run Tier 1 scanner loop (blocks forever)
    run_scanner_loop()


if __name__ == "__main__":
    main()
