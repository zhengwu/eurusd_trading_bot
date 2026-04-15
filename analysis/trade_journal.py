"""Trade journal — records signal recommendations and validates outcomes.

Records every Long/Short signal at notify time (suggested values).
After execution, links to the MT5 ticket.
Daily validation checks MT5 for closed positions and computes P&L.
Non-executed signals get a hypothetical outcome via price-bar replay.

Storage:
  data/trade_journal.jsonl  — append-only source of truth (one JSON per line)
  data/trade_journal.csv    — regenerated from JSONL on every write
                              (Excel / pandas friendly)

Design notes:
  - suggested_* values are captured at record time from the agent's _order_preview.
  - actual_*   values are populated at validation time from MT5 deal history.
  - Both are kept so the delta (agent suggestion vs what was traded) is visible.
  - User modifications to entry/SL/TP outside the agent are handled naturally:
    actual_entry/sl/tp come from MT5, not from the original suggestion.
"""
from __future__ import annotations

import csv
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import config
from utils.logger import get_logger

logger = get_logger(__name__)

_JOURNAL_JSONL = config.DATA_DIR / "trade_journal.jsonl"
_JOURNAL_CSV   = config.DATA_DIR / "trade_journal.csv"
_lock = threading.Lock()

# Validation window by time horizon keyword (how long to keep checking)
_WINDOW_DAYS: dict[str, int] = {
    "today":     1,
    "intraday":  1,
    "1-day":     1,
    "1 day":     1,
    "2-day":     3,
    "2 day":     3,
    "3-day":     3,
    "3 day":     3,
    "week":      7,
    "swing":     7,
    "medium":    14,
}

_CSV_FIELDS = [
    "signal_id", "recorded_at", "symbol", "signal", "confidence",
    "time_horizon", "rationale", "risk_note", "invalidation", "source",
    "suggested_entry", "suggested_sl", "suggested_tp", "suggested_lot",
    "risk_amount", "risk_reward", "limit_price",
    "debate_uncertainty", "debate_verdict", "debate_veto",
    "status", "mt5_ticket",
    "actual_entry", "actual_sl", "actual_tp", "actual_lot",
    "outcome", "pnl_pips", "pnl_usd", "close_price",
    "closed_at", "validated_at", "validation_window_days",
]


# ── internal I/O ──────────────────────────────────────────────────────────────

def _load() -> list[dict]:
    if not _JOURNAL_JSONL.exists():
        return []
    records: list[dict] = []
    with _JOURNAL_JSONL.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except Exception:
                    pass
    return records


def _save_all(records: list[dict]) -> None:
    """Rewrite JSONL then regenerate CSV."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _JOURNAL_JSONL.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    _write_csv(records)


def _append_record(record: dict) -> None:
    """Append one record to JSONL then regenerate CSV from full file."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _JOURNAL_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    _write_csv(_load())


def _write_csv(records: list[dict]) -> None:
    with _JOURNAL_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in records:
            writer.writerow({k: r.get(k, "") for k in _CSV_FIELDS})


# ── helpers ───────────────────────────────────────────────────────────────────

def _validation_window(time_horizon: str) -> int:
    """Return validation window in days from time_horizon string."""
    h = (time_horizon or "").lower()
    for key, days in _WINDOW_DAYS.items():
        if key in h:
            return days
    return 7  # default


# ── public write API ──────────────────────────────────────────────────────────

