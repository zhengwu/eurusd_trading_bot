"""Signal outcome tracker — records signals and fetches price outcomes at 8/16/24/48h.

Captures pre-debate and post-debate signals so we can measure whether the
ensemble debate improves or degrades the original Sonnet signal.

Each record stores:
  - Sonnet's signal BEFORE the debate (pre_debate_*)
  - Final signal AFTER the debate (post_debate_*)
  - Uncertainty score and debate scores
  - Whether the debate changed the signal
  - Actual price at 8h, 16h, 24h, 48h checkpoints

From this data you can answer:
  "Sonnet said Long → debate overrode to Wait → price moved +40 pips"  (bad debate call)
  "Sonnet said Long → debate kept Long → price dropped -30 pips"        (both wrong)
  "Sonnet said Long → debate overrode to Wait → price dropped -25 pips" (debate saved us)

Records stored in data/outcome_log.json (JSON list, rewritten on each update).
The background checker (run_outcome_checker_loop) polls every 30 minutes.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import config
from utils.logger import get_logger

logger = get_logger(__name__)

_LOG_PATH = config.DATA_DIR / "outcome_log.json"
_CHECKPOINTS_HOURS = [8, 16, 24, 48]
_CHECKER_INTERVAL_MINUTES = 30


# ── Storage helpers ────────────────────────────────────────────────────────────

def _load_log() -> list[dict]:
    if not _LOG_PATH.exists():
        return []
    try:
        return json.loads(_LOG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"outcome_tracker: failed to load log: {e}")
        return []


def _save_log(records: list[dict]) -> None:
    try:
        _LOG_PATH.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error(f"outcome_tracker: failed to save log: {e}")


# ── Price fetch ────────────────────────────────────────────────────────────────

def _fetch_current_price(symbol: str) -> float | None:
    """
    Fetch the current mid-price for symbol via MT5 tick data.
    Returns (bid + ask) / 2, or bid alone if ask is unavailable.
    Falls back gracefully if MT5 is not connected.
    """
    try:
        import MetaTrader5 as mt5
        from mt5.connector import is_connected
        if not is_connected():
            logger.warning(f"outcome_tracker: MT5 not connected — cannot fetch price for {symbol}")
            return None
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            logger.warning(f"outcome_tracker: no tick data for {symbol}")
            return None
        bid, ask = tick.bid, tick.ask
        if bid and ask:
            return round((bid + ask) / 2, 6)
        return bid or ask or None
    except Exception as e:
        logger.warning(f"outcome_tracker: MT5 price fetch failed for {symbol}: {e}")
        return None


# ── Record a signal ────────────────────────────────────────────────────────────

def record_signal(
    signal: dict[str, Any],
    symbol: str,
    entry_price: float | None,
    pre_debate_signal: str | None = None,
    pre_debate_confidence: str | None = None,
) -> None:
    """
    Record a signal at generation time for later outcome tracking.

    Call this AFTER run_ensemble_debate() has mutated the signal in-place so
    we capture both the pre-debate and post-debate state.

    Args:
        signal:                 The signal dict (already mutated by debate if applicable)
        symbol:                 Trading pair, e.g. "EURUSD"
        entry_price:            Live price at signal generation time (None for Wait signals)
        pre_debate_signal:      Sonnet's original direction before debate ran
        pre_debate_confidence:  Sonnet's original confidence before debate ran
    """
    signal_id = signal.get("_signal_id") or signal.get("id", "unknown")
    now = datetime.now(timezone.utc).isoformat()

    debate = signal.get("_debate") or {}
    evals  = debate.get("evaluations") or {}

    post_signal     = signal.get("signal", "Wait")
    post_confidence = signal.get("confidence")

    # Did the debate change the signal?
    debate_ran = bool(debate)
    pre_sig    = pre_debate_signal or post_signal  # if no debate, pre == post
    pre_conf   = pre_debate_confidence or post_confidence
    changed    = debate_ran and (pre_sig != post_signal)

    record: dict[str, Any] = {
        "signal_id":   signal_id,
        "symbol":      symbol,
        "created_at":  now,
        "entry_price": entry_price,

        # Sonnet's raw decision (before debate)
        "pre_debate_signal":     pre_sig,
        "pre_debate_confidence": pre_conf,

        # Final decision (after debate, or same as pre if no debate)
        "post_debate_signal":     post_signal,
        "post_debate_confidence": post_confidence,

        # Debate metadata
        "debate_ran":         debate_ran,
        "debate_changed":     changed,
        "uncertainty_score":  signal.get("_uncertainty_score"),
        "bull_score":         evals.get("bull_score"),
        "bear_score":         evals.get("bear_score"),
        "risk_level":         evals.get("risk_level"),
        "top_risk":           evals.get("top_risk"),
        "winning_case":       evals.get("winning_case"),
        "debate_final":       debate.get("final_decision"),

        # Trade levels
        "sl": signal.get("sl") or (signal.get("_order_preview") or {}).get("sl"),
        "tp": signal.get("tp") or (signal.get("_order_preview") or {}).get("tp"),

        # Outcomes — filled in by the checker thread
        "outcomes": {
            str(h): {
                "due_at":    (
                    datetime.now(timezone.utc) + timedelta(hours=h)
                ).isoformat(),
                "price":     None,
                "pip_move":  None,   # positive = price moved in signal direction
                "direction": None,   # "with" | "against" | "flat"
                "sl_hit":    None,
                "tp_hit":    None,
                "checked_at": None,
            }
            for h in _CHECKPOINTS_HOURS
        },
        "outcome_status": "pending",   # pending | partial | complete
    }

    records = _load_log()
    # Avoid duplicates — update if signal_id already exists
    idx = next((i for i, r in enumerate(records) if r.get("signal_id") == signal_id), None)
    if idx is not None:
        records[idx] = record
    else:
        records.append(record)
    _save_log(records)
    logger.info(
        f"outcome_tracker: recorded signal {signal_id} "
        f"[{pre_sig}→{post_signal}] uncertainty={signal.get('_uncertainty_score')} "
        f"entry={entry_price}"
    )


# ── Outcome checker ────────────────────────────────────────────────────────────

def _check_pending_outcomes() -> None:
    """Check all pending signals and fill in elapsed checkpoints."""
    records = _load_log()
    if not records:
        return

    now = datetime.now(timezone.utc)
    updated = 0

    for record in records:
        if record.get("outcome_status") == "complete":
            continue

        symbol     = record.get("symbol", config.MT5_SYMBOL)
        entry      = record.get("entry_price")
        direction  = record.get("post_debate_signal", "Wait")
        pip        = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["pip"]

        outcomes   = record.get("outcomes", {})
        any_filled = False

        for h_str, checkpoint in outcomes.items():
            if checkpoint.get("price") is not None:
                continue  # already filled

            due_at = checkpoint.get("due_at")
            if not due_at:
                continue
            due_dt = datetime.fromisoformat(due_at)
            if due_dt.tzinfo is None:
                due_dt = due_dt.replace(tzinfo=timezone.utc)

            if now < due_dt:
                continue  # not yet due

            # Fetch price now
            price = _fetch_current_price(symbol)
            if price is None:
                logger.warning(
                    f"outcome_tracker: could not fetch price for {symbol} "
                    f"at {h_str}h checkpoint — will retry next cycle"
                )
                continue

            checkpoint["price"]      = price
            checkpoint["checked_at"] = now.isoformat()

            if entry is not None:
                raw_move = price - entry
                pip_move = round(raw_move / pip, 1)

                # "with" means price moved in the direction of the signal
                if direction == "Long":
                    checkpoint["pip_move"]  = pip_move
                    checkpoint["direction"] = "with" if pip_move > 1 else ("against" if pip_move < -1 else "flat")
                elif direction == "Short":
                    checkpoint["pip_move"]  = -pip_move  # positive = price fell = good for Short
                    checkpoint["direction"] = "with" if pip_move < -1 else ("against" if pip_move > 1 else "flat")
                else:
                    checkpoint["pip_move"]  = pip_move
                    checkpoint["direction"] = "flat"

                # Check SL/TP hit (approximate — we only have discrete samples)
                sl = record.get("sl")
                tp = record.get("tp")
                if sl is not None:
                    try:
                        checkpoint["sl_hit"] = (
                            price <= float(sl) if direction == "Long"
                            else price >= float(sl)
                        )
                    except (TypeError, ValueError):
                        pass
                if tp is not None:
                    try:
                        checkpoint["tp_hit"] = (
                            price >= float(tp) if direction == "Long"
                            else price <= float(tp)
                        )
                    except (TypeError, ValueError):
                        pass

            any_filled = True
            logger.info(
                f"outcome_tracker: {record['signal_id']} [{symbol}] "
                f"{h_str}h checkpoint — price={price} pip_move={checkpoint.get('pip_move')}"
            )

        if any_filled:
            updated += 1

        # Update status
        all_filled   = all(c.get("price") is not None for c in outcomes.values())
        any_filled_r = any(c.get("price") is not None for c in outcomes.values())
        if all_filled:
            record["outcome_status"] = "complete"
        elif any_filled_r:
            record["outcome_status"] = "partial"

    if updated:
        _save_log(records)
        logger.info(f"outcome_tracker: updated {updated} record(s)")


def run_outcome_checker_loop() -> None:
    """
    Background thread — checks pending signal outcomes every 30 minutes.
    Call this as a daemon thread from job1_opportunity.py.
    """
    logger.info(
        f"Outcome checker started "
        f"(checkpoints: {_CHECKPOINTS_HOURS}h, interval: {_CHECKER_INTERVAL_MINUTES} min)"
    )
    while True:
        try:
            _check_pending_outcomes()
        except Exception as e:
            logger.error(f"Outcome checker error: {e}", exc_info=True)
        time.sleep(_CHECKER_INTERVAL_MINUTES * 60)


# ── Summary helper ─────────────────────────────────────────────────────────────

def get_outcome_summary() -> dict[str, Any]:
    """
    Return a human-readable summary of tracked outcomes.
    Useful for Slack /outcomes command or manual inspection.
    """
    records = _load_log()
    if not records:
        return {"total": 0, "message": "No signals recorded yet."}

    total        = len(records)
    complete     = sum(1 for r in records if r.get("outcome_status") == "complete")
    pending      = sum(1 for r in records if r.get("outcome_status") == "pending")
    partial      = total - complete - pending

    debate_changed = sum(1 for r in records if r.get("debate_changed"))
    overrides_to_wait = sum(
        1 for r in records
        if r.get("debate_changed")
        and r.get("pre_debate_signal") in ("Long", "Short")
        and r.get("post_debate_signal") == "Wait"
    )

    # Accuracy at 24h for completed records where debate changed signal to Wait
    false_negatives = []  # debate → Wait but price moved in original direction
    false_positives = []  # debate kept Long/Short but price moved against
    for r in records:
        c24 = r.get("outcomes", {}).get("24", {})
        if c24.get("price") is None:
            continue
        pip_move = c24.get("pip_move")
        if pip_move is None:
            continue

        if r.get("debate_changed") and r.get("post_debate_signal") == "Wait":
            # Was the original signal right?
            pre = r.get("pre_debate_signal")
            if pre in ("Long", "Short") and abs(pip_move) >= 10:
                if c24.get("direction") == "with":
                    false_negatives.append({"id": r["signal_id"], "pip_move": pip_move})
        elif r.get("post_debate_signal") in ("Long", "Short"):
            if c24.get("direction") == "against" and abs(pip_move) >= 10:
                false_positives.append({"id": r["signal_id"], "pip_move": pip_move})

    return {
        "total":             total,
        "complete":          complete,
        "partial":           partial,
        "pending":           pending,
        "debate_changed":    debate_changed,
        "overrides_to_wait": overrides_to_wait,
        "false_negatives_24h": false_negatives,   # debate blocked a good signal
        "false_positives_24h": false_positives,   # debate let through a bad signal
    }
