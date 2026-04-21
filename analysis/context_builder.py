"""Context builder — assembles the time-series context window for full LLM analysis.

Output format:
  === BREAKING EVENT [HH:MM UTC] ===
  === CURRENT MARKET REGIME ===    (if config.USE_REGIME_ANALYSIS is True)
  === TODAY: {date} ===            (full detail: headlines, events, prices)
  === UPCOMING EVENTS (next 48h) ===
  === PAST 7 DAYS (summaries) ===  (summary.md only — no raw headlines)
  === 30-DAY PRICE TREND ===       (prices only — no news text)

Enforces CONTEXT_MAX_TOKENS by trimming oldest price rows first.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import config
from utils.logger import get_logger
from utils.date_utils import today_str_utc

logger = get_logger(__name__)

# Rough token estimate: 1 token ≈ 4 chars
_CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    return len(text) // _CHARS_PER_TOKEN


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                continue
    return records


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None


def _fmt_intraday_headlines(records: list[dict]) -> str:
    if not records:
        return "  (none yet)"
    lines = []
    for r in records:
        score = r.get("triage_score") or "?"
        headline = r.get("headline", "")
        time_str = r.get("time", "")
        lines.append(f"  [{time_str} EST] [score:{score}] {headline}")
    return "\n".join(lines)


def _fmt_events(events: list[dict]) -> str:
    if not events:
        return "  (none)"
    lines = []
    for e in events:
        actual = e.get("actual")
        forecast = e.get("forecast")
        surprise = e.get("surprise", "")
        parts = [f"  {e.get('time','N/A')} | {e.get('event','')} ({e.get('currency','')})"]
        if actual is not None:
            parts.append(f"actual={actual}")
        if forecast is not None:
            parts.append(f"forecast={forecast}")
        if surprise:
            parts.append(f"[{surprise}]")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _filter_today_events(events: list[dict], today: str) -> list[dict]:
    """Return ForexFactory events for today + all FRED historical values."""
    return [
        e for e in events
        if e.get("release_date") == today or e.get("source") == "FRED"
    ]


def _fmt_upcoming_events(events: list[dict], today: str) -> str:
    """Format ForexFactory events scheduled in the next 48 hours."""
    tomorrow = (datetime.fromisoformat(today) + timedelta(days=1)).isoformat()
    day2     = (datetime.fromisoformat(today) + timedelta(days=2)).isoformat()
    upcoming = [
        e for e in events
        if e.get("release_date") in (tomorrow, day2) and e.get("source") == "ForexFactory"
    ]
    if not upcoming:
        return "  (none scheduled in next 48h)"
    lines = []
    for e in sorted(upcoming, key=lambda x: x.get("time", "")):
        try:
            dt = datetime.fromisoformat(e["time"])
            time_str = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            time_str = e.get("release_date", "TBA")
        impact   = e.get("impact", "").upper()
        forecast = e.get("forecast")
        previous = e.get("previous")
        line = f"  {time_str} | [{impact}] {e.get('event', '')} ({e.get('currency', '')})"
        if forecast is not None:
            line += f"  forecast={forecast}"
        if previous is not None:
            line += f"  prev={previous}"
        lines.append(line)
    return "\n".join(lines)


def _fmt_intraday_prices(prices: dict) -> str:
    def _v(asset):
        d = prices.get(asset) or {}
        close = d.get("close")
        pct = d.get("pct_change")
        if close is None:
            return "N/A"
        pct_str = f"{pct:+.2f}%" if pct is not None else "N/A"
        return f"{close}({pct_str})"
    return (
        f"  EURUSD:{_v('EURUSD')}  DXY:{_v('DXY')}  "
        f"US10Y:{_v('US10Y')}  Gold:{_v('Gold')}  VIX:{_v('VIX')}"
    )


def _fmt_price_table_row(date_str: str, prices: dict) -> str:
    def _v(asset):
        d = prices.get(asset) or {}
        c = d.get("close")
        return f"{c:.4f}" if c is not None else "  N/A  "
    return (
        f"{date_str} | {_v('EURUSD'):8} | {_v('DXY'):7} | "
        f"{_v('US10Y'):6} | {_v('Gold'):7} | {_v('VIX'):6}"
    )


def _filter_intraday_by_symbol(records: list[dict], symbol: str) -> list[dict]:
    """
    Return records relevant to the given symbol.
    Records saved before the symbol field was added (legacy) are included for all pairs.
    Records with a non-matching symbol are excluded.
    """
    out = []
    for r in records:
        rec_sym = r.get("symbol", "")
        # Include if: no symbol stored (legacy record) OR symbol matches
        if not rec_sym or rec_sym == symbol:
            out.append(r)
    return out


def build_context(trigger_item: dict | None = None, symbol: str | None = None) -> str:
    """
    Assemble the full context window string passed to the analysis LLM.
    Trims oldest price rows if total exceeds CONTEXT_MAX_TOKENS.
    """
    today = today_str_utc()
    sym = symbol or config.MT5_SYMBOL
    data_dir = config.DATA_DIR
    sections: list[str] = []

    # ── BREAKING EVENT ────────────────────────────────────────────────────────
    if trigger_item:
        now_str = datetime.now(timezone.utc).strftime("%H:%M")
        headline = trigger_item.get("headline") or trigger_item.get("title", "")
        score = trigger_item.get("score") or trigger_item.get("triage_score", "?")
        tag = trigger_item.get("tag") or trigger_item.get("triage_tag", "other")

        # Corroborating headlines: last few intraday items for this pair
        from triage.intraday_logger import read_today_intraday
        recent = _filter_intraday_by_symbol(read_today_intraday(), sym)
        corroborating = [
            r.get("headline", "")
            for r in recent[-10:]
            if r.get("headline") and r.get("headline") != headline
        ]

        pre_event_ctx = trigger_item.get("_pre_event_context", "")
        sections.append(
            f"=== BREAKING EVENT [{now_str} UTC] ===\n"
            f'Trigger: "{headline}" [score: {score}, tag: {tag}]\n'
            + (f"\n{pre_event_ctx}\n" if pre_event_ctx else "")
            + f"Recent corroborating headlines (last 2 hrs): "
            f"{', '.join(corroborating[:5]) or '(none)'}\n"
        )

    # ── CURRENT MARKET REGIME ────────────────────────────────────────────────
    if config.USE_REGIME_ANALYSIS:
        try:
            from pipeline.price_agent import get_regime_inputs
            from pipeline.regime_agent import get_market_regimes
            inputs = get_regime_inputs(symbol=sym)

            # Auto-derive macro bias from yield spread; fall back to config if unavailable
            from pipeline.price_agent import get_macro_bias
            macro_bias = get_macro_bias(symbol=sym)

            regime_text = get_market_regimes(
                vix                   = inputs.get("vix"),
                price                 = inputs.get("price"),
                sma20                 = inputs.get("sma20"),
                sma50                 = inputs.get("sma50"),
                sma200                = inputs.get("sma200"),
                atr_current           = inputs.get("atr_current"),
                atr_30d_avg           = inputs.get("atr_30d_avg"),
                m15_up_bars           = inputs.get("m15_up_bars"),
                m15_total_bars        = inputs.get("m15_total_bars", 16),
                pair_realized_vol_pct = inputs.get("pair_realized_vol_pct"),
                macro_bias            = macro_bias,
            )
            sections.append(regime_text + "\n")
        except Exception as e:
            logger.warning(f"Regime analysis failed — skipping: {e}")

    # ── TODAY ─────────────────────────────────────────────────────────────────
    today_dir = data_dir / today
    intraday = _read_jsonl(today_dir / "intraday.jsonl")
    # Filter to headlines scored for this pair (legacy records without a symbol field are kept).
    intraday = _filter_intraday_by_symbol(intraday, sym)
    # Cap to the 30 most-recent headlines — prevents this section inflating the
    # context and triggering truncation of the TA / price summary section later.
    intraday = intraday[-30:] if len(intraday) > 30 else intraday
    events = _read_json(today_dir / "events.json") or []
    prices_today = _read_json(today_dir / "prices.json") or {}

    today_events = _filter_today_events(events, today)
    sections.append(
        f"=== TODAY: {today} ===\n"
        f"Intraday news so far:\n{_fmt_intraday_headlines(intraday)}\n\n"
        f"Events:\n{_fmt_events(today_events)}\n\n"
        f"Price action since open:\n{_fmt_intraday_prices(prices_today)}\n"
    )

    # ── UPCOMING 48H EVENTS ───────────────────────────────────────────────────
    sections.append(
        f"=== UPCOMING EVENTS (next 48h) ===\n"
        f"{_fmt_upcoming_events(events, today)}\n"
    )

    # ── PAST 7 DAYS (summaries only) ──────────────────────────────────────────
    past_parts: list[str] = []
    for i in range(1, config.CONTEXT_DAYS_SUMMARY + 1):
        date_str = (datetime.now(timezone.utc).date() - timedelta(days=i)).isoformat()
        summary = _read_text(data_dir / date_str / "summary.md")
        if summary:
            past_parts.append(f"--- {date_str} ---\n{summary.strip()}")
        else:
            past_parts.append(
                f"--- {date_str} ---\n"
                "(summary unavailable — system may have been offline)"
            )
    sections.append("=== PAST 7 DAYS (summaries) ===\n" + "\n\n".join(past_parts) + "\n")

    # ── PRICE SUMMARY (live + technicals + correlated assets) ────────────────
    try:
        from pipeline.price_agent import get_price_summary
        price_summary = get_price_summary(symbol=sym)
    except Exception as e:
        logger.warning(f"Price agent failed — falling back to raw table: {e}")
        price_summary = _fallback_price_table(data_dir)

    sections.append(price_summary + "\n")

    # ── enforce token budget (trim the price summary if needed) ───────────────
    def _assemble():
        return "\n".join(sections)

    full = _assemble()
    # If over budget, truncate the price summary (keep first CONTEXT_MAX_TOKENS*4 chars)
    if _estimate_tokens(full) > config.CONTEXT_MAX_TOKENS:
        budget_chars = config.CONTEXT_MAX_TOKENS * _CHARS_PER_TOKEN
        used = sum(len(s) for s in sections[:-1])
        remaining = max(budget_chars - used, 500)
        sections[-1] = price_summary[:remaining] + "\n...[price summary truncated]\n"
        full = _assemble()

    token_est = _estimate_tokens(full)
    logger.info(f"Context assembled: ~{token_est} tokens")
    return full


def _load_journal_for_symbol(symbol: str) -> list[dict]:
    """Return all trade journal records for a given symbol."""
    journal_path = config.DATA_DIR / "trade_journal.jsonl"
    if not journal_path.exists():
        return []
    records: list[dict] = []
    try:
        with journal_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    if r.get("symbol") == symbol:
                        records.append(r)
                except Exception:
                    pass
    except Exception:
        pass
    return records


def get_open_position_theories(symbol: str) -> str:
    """
    Build a context block describing open positions for a symbol and their
    original trading thesis. Injected into the full analysis prompt so the LLM
    can classify new signals as 'continuation' or 'new_theory'.

    Returns empty string if no open positions or MT5 is unavailable.
    """
    try:
        from mt5.connector import is_connected
        from mt5.position_reader import get_open_positions
        if not is_connected():
            return ""

        positions = get_open_positions(symbol=symbol)
        if not positions:
            return ""

        # Build ticket → journal record lookup for original thesis
        journal_recs = _load_journal_for_symbol(symbol)
        ticket_to_theory = {
            str(r.get("mt5_ticket", "")): r
            for r in journal_recs
            if r.get("mt5_ticket") and r.get("status") == "executed"
        }

        display = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["display"]
        lines = [
            f"=== OPEN POSITIONS — {display} ===",
            f"You currently have {len(positions)} open position(s) on {display}.",
            "Each new signal you generate MUST include a theory_classification field (see JSON schema).",
            "",
        ]

        for i, pos in enumerate(positions, 1):
            direction = "LONG" if pos["type"] == "buy" else "SHORT"
            ticket    = str(pos["ticket"])
            entry     = pos["open_price"]
            sl        = pos["sl"] or "N/A"
            tp        = pos["tp"] or "N/A"
            pnl_usd   = pos.get("profit", 0.0)
            opened    = pos.get("open_time", "N/A")

            lines.append(f"Position #{i} — {direction}  Ticket: {ticket}")
            lines.append(f"  Entry: {entry}  SL: {sl}  TP: {tp}")
            lines.append(f"  Opened: {opened}  Unrealised P&L: ${pnl_usd:.2f}")

            rec = ticket_to_theory.get(ticket)
            if rec:
                rationale    = rec.get("rationale", "")
                invalidation = rec.get("invalidation", "")
                if rationale:
                    lines.append(f"  Original thesis: {rationale}")
                if invalidation:
                    lines.append(f"  Invalidation: {invalidation}")
            else:
                lines.append("  Original thesis: (unavailable — may have been opened manually)")
            lines.append("")

        lines += [
            "CLASSIFICATION RULES:",
            '  "continuation" — new signal is in the SAME direction AND the macro/technical thesis',
            "    is materially the same as an existing position above (not just the same direction).",
            '  "new_theory"   — different direction, OR a distinct thesis (e.g., existing position',
            "    based on ECB decision; new signal based on a US CPI surprise).",
            "  When continuation, set related_ticket to the MT5 ticket number of the matched position.",
            "",
        ]
        return "\n".join(lines)

    except Exception as e:
        logger.warning(f"[{symbol}] get_open_position_theories failed (non-fatal): {e}")
        return ""


def _fallback_price_table(data_dir: Path) -> str:
    """Raw 30-day closing price table — used if price_agent fails."""
    rows: list[str] = [
        "=== 30-DAY PRICE TREND (fallback — no live data) ===",
        "Date       | EURUSD   | DXY     | US10Y  | Gold    | VIX",
        "-" * 65,
    ]
    for i in range(1, config.CONTEXT_DAYS_PRICES + 1):
        date_str = (datetime.now(timezone.utc).date() - timedelta(days=i)).isoformat()
        prices = _read_json(data_dir / date_str / "prices.json")
        if prices:
            rows.append(_fmt_price_table_row(date_str, prices))
    return "\n".join(rows)
