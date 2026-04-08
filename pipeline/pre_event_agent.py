"""Pre-event analysis agent.

Runs each scan cycle (via scanner.py). Two modes:

1. PRE-EVENT BRIEF (45–75 min before a high-impact release):
   Uses Claude Haiku to generate a scenario brief and posts it to Slack:
   "If actual > forecast → USD bullish → EURUSD likely short setup at X.
    If actual < forecast → USD bearish → EURUSD likely long setup at X."
   Deduped per event per day — sends at most once.

2. POST-EVENT TRIGGER (actual released in last scan cycle, significant beat/miss):
   - Bypasses cooldown
   - Triggers a full analysis with a context note about the event outcome
   Deduped per event — triggers at most once.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv

import config
from utils.logger import get_logger

load_dotenv()
logger = get_logger(__name__)

# ── dedup store ───────────────────────────────────────────────────────────────
# Tracks which pre-event briefs and post-event triggers have already fired.
# Format: { "<event_key>_brief": "<iso_timestamp>", "<event_key>_trigger": "..." }
# Entries expire after 2 days.

_DEDUP_PATH = config.DATA_DIR / ".pre_event_dedup.json"


def _load_dedup() -> dict[str, str]:
    try:
        if _DEDUP_PATH.exists():
            return json.loads(_DEDUP_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_dedup(d: dict[str, str]) -> None:
    try:
        _DEDUP_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Pre-event dedup save failed: {e}")


def _event_key(event: dict) -> str:
    name = event.get("event", "unknown").replace(" ", "_")[:40]
    date = event.get("release_date", "")[:10]
    return f"{name}_{date}"


def _is_deduped(key: str) -> bool:
    d = _load_dedup()
    return key in d


def _mark_dedup(key: str) -> None:
    d = _load_dedup()
    # Expire entries older than 2 days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    d = {k: v for k, v in d.items() if v >= cutoff}
    d[key] = datetime.now(timezone.utc).isoformat()
    _save_dedup(d)


# ── calendar helpers ──────────────────────────────────────────────────────────

def _get_today_events() -> list[dict]:
    """Load today's events from the data directory (already refreshed by scanner)."""
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        path = config.DATA_DIR / today / "events.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug(f"Pre-event: could not load today events: {e}")
    return []