def record_signal(signal: dict[str, Any]) -> None:
    """
    Record a Long/Short signal at notify time.
    Called from notifier.notify() after _signal_id has been set.
    Captures agent-suggested entry/SL/TP at this moment (Option A).
    """
    action = signal.get("signal", "")
    if action not in ("Long", "Short"):
        return

    signal_id = signal.get("_signal_id", "")
    if not signal_id:
        logger.debug("record_signal: no _signal_id, skipping")
        return

    preview = signal.get("_order_preview") or {}
    debate  = signal.get("_debate") or {}

    record: dict[str, Any] = {
        "signal_id":              signal_id,
        "recorded_at":            datetime.now(timezone.utc).isoformat(),
        "symbol":                 signal.get("_symbol", config.MT5_SYMBOL),
        "signal":                 action,
        "confidence":             signal.get("confidence", ""),
        "time_horizon":           signal.get("time_horizon", ""),
        "rationale":              signal.get("rationale", ""),
        "risk_note":              signal.get("risk_note", ""),
        "invalidation":           signal.get("invalidation", ""),
        "source":                 signal.get("_source", ""),
        "suggested_entry":        preview.get("entry_price", ""),
        "suggested_sl":           preview.get("sl", ""),
        "suggested_tp":           preview.get("tp", ""),
        "suggested_lot":          preview.get("lot_size", ""),
        "risk_amount":            preview.get("risk_amount", ""),
        "risk_reward":            preview.get("risk_reward", ""),
        "limit_price":            signal.get("_limit_price", ""),
        "debate_uncertainty":     signal.get("_uncertainty_score", ""),
        "debate_verdict":         debate.get("final_decision", ""),
        "debate_veto":            signal.get("_debate_veto", ""),
        "status":                 "pending",
        "mt5_ticket":             "",
        "actual_entry":           "",
        "actual_sl":              "",
        "actual_tp":              "",
        "actual_lot":             "",
        "outcome":                "",
        "pnl_pips":               "",
        "pnl_usd":                "",
        "close_price":            "",
        "closed_at":              "",
        "validated_at":           "",
        "validation_window_days": _validation_window(signal.get("time_horizon", "")),
    }

    with _lock:
        _append_record(record)
    logger.info(
        f"Journal: recorded {action} {record['symbol']} signal_id={signal_id}"
    )


def update_signal_status(signal_id: str, **updates: Any) -> bool:
    """Update fields on an existing journal record by signal_id."""
    if not signal_id:
        return False
    with _lock:
        records = _load()
        for r in records:
            if r.get("signal_id") == signal_id:
                r.update(updates)
                _save_all(records)
                return True
    return False


def link_ticket(signal_id: str, mt5_ticket: int) -> None:
    """
    Link an MT5 ticket to a journal record after successful execution.
    Called from job3_executor.approve_and_execute().
    """
    if not signal_id:
        return
    ok = update_signal_status(
        signal_id,
        status="executed",
        mt5_ticket=str(mt5_ticket),
    )
    if ok:
        logger.info(f"Journal: linked ticket #{mt5_ticket} → signal {signal_id}")
    else:
        logger.warning(f"Journal: signal {signal_id} not found for ticket link")


def reject_in_journal(signal_id: str) -> None:
    """Mark a journal record as rejected."""
    if signal_id:
        update_signal_status(signal_id, status="rejected")


# ── validation ────────────────────────────────────────────────────────────────

def validate_pending(symbol: str | None = None) -> int:
    """
    Validate all unresolved journal records:
      - Executed records: check MT5 for open/closed status and compute P&L.
      - Non-executed (rejected/expired/pending) records within their window:
        replay price bars to compute a hypothetical outcome.
      - Records past their window + 7 day grace period → expired_untracked.

    Returns count of records updated.
    """
    from mt5.connector import is_connected

    if not is_connected():
        logger.warning("Journal validate_pending: MT5 not connected, skipping")
        return 0

    with _lock:
        records = _load()
        updated = 0
        now = datetime.now(timezone.utc)

        for r in records:
            outcome = r.get("outcome", "")
            # Skip already fully resolved records
            if outcome and outcome not in ("still_open",):
                continue
            if r.get("signal") not in ("Long", "Short"):
                continue

            sym = r.get("symbol") or config.MT5_SYMBOL
            if symbol and sym != symbol:
                continue

            # Parse recorded_at
            try:
                recorded_at = datetime.fromisoformat(r["recorded_at"])
                if recorded_at.tzinfo is None:
                    recorded_at = recorded_at.replace(tzinfo=timezone.utc)
            except Exception:
                continue

            window_days = int(r.get("validation_window_days") or 7)
            expiry = recorded_at + timedelta(days=window_days + 7)

            if now > expiry and not outcome:
                r["outcome"] = "expired_untracked"
                r["validated_at"] = now.isoformat()
                updated += 1
                continue

            # ── Executed: check MT5 ───────────────────────────────────────
            ticket_str = r.get("mt5_ticket", "")
            if r.get("status") == "executed" and ticket_str:
                try:
                    result = _check_position(sym, int(ticket_str), r)
                    if result:
                        r.update(result)
                        r["validated_at"] = now.isoformat()
                        updated += 1
                except Exception as e:
                    logger.error(
                        f"Journal validation error ticket #{ticket_str}: {e}",
                        exc_info=True,
                    )
                continue

            # ── Non-executed: hypothetical check ─────────────────────────
            if r.get("status") in ("rejected", "expired", "pending") and not outcome:
                s_entry = r.get("suggested_entry")
                s_sl    = r.get("suggested_sl")
                s_tp    = r.get("suggested_tp")
                if not (s_entry and (s_sl or s_tp)):
                    continue
                try:
                    hyp = _hypothetical_outcome(
                        symbol=sym,
                        direction=r.get("signal", "Long"),
                        signal_time=recorded_at,
                        suggested_entry=float(s_entry) if s_entry else None,
                        suggested_sl=float(s_sl) if s_sl else None,
                        suggested_tp=float(s_tp) if s_tp else None,
                        window_days=window_days,
                    )
                    if hyp:
                        r.update(hyp)
                        r["validated_at"] = now.isoformat()
                        updated += 1
                except Exception as e:
                    logger.error(
                        f"Journal hypothetical check failed {r.get('signal_id')}: {e}",
                        exc_info=True,
                    )

        if updated:
            _save_all(records)
        logger.info(f"Journal validate_pending: {updated} record(s) updated")
        return updated


