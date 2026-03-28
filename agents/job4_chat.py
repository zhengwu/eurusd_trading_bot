"""Job 4 — Conversational Trading Assistant.

Claude Sonnet with tool use. Runs inside the Slack bot as threaded conversations.

Each Slack thread maintains its own conversation history. Claude can call tools
to fetch live data, then propose orders that go through the normal approval flow.

Supported order types via propose_order tool:
  Long / Short  — open new position
  Exit          — fully close a position (requires ticket)
  Trim          — partially close (requires ticket)
  SetSL         — set/update stop loss (requires ticket + sl)
  SetTP         — set/update take profit (requires ticket + tp)
  SetSLTP       — set both SL and TP (requires ticket + sl + tp)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import anthropic

import config
from utils.logger import get_logger

_anthropic_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _anthropic_client

logger = get_logger(__name__)

# ── conversation state ────────────────────────────────────────────────────────

# thread_ts → {"messages": [...], "last_active": datetime}
_threads: dict[str, dict] = {}
_THREAD_EXPIRY_HOURS = 2


def get_active_thread_ids() -> set[str]:
    """Return set of currently active thread IDs (for Slack bot routing)."""
    _cleanup_threads()
    return set(_threads.keys())


def seed_thread(thread_ts: str, assistant_message: str, symbol: str | None = None) -> None:
    """
    Pre-populate a thread with an initial assistant message.
    Used by the `chat` command to inject a market analysis as background
    before the user asks any questions.
    symbol: the currency pair this thread is focused on (defaults to primary pair).
    """
    _cleanup_threads()
    _threads[thread_ts] = {
        "messages": [{"role": "assistant", "content": assistant_message}],
        "last_active": datetime.now(timezone.utc),
        "symbol": symbol or config.MT5_SYMBOL,
    }


def close_thread(thread_ts: str) -> bool:
    """Explicitly close a conversation thread. Returns True if it existed."""
    if thread_ts in _threads:
        del _threads[thread_ts]
        return True
    return False


def _cleanup_threads() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_THREAD_EXPIRY_HOURS)
    expired = [ts for ts, t in _threads.items() if t["last_active"] < cutoff]
    for ts in expired:
        del _threads[ts]


# ── system prompt ─────────────────────────────────────────────────────────────

_SYSTEM_TEMPLATE = """\
You are a professional {display} trading assistant integrated with MetaTrader5.
You help the user understand current market conditions, analyse open positions,
and make informed trading decisions for {display}.

You have tools to fetch live data. Use them when the user asks about current
conditions — don't rely on your training data for prices or news.

When the user wants to take a trading action, use propose_order to create a
formal proposal. The user will approve or reject it via the Slack bot.

Supported actions:
  Long / Short  — open a new position (market order by default)
  Exit          — fully close a position (requires ticket)
  Trim          — partially close (requires ticket)
  SetSL         — update stop loss (requires ticket + sl price)
  SetTP         — update take profit (requires ticket + tp price)
  SetSLTP       — set both SL and TP (requires ticket + sl + tp)

For Long/Short you may also propose a limit order by providing limit_price.
The order will rest in MT5 until the market reaches that price.
When suggesting a limit entry, explain clearly why that level is the better entry.