def _parse_event_dt(event: dict) -> datetime | None:
    """Parse the ForexFactory datetime string into a UTC-aware datetime."""
    time_str = event.get("time", "")
    if not time_str:
        return None
    try:
        # Format: "2026-03-15T13:30:00-0400"
        # Python 3.7+ can parse %z with offset like -0400
        dt = datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S%z")
        return dt.astimezone(timezone.utc)
    except ValueError:
        try:
            # Fallback: try parsing without offset as UTC
            dt = datetime.fromisoformat(time_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None


def _pair_currencies(symbol: str) -> set[str]:
    """Return the set of relevant currencies for a trading pair."""
    mapping = {
        "EURUSD": {"USD", "EUR"},
        "GBPUSD": {"USD", "GBP"},
        "USDJPY": {"USD", "JPY"},
        "EURJPY": {"EUR", "JPY"},
        "GBPJPY": {"GBP", "JPY"},
        "EURGBP": {"EUR", "GBP"},
    }
    return mapping.get(symbol.upper(), {"USD"})


# ── pre-event brief (45–75 min before release) ────────────────────────────────

def get_upcoming_high_impact(
    symbol: str,
    lookahead_min_low: int | None = None,
    lookahead_min_high: int | None = None,
) -> list[dict]:
    """
    Return high-impact events between lookahead_min_low and lookahead_min_high
    minutes away from now, relevant to the given symbol.
    Defaults come from config so the window can be tuned without code changes.
    """
    low = lookahead_min_low  if lookahead_min_low  is not None else config.PRE_EVENT_BRIEF_MIN
    high = lookahead_min_high if lookahead_min_high is not None else config.PRE_EVENT_BRIEF_MAX
    now        = datetime.now(timezone.utc)
    cutoff_low = now + timedelta(minutes=low)
    cutoff_hi  = now + timedelta(minutes=high)
    currencies = _pair_currencies(symbol)

    results = []
    for ev in _get_today_events():
        if ev.get("impact") != "high":
            continue
        if ev.get("currency") not in currencies:
            continue
        dt = _parse_event_dt(ev)
        if dt is None:
            continue
        if cutoff_low <= dt <= cutoff_hi:
            results.append(ev)
    return results


def _generate_pre_event_brief(event: dict, symbol: str) -> str | None:
    """
    Use Claude Haiku to generate a scenario brief for an upcoming high-impact event.
    Returns a short Slack message string, or None on failure.
    """
    try:
        ev_name    = event.get("event", "Unknown event")
        currency   = event.get("currency", "")
        forecast   = event.get("forecast")
        previous   = event.get("previous")
        ev_time_dt = _parse_event_dt(event)
        ev_time    = ev_time_dt.strftime("%H:%M UTC") if ev_time_dt else "soon"
        display    = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL]).get("display", symbol)

        forecast_str = f"{forecast}" if forecast is not None else "unknown"
        previous_str = f"{previous}" if previous is not None else "unknown"

        prompt = (
            f"A high-impact {currency} economic release is coming up in ~60 minutes: "
            f"**{ev_name}** at {ev_time}.\n"
            f"Forecast: {forecast_str}  |  Previous: {previous_str}\n\n"
            f"The active trading pair is {display}.\n\n"
            f"Write a concise pre-event scenario brief (max 120 words) for a forex trader. "
            f"Cover two scenarios: (1) if actual beats forecast — what does it mean for {currency} "
            f"and {display}? What price direction? "
            f"(2) if actual misses forecast — same questions. "
            f"End with: 'Monitor price reaction at release for entry.' "
            f"Be specific: mention buy/sell direction, not abstract macro commentary. "
            f"Format as plain text (no markdown headers)."
        )

        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        msg = client.messages.create(
            model=config.PRE_EVENT_BRIEF_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        logger.error(f"Pre-event brief generation failed: {e}")
        return None


def run_pre_event_briefs(symbol: str) -> None:
    """
    Check for upcoming high-impact events and send pre-event briefs to Slack.
    Called each scan cycle. Deduped — each event brief is sent at most once.
    """
    events = get_upcoming_high_impact(symbol)
    for ev in events:
        key = _event_key(ev) + "_brief"
        if _is_deduped(key):
            continue

        ev_name = ev.get("event", "Unknown")
        ev_time_dt = _parse_event_dt(ev)
        ev_time = ev_time_dt.strftime("%H:%M UTC") if ev_time_dt else "soon"
        logger.info(f"[{symbol}] Pre-event brief: {ev_name} at {ev_time}")

        brief = _generate_pre_event_brief(ev, symbol)
        if not brief:
            continue

        display = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL]).get("display", symbol)
        forecast_str = (f" (forecast: {ev.get('forecast')})" if ev.get("forecast") is not None else "")
        message = (
            f":calendar: *Pre-event brief — {ev_name}{forecast_str} [{ev_time}]*\n"
            f"*Pair:* {display}\n\n{brief}"
        )

        try:
            from notifications.notifier import notify_text
            notify_text(message)
            _mark_dedup(key)
            logger.info(f"[{symbol}] Pre-event brief sent for: {ev_name}")
        except Exception as e:
            logger.error(f"[{symbol}] Pre-event Slack send failed: {e}")


# ── post-event trigger (actual just released) ─────────────────────────────────