def _check_position(symbol: str, ticket: int, record: dict) -> dict | None:
    """
    Check MT5 for the current state of a position.
    Returns partial update dict, or None if nothing changed.
    """
    import MetaTrader5 as mt5

    pip = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["pip"]
    direction = record.get("signal", "Long")

    # Check open positions first
    all_open = mt5.positions_get() or []
    open_pos = next((p for p in all_open if p.ticket == ticket), None)
    if open_pos is not None:
        open_p = open_pos.price_open
        curr_p = open_pos.price_current
        pips = (curr_p - open_p) / pip if direction == "Long" else (open_p - curr_p) / pip
        return {
            "outcome":      "still_open",
            "actual_entry": round(open_p, 5),
            "actual_sl":    round(open_pos.sl, 5) if open_pos.sl else "",
            "actual_tp":    round(open_pos.tp, 5) if open_pos.tp else "",
            "actual_lot":   open_pos.volume,
            "pnl_pips":     round(pips, 1),
            "pnl_usd":      round(open_pos.profit, 2),
            "close_price":  "",
            "closed_at":    "",
        }

    # Not open — check deal history
    deals = mt5.history_deals_get(position=ticket)
    if not deals:
        return None

    # Separate entry and close deals
    # DEAL_ENTRY_IN = 0 (entry), DEAL_ENTRY_OUT = 1 (close)
    entry_deal = next((d for d in deals if d.entry == 0), None)
    close_deal  = next((d for d in deals if d.entry == 1), None)

    if entry_deal is None:
        return None

    entry_price  = round(entry_deal.price, 5)
    total_profit = round(sum(d.profit for d in deals), 2)

    if close_deal:
        close_price = round(close_deal.price, 5)
        close_time  = datetime.fromtimestamp(
            close_deal.time, tz=timezone.utc
        ).isoformat()
        pips = (
            (close_price - entry_price) / pip if direction == "Long"
            else (entry_price - close_price) / pip
        )

        # Determine outcome based on actual TP/SL (user may have modified them)
        # Use the most recent actual values stored in the record first,
        # then fall back to suggested values
        tp_ref = record.get("actual_tp") or record.get("suggested_tp")
        sl_ref = record.get("actual_sl") or record.get("suggested_sl")
        outcome = "manual_close"
        if tp_ref:
            try:
                tp = float(tp_ref)
                if direction == "Long" and close_price >= tp * 0.9999:
                    outcome = "TP_hit"
                elif direction == "Short" and close_price <= tp * 1.0001:
                    outcome = "TP_hit"
            except Exception:
                pass
        if outcome != "TP_hit" and sl_ref:
            try:
                sl = float(sl_ref)
                if direction == "Long" and close_price <= sl * 1.0001:
                    outcome = "SL_hit"
                elif direction == "Short" and close_price >= sl * 0.9999:
                    outcome = "SL_hit"
            except Exception:
                pass

        return {
            "actual_entry": entry_price,
            "actual_lot":   round(entry_deal.volume, 2),
            "outcome":      outcome,
            "pnl_pips":     round(pips, 1),
            "pnl_usd":      total_profit,
            "close_price":  close_price,
            "closed_at":    close_time,
        }

    # Has entry deal but no close deal — opened but not fully closed
    return {
        "actual_entry": entry_price,
        "actual_lot":   round(entry_deal.volume, 2),
        "outcome":      "still_open",
    }


