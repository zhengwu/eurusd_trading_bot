"""Context builder — assembles the time-series context window for full LLM analysis.

Output format:
  === BREAKING EVENT [HH:MM UTC] ===
  === TODAY: {date} ===         (full detail: headlines, events, prices)
  === PAST 7 DAYS (summaries) === (summary.md only — no raw headlines)
  === 30-DAY PRICE TREND ===    (prices only — no news text)

Enforces CONTEXT_MAX_TOKENS by trimming oldest price rows first.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
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


def build_context(trigger_item: dict | None = None) -> str:
    """
    Assemble the full context window string passed to the analysis LLM.
    Trims oldest price rows if total exceeds CONTEXT_MAX_TOKENS.
    """
    today = today_str_utc()
    data_dir = config.DATA_DIR
    sections: list[str] = []

    # ── BREAKING EVENT ────────────────────────────────────────────────────────
    if trigger_item:
        now_str = datetime.now(timezone.utc).strftime("%H:%M")
        headline = trigger_item.get("headline") or trigger_item.get("title", "")
        score = trigger_item.get("score") or trigger_item.get("triage_score", "?")
        tag = trigger_item.get("tag") or trigger_item.get("triage_tag", "other")

        # Corroborating headlines: last few intraday items (excluding the trigger)
        from triage.intraday_logger import read_today_intraday
        recent = read_today_intraday()
        corroborating = [
            r.get("headline", "")
            for r in recent[-10:]
            if r.get("headline") and r.get("headline") != headline
        ]

        sections.append(
            f"=== BREAKING EVENT [{now_str} UTC] ===\n"
            f'Trigger: "{headline}" [score: {score}, tag: {tag}]\n'
            f"Recent corroborating headlines (last 2 hrs): "
            f"{', '.join(corroborating[:5]) or '(none)'}\n"
        )

    # ── TODAY ─────────────────────────────────────────────────────────────────
    today_dir = data_dir / today
    intraday = _read_jsonl(today_dir / "intraday.jsonl")
    events = _read_json(today_dir / "events.json") or []
    prices_today = _read_json(today_dir / "prices.json") or {}

    sections.append(
        f"=== TODAY: {today} ===\n"
        f"Intraday news so far:\n{_fmt_intraday_headlines(intraday)}\n\n"
        f"Events:\n{_fmt_events(events)}\n\n"
        f"Price action since open:\n{_fmt_intraday_prices(prices_today)}\n"
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

    # ── 30-DAY PRICE TREND ────────────────────────────────────────────────────
    price_rows: list[str] = [
        "Date       | EURUSD   | DXY     | US10Y  | Gold    | VIX",
        "-" * 65,
    ]
    for i in range(1, config.CONTEXT_DAYS_PRICES + 1):
        date_str = (datetime.now(timezone.utc).date() - timedelta(days=i)).isoformat()
        prices = _read_json(data_dir / date_str / "prices.json")
        if prices:
            price_rows.append(_fmt_price_table_row(date_str, prices))

    sections.append("=== 30-DAY PRICE TREND ===\n" + "\n".join(price_rows) + "\n")

    # ── enforce token budget (trim oldest price rows first) ───────────────────
    def _assemble():
        return "\n".join(sections)

    full = _assemble()
    while _estimate_tokens(full) > config.CONTEXT_MAX_TOKENS and len(price_rows) > 3:
        price_rows.pop()  # remove oldest row (list is most-recent-first)
        sections[-1] = "=== 30-DAY PRICE TREND ===\n" + "\n".join(price_rows) + "\n"
        full = _assemble()

    token_est = _estimate_tokens(full)
    logger.info(f"Context assembled: ~{token_est} tokens ({len(price_rows) - 2} price rows)")
    return full