def _is_significant_surprise(event: dict) -> bool:
    """
    Return True if the surprise magnitude clears the minimum threshold
    configured in config.PRE_EVENT_MIN_SURPRISE.
    Matches on keywords in the event name; falls back to DEFAULT.
    """
    magnitude = event.get("surprise_magnitude")
    if magnitude is None:
        return False
    magnitude = abs(magnitude)

    name_upper = event.get("event", "").upper()
    thresholds = config.PRE_EVENT_MIN_SURPRISE

    if "NONFARM" in name_upper or "NFP" in name_upper or "PAYROLL" in name_upper:
        threshold = thresholds.get("NFP", thresholds["DEFAULT"])
    elif "CPI" in name_upper or "INFLATION" in name_upper or "PCE" in name_upper:
        threshold = thresholds.get("CPI", thresholds["DEFAULT"])
    elif "GDP" in name_upper or "GROWTH" in name_upper:
        threshold = thresholds.get("GDP", thresholds["DEFAULT"])
    elif "RATE" in name_upper or "INTEREST" in name_upper or "FOMC" in name_upper:
        threshold = thresholds.get("RATE", thresholds["DEFAULT"])
    else:
        threshold = thresholds["DEFAULT"]

    return magnitude >= threshold


def get_fresh_releases(symbol: str, lookback_min: int = 35) -> list[dict]:
    """
    Return high-impact events that:
    - Have a non-None actual (just released)
    - Released in the last lookback_min minutes
    - Have a clear beat or miss (not inline)
    - Surprise magnitude exceeds the per-event-type threshold in config
    - Are relevant to this symbol's currencies
    """
    now        = datetime.now(timezone.utc)
    cutoff     = now - timedelta(minutes=lookback_min)
    currencies = _pair_currencies(symbol)

    results = []
    for ev in _get_today_events():
        if ev.get("impact") != "high":
            continue
        if ev.get("currency") not in currencies:
            continue
        if ev.get("actual") is None:
            continue
        if ev.get("surprise") not in ("beat", "miss"):
            continue
        if not _is_significant_surprise(ev):
            logger.debug(
                f"Skipping minor surprise: {ev.get('event')} "
                f"magnitude={ev.get('surprise_magnitude')}"
            )
            continue
        dt = _parse_event_dt(ev)
        if dt is None:
            continue
        if cutoff <= dt <= now:
            results.append(ev)
    return results


