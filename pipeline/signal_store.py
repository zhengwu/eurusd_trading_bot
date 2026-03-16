"""Pending signal store — shared queue for Job 1, Job 2, and Job 3.

Job 1 and Job 2 save signals here after generating them.
Job 3 reads approved signals and executes them.

Approval flow:
  1. Signal saved → status="pending" → Slack alert includes signal ID
  2. User approves via CLI: python -m agents.job3_executor --approve <ID>
  3. Job 3 executes → status="executed"
  4. (Future) Slack two-way bot handles approval in-channel

Storage: data/pending_signals.json (human-readable, easy to inspect)
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import config
from utils.logger import get_logger

logger = get_logger(__name__)

_STORE_PATH = config.DATA_DIR / "pending_signals.json"
_lock = threading.Lock()


# ── internal helpers ──────────────────────────────────────────────────────────

def _load() -> list[dict]:
    if not _STORE_PATH.exists():
        return []
    try:
        return json.loads(_STORE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Signal store load failed: {e}")
        return []


def _save(signals: list[dict]) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _STORE_PATH.write_text(
        json.dumps(signals, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _is_expired(signal: dict) -> bool:
    created_at = signal.get("created_at")
    if not created_at:
        return False
    try:
        age_min = (
            datetime.now(timezone.utc) - datetime.fromisoformat(created_at)
        ).total_seconds() / 60
        return age_min > config.JOB3_SIGNAL_EXPIRY_MINUTES
    except Exception:
        return False


# ── public API ────────────────────────────────────────────────────────────────

def save_pending_signal(signal: dict[str, Any], source: str) -> str:
    """
    Save a new signal as pending. Returns the short signal ID (8 chars).
    source: "job1", "job2", or "job4"
    """
    with _lock:
        signal_id = str(uuid.uuid4())[:8].upper()
        signals = _load()
        for s in signals:
            if s.get("status") == "pending" and _is_expired(s):
                s["status"] = "expired"
        entry = {
            **signal,
            "id":               signal_id,
            "source":           source,
            "status":           "pending",
            "created_at":       datetime.now(timezone.utc).isoformat(),
            "approved_at":      None,
            "rejected_at":      None,
            "executed_at":      None,
            "execution_result": None,
        }
        signals.append(entry)
        _save(signals)
        logger.info(f"Signal saved: [{signal_id}] {source} → {signal.get('signal')} [{signal.get('confidence')}]")
        return signal_id


def get_pending_signals() -> list[dict]:
    """Return all non-expired pending signals."""
    with _lock:
        return [
            s for s in _load()
            if s.get("status") == "pending" and not _is_expired(s)
        ]


def get_signal_by_id(signal_id: str) -> dict | None:
    with _lock:
        for s in _load():
            if s.get("id", "").upper() == signal_id.upper():
                return s
    return None


def approve_signal(signal_id: str) -> bool:
    with _lock:
        signals = _load()
        for s in signals:
            if s.get("id", "").upper() == signal_id.upper():
                if s.get("status") != "pending":
                    logger.warning(f"Signal {signal_id} is not pending (status={s.get('status')})")
                    return False
                s["status"] = "approved"
                s["approved_at"] = datetime.now(timezone.utc).isoformat()
                _save(signals)
                logger.info(f"Signal {signal_id} approved")
                return True
        logger.warning(f"Signal {signal_id} not found")
        return False


def reject_signal(signal_id: str) -> bool:
    with _lock:
        signals = _load()
        for s in signals:
            if s.get("id", "").upper() == signal_id.upper():
                s["status"] = "rejected"
                s["rejected_at"] = datetime.now(timezone.utc).isoformat()
                _save(signals)
                logger.info(f"Signal {signal_id} rejected")
                return True
        return False


def mark_executed(signal_id: str, execution_result: dict) -> None:
    with _lock:
        signals = _load()
        for s in signals:
            if s.get("id", "").upper() == signal_id.upper():
                s["status"] = "executed"
                s["executed_at"] = datetime.now(timezone.utc).isoformat()
                s["execution_result"] = execution_result
                _save(signals)
                return


def expire_old_signals() -> int:
    """Mark expired pending signals. Returns count expired."""
    with _lock:
        signals = _load()
        count = 0
        for s in signals:
            if s.get("status") == "pending" and _is_expired(s):
                s["status"] = "expired"
                count += 1
        if count:
            _save(signals)
            logger.info(f"Expired {count} pending signal(s)")
        return count


def cleanup_old_signals(keep_days: int = 7) -> int:
    """Remove executed/expired/rejected signals older than keep_days. Returns count removed."""
    signals = _load()
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    terminal = {"executed", "expired", "rejected"}
    kept = []
    removed = 0
    for s in signals:
        if s.get("status") in terminal:
            created = s.get("created_at")
            if created:
                try:
                    age = datetime.fromisoformat(created)
                    if age.tzinfo is None:
                        age = age.replace(tzinfo=timezone.utc)
                    if age < cutoff:
                        removed += 1
                        continue
                except Exception:
                    pass
        kept.append(s)
    if removed:
        _save(kept)
        logger.info(f"Cleaned up {removed} old signal(s)")
    return removed


def list_signals(status: str | None = None) -> list[dict]:
    """List all signals, optionally filtered by status."""
    signals = _load()
    if status:
        return [s for s in signals if s.get("status") == status]
    return signals