Be concise but precise. Reference specific prices and data in your analysis.
Always highlight key risks. When proposing orders, explain the rationale clearly.
"""


def _build_system(symbol: str) -> str:
    display = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["display"]
    return _SYSTEM_TEMPLATE.format(display=display)

# ── tool definitions ──────────────────────────────────────────────────────────

_TOOLS = [
    {
        "name": "get_positions",
        "description": "Get all currently open positions from MT5 across all active pairs, including ticket, direction, volume, open price, current price, P&L, SL, and TP.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_account",
        "description": "Get account summary: equity, balance, free margin, margin level, drawdown.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_news",
        "description": "Get recent news articles for the current pair scored by market relevance (1-10).",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "description": "How many hours back to look (default 6, max 24)",
                }
            },
            "required": [],
        },
    },
    {
        "name": "get_market_context",
        "description": "Get full market context for the current pair: recent prices, correlated asset prices (DXY, Gold, US10Y, VIX, etc.), key technical levels, and upcoming economic events.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_pending_signals",
        "description": "Get signals currently pending approval in the signal store.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "modify_order",
        "description": (
            "Modify a pending signal's SL, TP, or lot size before the user approves it. "
            "Recalculates the order preview (risk amount, R:R) after changes. "
            "Use this when the user wants to adjust parameters of a pending signal."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "signal_id": {
                    "type": "string",
                    "description": "8-char signal ID to modify",
                },
                "sl": {
                    "type": "number",
                    "description": "New stop loss price",
                },
                "tp": {
                    "type": "number",
                    "description": "New take profit price",
                },
                "lot_size": {
                    "type": "number",
                    "description": "Override lot size (bypasses risk-based calculation)",
                },
            },
            "required": ["signal_id"],
        },
    },
    {
        "name": "propose_order",
        "description": (
            "Propose a trading action for user approval. "
            "Saves a pending signal to the store and returns the signal ID. "
            "The user must approve it with 'approve <ID>' before it executes on MT5."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["Long", "Short", "Exit", "Trim", "SetSL", "SetTP", "SetSLTP"],
                    "description": "The trading action",
                },
                "ticket": {
                    "type": "integer",
                    "description": "MT5 position ticket (required for Exit/Trim/SetSL/SetTP/SetSLTP)",
                },
                "sl": {
                    "type": "number",
                    "description": "Stop loss price (for SetSL, SetSLTP, Long, Short)",
                },
                "tp": {
                    "type": "number",
                    "description": "Take profit price (for SetTP, SetSLTP, Long, Short)",
                },
                "confidence": {
                    "type": "string",
                    "enum": ["High", "Medium", "Low"],
                },
                "rationale": {
                    "type": "string",
                    "description": "Brief explanation for this action",
                },
                "limit_price": {
                    "type": "number",
                    "description": (
                        "For Long/Short only: place a pending limit order at this price "
                        "instead of a market order. The order fills only when the market "
                        "reaches this level. Omit for market orders."
                    ),
                },
            },
            "required": ["action", "confidence", "rationale"],
        },
    },
]


# ── tool implementations ──────────────────────────────────────────────────────

def _tool_get_positions() -> str:
    try:
        from mt5.connector import is_connected
        from mt5.position_reader import get_open_positions, get_current_tick
        if not is_connected():
            return "MT5 is not connected."
        all_positions = []
        for sym in config.ACTIVE_PAIRS:
            all_positions.extend(get_open_positions(symbol=sym))
        if not all_positions:
            return f"No open positions for {', '.join(config.ACTIVE_PAIRS)}."
        lines = [f"Open positions ({len(all_positions)} total):"]
        for p in all_positions:
            sym = p.get("symbol", config.MT5_SYMBOL)
            pip = config.PAIRS.get(sym, config.PAIRS[config.MT5_SYMBOL])["pip"]
            direction = p["type"].upper()
            pips = (p["current_price"] - p["open_price"]) / pip if direction == "BUY" \
                else (p["open_price"] - p["current_price"]) / pip
            lines.append(
                f"  #{p['ticket']} {sym} {direction} {p['volume']}L @ {p['open_price']} "
                f"| Now: {p['current_price']} | P&L: ${p['profit']:.2f} ({pips:+.1f}pips) "
                f"| SL: {p['sl'] or 'None'} TP: {p['tp'] or 'None'}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching positions: {e}"


def _tool_get_account() -> str:
    try:
        from mt5.connector import is_connected
        from mt5.position_reader import get_account_summary
        if not is_connected():
            return "MT5 is not connected."
        a = get_account_summary()
        equity = a.get("equity")
        balance = a.get("balance")
        drawdown = None
        if equity and balance and balance > 0:
            drawdown = round((balance - equity) / balance * 100, 2)
        return (
            f"Account: equity=${equity} balance=${balance} "
            f"free_margin=${a.get('free_margin')} "
            f"margin_level={a.get('margin_level')}% "
            f"drawdown={drawdown}%"
        )
    except Exception as e:
        return f"Error fetching account: {e}"


def _tool_get_news(hours: int = 6, symbol: str | None = None) -> str:
    try:
        from triage.intraday_logger import read_today_intraday
        records = read_today_intraday()
        if not records:
            return "No news articles recorded today."

        # Filter to the relevant pair if specified
        sym = symbol or config.MT5_SYMBOL
        records = [r for r in records if r.get("symbol", config.MT5_SYMBOL) == sym]

        from utils.date_utils import to_est
        cutoff_est = to_est(datetime.now(timezone.utc) - timedelta(hours=min(hours, 24)))
        cutoff_str = cutoff_est.strftime("%H:%M")

        # Filter by EST time string stored in records
        recent = [r for r in records if r.get("time", "00:00") >= cutoff_str]
        if not recent:
            recent = records  # fall back to all today's articles

        display = config.PAIRS.get(sym, config.PAIRS[config.MT5_SYMBOL])["display"]
        # Sort by score descending, take top 15
        recent.sort(key=lambda x: x.get("triage_score") or 0, reverse=True)
        lines = [f"Recent {display} news (top {min(15, len(recent))} by score, last {hours}h):"]
        for r in recent[:15]:
            score = r.get("triage_score", "?")
            headline = r.get("headline", "")
            tag = r.get("triage_tag", "")
            time = r.get("time", "")
            triggered = " ★" if r.get("triggered_full_analysis") else ""
            lines.append(f"  [{score}] {time} {headline} ({tag}){triggered}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching news: {e}"


def _tool_get_market_context(symbol: str | None = None) -> str:
    try:
        from analysis.context_builder import build_context
        ctx = build_context(trigger_item=None, symbol=symbol or config.MT5_SYMBOL)
        # Cap at 3000 chars to keep token usage reasonable
        return ctx[:3000] + ("\n...[truncated]" if len(ctx) > 3000 else "")
    except Exception as e:
        return f"Error building market context: {e}"


def _tool_get_pending_signals() -> str:
    try:
        from pipeline.signal_store import list_signals
        pending = list_signals(status="pending")
        if not pending:
            return "No pending signals."
        lines = [f"Pending signals ({len(pending)}):"]
        for s in pending:
            lines.append(
                f"  [{s['id']}] {s.get('signal')} [{s.get('confidence')}] "
                f"source={s.get('source')} created={s.get('created_at','')[:16]}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching signals: {e}"


def _tool_modify_order(inputs: dict) -> str:
    try:
        from pipeline.signal_store import get_signal_by_id, update_signal
        signal_id = inputs.get("signal_id", "").upper()
        signal = get_signal_by_id(signal_id)
        if signal is None:
            return f"Signal {signal_id} not found."
        if signal.get("status") != "pending":
            return f"Signal {signal_id} is not pending (status={signal.get('status')}) — cannot modify."

        updates: dict = {}
        if "sl" in inputs:
            updates["sl"] = inputs["sl"]
            levels = dict(signal.get("key_levels") or {})
            action = signal.get("signal")
            if action == "Long":
                levels["support"] = inputs["sl"]
            else:
                levels["resistance"] = inputs["sl"]
            updates["key_levels"] = levels
        if "tp" in inputs:
            updates["tp"] = inputs["tp"]
            levels = dict(signal.get("key_levels") or {})
            action = signal.get("signal")
            if action == "Long":
                levels["resistance"] = inputs["tp"]
            else:
                levels["support"] = inputs["tp"]
            updates["key_levels"] = levels
        if "lot_size" in inputs:
            updates["_lot_override"] = inputs["lot_size"]

        if not updates:
            return "No changes provided — specify sl, tp, or lot_size."

        update_signal(signal_id, updates)

        # Recompute order preview with new values
        updated_signal = {**signal, **updates}
        from agents.job3_executor import compute_order_preview
        preview = compute_order_preview(updated_signal)
        if preview:
            if "lot_size" in inputs:
                preview["lot_size"] = inputs["lot_size"]
                risk = preview.get("risk_amount", 0)
                tp_p = preview.get("tp_pips")
                sl_p = preview.get("sl_pips")
                if sl_p and tp_p and inputs["lot_size"]:
                    preview["risk_reward"] = f"1:{round(tp_p / sl_p, 2)}" if sl_p > 0 else "—"
            update_signal(signal_id, {"_order_preview": preview})
            return (
                f"Signal {signal_id} updated.\n"
                f"Entry: {preview.get('entry_price')}  "
                f"SL: {preview.get('sl')} (-{preview.get('sl_pips')}p)  "
                f"TP: {preview.get('tp')} (+{preview.get('tp_pips')}p)\n"
                f"Lot: {preview.get('lot_size')}  Risk: ${preview.get('risk_amount')}  "
                f"R:R: {preview.get('risk_reward')}\n"
                f"Approve with: approve {signal_id}"
            )
        update_signal(signal_id, updates)
        return f"Signal {signal_id} updated with {list(updates.keys())}. Approve with: approve {signal_id}"
    except Exception as e:
        return f"Error modifying order: {e}"


def _tool_propose_order(inputs: dict, new_proposals: list[str], symbol: str | None = None) -> str:
    try:
        from pipeline.signal_store import save_pending_signal
        action = inputs["action"]
        limit_price = inputs.get("limit_price")
        signal = {
            "signal":       action,
            "confidence":   inputs.get("confidence", "Medium"),
            "time_horizon": "Intraday",
            "rationale":    inputs.get("rationale", ""),
            "key_levels": {
                "support":    inputs.get("sl"),
                "resistance": inputs.get("tp"),
            },
            "invalidation":  "",
            "risk_note":     "",
            "generated_at":  datetime.now(timezone.utc).isoformat(),
            "_symbol":       symbol or config.MT5_SYMBOL,
            "_ticket":       inputs.get("ticket"),
            "_source":       "job4",
            "sl":            inputs.get("sl"),
            "tp":            inputs.get("tp"),
        }
        if limit_price is not None:
            signal["_limit_price"] = float(limit_price)

        signal_id = save_pending_signal(signal, source="job4")
        signal["_signal_id"] = signal_id
        new_proposals.append(signal_id)
        logger.info(f"Job 4 proposed order: {signal_id} — {action}" + (f" limit @ {limit_price}" if limit_price else ""))
        approve_hint = f"approve {signal_id}" + (f" limit {limit_price}" if limit_price else "")
        return (
            f"Order proposed. Signal ID: {signal_id}. "
            f"Tell the user to approve with: {approve_hint}"
        )
    except Exception as e:
        return f"Error proposing order: {e}"


def _execute_tool(
    name: str,
    inputs: dict[str, Any],
    new_proposals: list[str],
    symbol: str | None = None,
) -> str:
    if name == "get_positions":
        return _tool_get_positions()
    elif name == "get_account":
        return _tool_get_account()
    elif name == "get_news":
        return _tool_get_news(hours=inputs.get("hours", 6), symbol=symbol)
    elif name == "get_market_context":
        return _tool_get_market_context(symbol=symbol)
    elif name == "get_pending_signals":
        return _tool_get_pending_signals()
    elif name == "modify_order":
        return _tool_modify_order(inputs)
    elif name == "propose_order":
        return _tool_propose_order(inputs, new_proposals, symbol=symbol)
    else:
        return f"Unknown tool: {name}"


# ── main chat function ────────────────────────────────────────────────────────

def chat(thread_ts: str, user_message: str) -> tuple[str, list[str]]:
    """
    Process a user message in a conversation thread.
    Returns (assistant_reply, list_of_new_signal_ids_proposed).
    """
    _cleanup_threads()

    if thread_ts not in _threads:
        _threads[thread_ts] = {
            "messages":    [],
            "last_active": datetime.now(timezone.utc),
            "symbol":      config.MT5_SYMBOL,
        }

    thread = _threads[thread_ts]
    thread["last_active"] = datetime.now(timezone.utc)
    thread["messages"].append({"role": "user", "content": user_message})

    symbol = thread.get("symbol", config.MT5_SYMBOL)
    system = _build_system(symbol)

    client = _get_client()
    messages = list(thread["messages"])
    new_proposals: list[str] = []
    final_reply = ""

    # Agentic loop — keep going until end_turn or no more tool calls
    while True:
        response = client.messages.create(
            model=config.ANALYSIS_MODEL,
            max_tokens=1024,
            system=system,
            tools=_TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    final_reply = block.text
            break

        elif response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = _execute_tool(block.name, block.input, new_proposals, symbol=symbol)
                    logger.debug(f"Tool {block.name} → {result[:100]}")
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     result,
                    })
            messages.append({"role": "user", "content": tool_results})

        else:
            # Unexpected stop reason — extract any text and break
            for block in response.content:
                if hasattr(block, "text"):
                    final_reply = block.text
            break

    # Save only the final assistant text to thread history
    if final_reply:
        thread["messages"].append({"role": "assistant", "content": final_reply})

    return final_reply, new_proposals
