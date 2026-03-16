"""Cooldown lock — suppresses re-trigger of full analysis for COOLDOWN_MINUTES.

Backed by a timestamp file: data/.cooldown_lock
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from utils.logger import get_logger

logger = get_logger(__name__)

_LOCK_FILE = config.DATA_DIR / ".cooldown_lock"


def is_cooling_down() -> bool:
    """Return True if the cooldown window is still active."""
    if not _LOCK_FILE.exists():
        return False
    try:
        ts_str = _LOCK_FILE.read_text(encoding="utf-8").strip()
        lock_time = datetime.fromisoformat(ts_str)
        if lock_time.tzinfo is None:
            lock_time = lock_time.replace(tzinfo=timezone.utc)
        expiry = lock_time + timedelta(minutes=config.COOLDOWN_MINUTES)
        now = datetime.now(timezone.utc)
        if now < expiry:
            remaining = int((expiry - now).total_seconds() / 60)
            logger.debug(f"Cooldown active — {remaining} min remaining")
            return True
        return False
    except Exception as e:
        logger.warning(f"Cooldown check failed: {e}")
        return False


def set_cooldown() -> None:
    """Set the cooldown lock to now."""
    try:
        _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        _LOCK_FILE.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        logger.info(f"Cooldown set for {config.COOLDOWN_MINUTES} minutes")
    except Exception as e:
        logger.error(f"Failed to set cooldown: {e}")


def clear_cooldown() -> None:
    """Remove the cooldown lock (e.g. for testing)."""
    try:
        if _LOCK_FILE.exists():
            _LOCK_FILE.unlink()
    except Exception as e:
        logger.warning(f"Failed to clear cooldown: {e}")
