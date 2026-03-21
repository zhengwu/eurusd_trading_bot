"""Job 3 — Trade Executor.

Reads approved signals from the pending signal store and executes them on MT5.

Signal routing:
  Job 1 Long  → open_position("buy",  lot, sl, tp)
  Job 1 Short → open_position("sell", lot, sl, tp)
  Job 2 Exit  → close_position(ticket)            full close
  Job 2 Trim  → close_position(ticket, lot)       partial close
  Hold / Wait → never saved to store, never executed

Usage (CLI):
  python -m agents.job3_executor --list              # show pending/approved signals
  python -m agents.job3_executor --run               # execute all approved signals
  python -m agents.job3_executor --approve <ID>      # approve then execute
  python -m agents.job3_executor --reject  <ID>      # reject signal

Typically called directly from the Slack bot after user approval.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

load_dotenv()

import config
from mt5.connector import connect, disconnect, is_connected
from mt5.position_reader import get_current_tick, get_open_positions
from mt5.risk_manager import calculate_lot_size, calculate_sl_tp, sl_pips_from_price
from mt5.order_manager import open_position, close_position
from pipeline.signal_store import (
    approve_signal,
    get_signal_by_id,
    list_signals,
    mark_executed,
    reject_signal,
)
from utils.logger import get_logger

logger = get_logger(__name__)


# ── execution logic ───────────────────────────────────────────────────────────

def execute_signal(signal: dict[str, Any], trim_pct: float | None = None) -> dict[str, Any]:
    """
    Execute a single approved signal. Returns execution result dict.
    trim_pct: percentage of position to close for Trim signals (0-100).
              None defaults to 50%. E.g. 25 = close 25% of position.
    """
    if not is_connected():
        return {"ok": False, "error": "MT5 is not connected"}

    action = signal.get("signal", "")
    source = signal.get("_source", signal.get("source", "job1"))

    # ── Job 1: open a new position ─────────────────────────────────────────
    if action in ("Long", "Short"):
        direction = "buy" if action == "Long" else "sell"

        tick = get_current_tick()
        if tick is None:
            return {"ok": False, "error": "No tick data available"}
        current_price = tick["ask"] if direction == "buy" else tick["bid"]

        sl_price, tp_price = calculate_sl_tp(signal, current_price, direction)

        sl_pips = sl_pips_from_price(sl_price, current_price, direction)
        if sl_pips < 1:
            sl_pips = config.JOB3_DEFAULT_SL_PIPS

        account_info = _get_equity()
        if account_info is None:
            return {"ok": False, "error": "Cannot read account equity"}

        lot = calculate_lot_size(account_info, config.JOB3_RISK_PCT, sl_pips)

        result = open_position(
            symbol=config.MT5_SYMBOL,
            direction=direction,
            lot=lot,
            sl=sl_price,
            tp=tp_price,
            comment=f"job1_{signal.get('id', '')}",
        )
        return result

    # ── Job 2: Exit — close full position ──────────────────────────────────
    elif action == "Exit":
        ticket = signal.get("_ticket") or signal.get("ticket")
        if ticket is None:
            return {"ok": False, "error": "No ticket in Exit signal"}
        return close_position(int(ticket))

    # ── Job 2: Trim — partial close ────────────────────────────────────────
    elif action == "Trim":
        ticket = signal.get("_ticket") or signal.get("ticket")
        if ticket is None:
            return {"ok": False, "error": "No ticket in Trim signal"}

        positions = get_open_positions(symbol=config.MT5_SYMBOL)
        pos = next((p for p in positions if p["ticket"] == int(ticket)), None)
        if pos is None:
            return {"ok": False, "error": f"Position #{ticket} not found"}

        pct = trim_pct if trim_pct is not None else 50.0
        pct = max(1.0, min(100.0, pct))
        lot = round(pos["volume"] * pct / 100.0, 2)
        lot = max(config.JOB3_MIN_LOT, lot)
        logger.info(f"Trim {pct}% of {pos['volume']} lots = {lot} lots")

        return close_position(int(ticket), lot=lot)

    # ── Job 4: SetSL / SetTP / SetSLTP ────────────────────────────────────
    elif action in ("SetSL", "SetTP", "SetSLTP"):
        from mt5.order_manager import modify_sl_tp
        ticket = signal.get("_ticket") or signal.get("ticket")
        if ticket is None:
            return {"ok": False, "error": f"No ticket in {action} signal"}
        sl = signal.get("sl") or signal.get("suggested_sl")
        tp = signal.get("tp") or signal.get("suggested_tp")
        if action == "SetSL" and sl is None:
            return {"ok": False, "error": "SetSL signal missing sl price"}
        if action == "SetTP" and tp is None:
            return {"ok": False, "error": "SetTP signal missing tp price"}
        if action == "SetSLTP" and (sl is None or tp is None):
            return {"ok": False, "error": "SetSLTP signal missing sl or tp price"}
        return modify_sl_tp(int(ticket), sl=sl, tp=tp)

    else:
        return {"ok": False, "error": f"Unsupported signal action: {action!r}"}


def _get_equity() -> float | None:
    """Return current account equity, or None on failure."""
    try:
        from mt5.position_reader import get_account_summary
        info = get_account_summary()
        return info.get("equity")
    except Exception as e:
        logger.error(f"Failed to get account equity: {e}")
        return None


def compute_order_preview(signal: dict[str, Any]) -> dict[str, Any] | None:
    """
    Compute the full order details for a pending Long/Short signal:
    entry price, SL, TP, lot size, risk amount, R:R ratio.

    Returns a dict with these fields, or None if MT5 is unavailable.
    Falls back to key_levels from the signal if no live tick.
    """
    action = signal.get("signal")
    if action not in ("Long", "Short"):
        return None

    try:
        direction = "buy" if action == "Long" else "sell"

        # Try live tick first, fall back gracefully
        tick = get_current_tick() if is_connected() else None
        if tick:
            current_price = tick["ask"] if direction == "buy" else tick["bid"]
        else:
            # Estimate from key_levels midpoint
            levels = signal.get("key_levels") or {}
            sup = levels.get("support")
            res = levels.get("resistance")
            if sup and res:
                current_price = round((float(sup) + float(res)) / 2, 5)
            else:
                return None  # can't compute without any price reference

        sl_price, tp_price = calculate_sl_tp(signal, current_price, direction)
        sl_pips = sl_pips_from_price(sl_price, current_price, direction)
        if sl_pips < 1:
            sl_pips = config.JOB3_DEFAULT_SL_PIPS

        equity = _get_equity()
        lot = calculate_lot_size(equity or 10000.0, config.JOB3_RISK_PCT, sl_pips)
        risk_amount = round((equity or 0) * config.JOB3_RISK_PCT / 100, 2)

        tp_pips = abs(tp_price - current_price) / 0.0001 if tp_price else None
        rr = round(tp_pips / sl_pips, 2) if tp_pips and sl_pips > 0 else None

        return {
            "entry_price": round(current_price, 5),
            "sl":          round(sl_price, 5) if sl_price else None,
            "tp":          round(tp_price, 5) if tp_price else None,
            "sl_pips":     round(sl_pips, 1),
            "tp_pips":     round(tp_pips, 1) if tp_pips else None,
            "lot_size":    lot,
            "risk_amount": risk_amount,
            "risk_reward": f"1:{rr}" if rr else None,
            "live_price":  tick is not None,
        }
    except Exception as e:
        logger.warning(f"compute_order_preview failed: {e}")
        return None


def run_executor_once() -> int:
    """
    Find all approved signals and execute them.
    Returns count of signals processed.
    """
    approved = list_signals(status="approved")
    if not approved:
        logger.info("No approved signals to execute")
        return 0

    logger.info(f"=== Job 3: executing {len(approved)} approved signal(s) ===")
    count = 0
    for signal in approved:
        signal_id = signal.get("id", "?")
        action = signal.get("signal", "?")
        logger.info(f"Executing signal {signal_id} — {action}")
        try:
            result = execute_signal(signal)
            mark_executed(signal_id, result)
            if result.get("ok"):
                logger.info(f"Signal {signal_id} executed: {result}")
            else:
                logger.error(f"Signal {signal_id} execution failed: {result.get('error')}")
            count += 1
        except Exception as e:
            logger.error(f"Signal {signal_id} exception: {e}", exc_info=True)
            mark_executed(signal_id, {"ok": False, "error": str(e)})

    return count


# ── Slack-callable helper ─────────────────────────────────────────────────────

def approve_and_execute(signal_id: str, trim_pct: float | None = None) -> dict[str, Any]:
    """
    Approve a pending signal and immediately execute it.
    Called directly from the Slack bot after user approval.
    trim_pct: percentage of position to close for Trim signals (e.g. 25 = 25%).
    Returns a result dict suitable for sending back to Slack.
    """
    signal = get_signal_by_id(signal_id)
    if signal is None:
        return {"ok": False, "error": f"Signal `{signal_id}` not found"}

    if signal.get("status") != "pending":
        return {"ok": False, "error": f"Signal `{signal_id}` is not pending (status={signal.get('status')})"}

    if not approve_signal(signal_id):
        return {"ok": False, "error": f"Failed to approve signal `{signal_id}`"}

    if not is_connected():
        return {"ok": False, "error": "MT5 is not connected — signal approved but not executed"}

    result = execute_signal(signal, trim_pct=trim_pct)
    mark_executed(signal_id, result)
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def _print_signals(signals: list[dict]) -> None:
    if not signals:
        print("  (none)")
        return
    for s in signals:
        print(
            f"  [{s.get('id')}] {s.get('signal'):6} [{s.get('confidence')}] "
            f"source={s.get('source','?')} status={s.get('status')} "
            f"created={s.get('created_at','')[:16]}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="EUR/USD Job 3 — Trade Executor")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list",    action="store_true", help="List pending and approved signals")
    group.add_argument("--run",     action="store_true", help="Execute all approved signals")
    group.add_argument("--approve", metavar="ID",        help="Approve a signal by ID")
    group.add_argument("--reject",  metavar="ID",        help="Reject a signal by ID")
    parser.add_argument("--pct",    type=float, default=None,
                        help="Percentage of position to close for Trim signals, e.g. 25 = 25%% (default 50%%)")
    args = parser.parse_args()

    if args.list:
        print("\n=== Pending signals ===")
        _print_signals(list_signals(status="pending"))
        print("\n=== Approved signals ===")
        _print_signals(list_signals(status="approved"))
        print("\n=== Recently executed ===")
        executed = list_signals(status="executed")
        _print_signals(executed[-5:] if len(executed) > 5 else executed)
        return

    if args.reject:
        if reject_signal(args.reject.upper()):
            print(f"Signal {args.reject.upper()} rejected.")
        else:
            print(f"Signal {args.reject.upper()} not found or not pending.")
        return

    # --approve and --run both need MT5
    if not connect():
        print("ERROR: Cannot connect to MT5 — is the terminal running?")
        sys.exit(1)

    try:
        if args.approve:
            signal_id = args.approve.upper()
            result = approve_and_execute(signal_id, trim_pct=args.pct)
            if result.get("ok"):
                print(f"Signal {signal_id} executed: ticket={result.get('ticket')} price={result.get('price')}")
            else:
                print(f"ERROR: {result.get('error')}")

        elif args.run:
            n = run_executor_once()
            print(f"Executed {n} signal(s).")

    finally:
        disconnect()


if __name__ == "__main__":
    main()