def _hypothetical_outcome(
    symbol: str,
    direction: str,
    signal_time: datetime,
    suggested_entry: float | None,
    suggested_sl: float | None,
    suggested_tp: float | None,
    window_days: int,
) -> dict | None:
    """
    Replay M15 bars from signal_time to see which was hit first: TP or SL.
    Delegates to mt5.bar_replay.find_first_exit() and maps back to the
    trade-journal outcome schema ("hypothetical_sl" / "hypothetical_tp" / "hypothetical_open").
    """
    from mt5.bar_replay import find_first_exit

    result = find_first_exit(
        symbol=symbol,
        direction=direction,
        from_dt=signal_time,
        entry=suggested_entry,
        sl=suggested_sl,
        tp=suggested_tp,
        window_hours=window_days * 24,
    )
    if result is None:
        return None

    r = result["result"]
    pips = result["pnl_pips"] if result["pnl_pips"] is not None else ""
    if r == "sl":
        return {
            "outcome":     "hypothetical_sl",
            "pnl_pips":    pips,
            "close_price": round(result["hit_price"], 5),
        }
    if r == "tp":
        return {
            "outcome":     "hypothetical_tp",
            "pnl_pips":    pips,
            "close_price": round(result["hit_price"], 5),
        }
    return {"outcome": "hypothetical_open", "pnl_pips": ""}


# ── reporting ─────────────────────────────────────────────────────────────────

def journal_report(last_n: int = 30) -> str:
    """
    Generate a performance summary from the last N resolved records.
    """
    records = _load()
    resolved = [
        r for r in records
        if r.get("outcome") and r["outcome"] not in (
            "", "still_open", "expired_untracked", "hypothetical_open"
        )
    ]
    resolved.sort(key=lambda r: r.get("recorded_at", ""), reverse=True)
    recent = resolved[:last_n]

    if not recent:
        return "No resolved trades in the journal yet. Run `journal validate` to update."

    executed = [r for r in recent if r.get("status") == "executed"]
    hyp      = [r for r in recent if r.get("status") != "executed"]

    def _stats(trades: list[dict], include_usd: bool = False) -> list[str]:
        if not trades:
            return ["  (none)"]
        wins   = [r for r in trades if r.get("outcome") in ("TP_hit", "hypothetical_tp")]
        losses = [r for r in trades if r.get("outcome") in ("SL_hit", "hypothetical_sl")]
        manual = [r for r in trades if r.get("outcome") == "manual_close"]
        wr     = len(wins) / len(trades) * 100

        pnl_vals = []
        for r in trades:
            try:
                pnl_vals.append(float(r["pnl_pips"]))
            except Exception:
                pass
        total_pips = sum(pnl_vals)
        avg_pips   = total_pips / len(pnl_vals) if pnl_vals else 0

        lines = [
            f"  Trades:   {len(trades)}  "
            f"(TP {len(wins)} | SL {len(losses)} | Manual {len(manual)})",
            f"  Win rate: {wr:.0f}%",
            f"  Pips:     avg {avg_pips:+.1f}  total {total_pips:+.1f}",
        ]
        if include_usd:
            usd_vals = []
            for r in trades:
                try:
                    usd_vals.append(float(r["pnl_usd"]))
                except Exception:
                    pass
            if usd_vals:
                lines.append(f"  P&L USD:  ${sum(usd_vals):+.2f}")
        return lines

    lines = [
        f"=== Trade Journal Report (last {last_n} resolved) ===",
        "",
        f"Executed ({len(executed)}):",
        *_stats(executed, include_usd=True),
        "",
        f"Hypothetical — not executed ({len(hyp)}):",
        *_stats(hyp),
    ]

    # Per-symbol breakdown if multiple pairs
    syms = sorted({r.get("symbol") for r in recent if r.get("symbol")})
    if len(syms) > 1:
        lines.append("\nPer pair:")
        for sym in syms:
            sym_t = [r for r in recent if r.get("symbol") == sym]
            wins  = sum(1 for r in sym_t if r.get("outcome") in ("TP_hit", "hypothetical_tp"))
            wr    = wins / len(sym_t) * 100 if sym_t else 0
            lines.append(f"  {sym}: {len(sym_t)} trades  {wr:.0f}% win rate")

    lines.append(f"\nFull CSV: {_JOURNAL_CSV}")
    return "\n".join(lines)


def list_recent(n: int = 10) -> list[dict]:
    """Return the N most recent journal records (newest first)."""
    records = _load()
    return list(reversed(records[-n:]))
