"""Per-symbol cooldown lock — suppresses re-trigger of full analysis for COOLDOWN_MINUTES.

Backed by timestamp files: data/.cooldown_SYMBOL (one per pair).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from utils.logger import get_logger

logger = get_logger(__name__)


def _lock_file(symbol: str) -> Path:
    return config.DATA_DIR / f".cooldown_{symbol}"


def is_cooling_down(symbol: str | None = None) -> bool:
    """Return True if the cooldown window is still active for the given symbol."""
    sym = symbol or config.MT5_SYMBOL
    lock = _lock_file(sym)
    if not lock.exists():
        return False
    try:
        ts_str = lock.read_text(encoding="utf-8").strip()
        lock_time = datetime.fromisoformat(ts_str)
        if lock_time.tzinfo is None:
            lock_time = lock_time.replace(tzinfo=timezone.utc)
        expiry = lock_time + timedelta(minutes=config.COOLDOWN_MINUTES)
        now = datetime.now(timezone.utc)
        if now < expiry:
            remaining = int((expiry - now).total_seconds() / 60)
            logger.debug(f"Cooldown active [{sym}] — {remaining} min remaining")
            return True
        return False
    except Exception as e:
        logger.warning(f"Cooldown check failed [{sym}]: {e}")
        return False


def set_cooldown(symbol: str | None = None) -> None:
    """Set the cooldown lock to now for the given symbol."""
    sym = symbol or config.MT5_SYMBOL
    lock = _lock_file(sym)
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        logger.info(f"Cooldown set [{sym}] for {config.COOLDOWN_MINUTES} minutes")
    except Exception as e:
        logger.error(f"Failed to set cooldown [{sym}]: {e}")


def clear_cooldown(symbol: str | None = None) -> None:
    """Remove the cooldown lock for the given symbol (e.g. for testing)."""
    sym = symbol or config.MT5_SYMBOL
    lock = _lock_file(sym)
    try:
        if lock.exists():
            lock.unlink()
    except Exception as e:
        logger.warning(f"Failed to clear cooldown [{sym}]: {e}")