def run_post_event_triggers(symbol: str) -> list[dict]:
    """
    Check for fresh event releases and trigger full analysis for significant ones.
    Bypasses cooldown. Returns list of events that triggered analysis.
    Called each scan cycle. Deduped — each event triggers at most once.
    """
    triggered = []
    for ev in get_fresh_releases(symbol):
        key = _event_key(ev) + "_trigger"
        if _is_deduped(key):
            continue

        ev_name    = ev.get("event", "Unknown")
        currency   = ev.get("currency", "")
        actual     = ev.get("actual")
        forecast   = ev.get("forecast")
        surprise   = ev.get("surprise", "")
        magnitude  = ev.get("surprise_magnitude")
        ev_time_dt = _parse_event_dt(ev)
        ev_time    = ev_time_dt.strftime("%H:%M UTC") if ev_time_dt else "N/A"

        # Compute exact elapsed time since release
        now_trigger = datetime.now(timezone.utc)
        if ev_time_dt is not None:
            elapsed_sec = int((now_trigger - ev_time_dt).total_seconds())
            elapsed_min = elapsed_sec // 60
            elapsed_sec_part = elapsed_sec % 60
            elapsed_str = (
                f"{elapsed_min}m {elapsed_sec_part}s"
                if elapsed_min > 0 else f"{elapsed_sec_part}s"
            )
        else:
            elapsed_str = "unknown"

        logger.info(
            f"[{symbol}] Post-event trigger: {ev_name} "
            f"actual={actual} forecast={forecast} surprise={surprise} "
            f"elapsed={elapsed_str}"
        )

        try:
            from analysis.context_builder import build_context
            from analysis.full_analysis_prompt import run_full_analysis
            from analysis.signal_formatter import format_signal
            from notifications.notifier import notify
            from triage.cooldown import set_cooldown

            # Build trigger item — surfaces as the headline in context
            mag_str = f"{magnitude:+.4f}" if magnitude is not None else ""
            trigger_item = {
                "title": (
                    f"RELEASE: {ev_name} ({currency}) actual={actual} "
                    f"vs forecast={forecast} [{surprise.upper()} {mag_str}]"
                ),
                "headline": (
                    f"{ev_name}: actual={actual}, forecast={forecast}, "
                    f"previous={ev.get('previous')} — {surprise.upper()} at {ev_time}"
                ),
                "score": 10,
                "tag": "economic_data",
                "symbol": symbol,
                "triggered_full_analysis": True,
                "_pre_event_context": (
                    f"EVENT OUTCOME: {ev_name} ({currency})\n"
                    f"  Released at: {ev_time}  |  Analysis triggered: "
                    f"{now_trigger.strftime('%H:%M UTC')}  |  "
                    f"Elapsed since release: {elapsed_str}\n"
                    f"  Actual: {actual}  |  Forecast: {forecast}  |  "
                    f"Previous: {ev.get('previous')}\n"
                    f"  Result: {surprise.upper()}"
                    + (f" by {mag_str}" if mag_str else "")
                    + f"\n"
                    f"  Implications: {currency} "
                    + ("STRENGTH (beat) — bullish for {currency} leg of {symbol}"
                       .format(currency=currency, symbol=symbol)
                       if surprise == "beat" else
                       "WEAKNESS (miss) — bearish for {currency} leg of {symbol}"
                       .format(currency=currency, symbol=symbol))
                    + f"\n"
                    f"  TIMING NOTE: The initial spike from this release occurred "
                    f"{elapsed_str} ago. Do NOT chase the initial move. Look for "
                    f"second-wave continuation setups or mean-reversion if the spike "
                    f"was excessive — check current price vs key levels and Bollinger position."
                ),
            }

            context = build_context(symbol=symbol, trigger_item=trigger_item)
            raw_signal = run_full_analysis(context, symbol=symbol)
            signal = format_signal(raw_signal)
            signal["_symbol"] = symbol
            signal["_triggered_by"] = f"post_event:{ev_name}"

            if signal.get("signal") in ("Long", "Short"):
                from pipeline.signal_store import save_pending_signal
                from agents.job3_executor import compute_order_preview

                preview = compute_order_preview(signal, symbol=symbol)
                if preview:
                    signal["_order_preview"] = preview

                if config.USE_MULTI_AGENT_DEBATE:
                    try:
                        from analysis.ensemble_agent import run_ensemble_debate
                        run_ensemble_debate(context, signal, symbol=symbol)
                        if signal.get("sl") or signal.get("tp"):
                            updated_preview = compute_order_preview(signal, symbol=symbol)
                            if updated_preview:
                                signal["_order_preview"] = updated_preview
                    except Exception as e:
                        logger.error(f"[{symbol}] Post-event debate failed: {e}")

                signal_id = save_pending_signal(signal, source="pre_event")
                signal["_signal_id"] = signal_id
                signal["_source"] = "pre_event"
                logger.info(f"[{symbol}] Post-event signal saved: {signal_id}")

            # Set cooldown AFTER triggering so this counts as an analysis cycle
            set_cooldown(symbol)
            notify(signal, trigger_item=trigger_item)
            _mark_dedup(key)
            triggered.append(ev)
            logger.info(
                f"[{symbol}] Post-event analysis complete: {signal.get('signal')} "
                f"[{signal.get('confidence')}] triggered by {ev_name}"
            )

        except Exception as e:
            logger.error(f"[{symbol}] Post-event analysis failed for {ev_name}: {e}", exc_info=True)

    return triggered


# ── public entry point ────────────────────────────────────────────────────────

def run_pre_event_check(symbol: str) -> None:
    """
    Run both pre-event brief check and post-event trigger check for a pair.
    Called from scanner.py each scan cycle.
    """
    try:
        run_pre_event_briefs(symbol)
    except Exception as e:
        logger.error(f"[{symbol}] Pre-event brief check failed: {e}", exc_info=True)

    try:
        run_post_event_triggers(symbol)
    except Exception as e:
        logger.error(f"[{symbol}] Post-event trigger check failed: {e}", exc_info=True)
