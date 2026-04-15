"""Full LLM analysis — Claude Sonnet for detailed EUR/USD signal generation.

Triggered by triage escalation (score >= threshold, cooldown clear).
Returns a parsed signal dict; logs raw response on parse failure.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import anthropic
from dotenv import load_dotenv

import config
from utils.logger import get_logger
from utils.retry import call_with_retry

load_dotenv()
logger = get_logger(__name__)

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _client


_SYSTEM_TEMPLATE = (
    "You are a professional {display} forex analyst with deep expertise in "
    "macroeconomics, central bank policy, and technical analysis."
)

_PROMPT_TEMPLATE = """\
You are a professional {display} forex analyst and institutional trader.
You have been given a rich context including: recent news, economic events, market regime data, \
and pre-computed technical indicators across three timeframes (D1, H4, M15).

Your goal is to identify high-probability trading setups using a top-down multi-timeframe approach.

ANALYTICAL PROCESS — follow this order:
1. MACRO CONTEXT: What is the dominant macro/news narrative driving the pair right now?
2. MULTI-TIMEFRAME STRUCTURE: Read the "Multi-timeframe confluence" section in the price summary.
   This is your primary structural anchor. A BEARISH/BULLISH ALIGNED verdict significantly raises \
signal confidence. A CONFLICTED verdict should result in Wait unless the macro catalyst is overwhelming.
3. MOMENTUM CHECK: Use MACD (D1) — is momentum confirming the MTF direction?
   Use H4 RSI — overbought (>70) warns against longs, oversold (<30) warns against shorts.
4. MEAN-REVERSION vs TREND-CONTINUATION: Use Bollinger Bands (D1).
   Price above 80% of band range = extended, mean-reversion risk. Price below 20% = potential bounce.
   Combine with ADX: ADX > 25 favours trend continuation; ADX < 20 favours range / mean-reversion.
5. ENTRY PRECISION: Use H4 EMA 20/50 and M15 structure for entry zone. Where is the nearest \
H4 EMA acting as dynamic support/resistance?
6. STOP LOSS RULE: Never place SL at an obvious level. Pad by at least 1×ATR-14 (D1) beyond \
the nearest key level. Wider SL in high-volatility regimes.
7. RISK:REWARD RULE: Only output Long or Short if TP distance >= {min_rr}× SL distance \
(R:R >= 1:{min_rr}). Use the next significant technical level as TP — do NOT project TP \
arbitrarily to hit a ratio. If the nearest real TP level does not give R:R >= 1:{min_rr}, \
output Wait. A structurally sound Wait is better than a negative-edge trade.

{context_window}

Provide your analysis in the following JSON format. Use chain-of-thought reasoning: complete the \
analytical fields first to build your thesis, THEN output the signal and trade setup.

{{
  "today_summary": "1-2 sentences: what drove price action today, key news/events",
  "regime_alignment": "Explain how your trade direction and Stop Loss width align with the current \
Risk Sentiment, Technical Trend, and Volatility regimes. If no regime data is available, write N/A.",
  "mtf_confluence": "State the D1/H4/M15 trend directions from the price summary and the confluence \
verdict (ALIGNED / LEANING / CONFLICTED). Explain how this shapes your bias and confidence level.",
  "technical_rationale": "Describe the full TA picture: MTF structure, MACD momentum, Bollinger \
position (extended or mid-range?), H4 EMA support/resistance, key levels. Where is the liquidity? \
What structure are you trading? Why is this setup high-probability given the confluence?",
  "signal": "Long | Short | Wait",
  "confidence": "High | Medium | Low",
  "time_horizon": "Intraday | 1-3 days | This week",
  "trade_setup": {{
    "entry_price": 0.0000,
    "stop_loss": 0.0000,
    "take_profit": 0.0000,
    "risk_reward_ratio": "e.g., 1:2.5",
    "stop_loss_reasoning": "Explain why this SL placement avoids sweeps — reference ATR-14, \
nearest key level, and volatility regime"
  }},
  "key_levels": {{
    "support": 0.0000,
    "resistance": 0.0000
  }},
  "price_snapshot": {{
    "current": 0.0000,
    "session_open": 0.0000,
    "session_high": 0.0000,
    "session_low": 0.0000,
    "session_change_pct": "+0.00%",
    "trend": "e.g., Bearish — BEARISH ALIGNED across D1/H4/M15, MACD bearish, below EMA20 H4"
  }},
  "invalidation": "What macro or technical development would invalidate this signal",
  "risk_note": "Any asymmetric risks or upcoming events that could disrupt the setup"
}}

Return ONLY valid JSON, no other text."""


def run_full_analysis(context_window: str, symbol: str | None = None) -> dict[str, Any]:
    """
    Run full analysis using Claude Sonnet for the given currency pair.
    Returns parsed signal dict. Returns a safe default on failure.
    """
    sym = symbol or config.MT5_SYMBOL
    display = config.PAIRS.get(sym, config.PAIRS[config.MT5_SYMBOL])["display"]
    system = _SYSTEM_TEMPLATE.format(display=display)
    prompt = _PROMPT_TEMPLATE.format(
        display=display,
        context_window=context_window,
        min_rr=config.JOB3_MIN_RR,
    )

    try:
        client = _get_client()
        message = call_with_retry(
            client.messages.create,
            model=config.ANALYSIS_MODEL,
            max_tokens=12000,
            thinking={"type": "enabled", "budget_tokens": 10000},
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )

        # Log token usage so we can evaluate whether the 10K thinking budget is sufficient.
        # output_tokens includes thinking + text; thinking_chars is a rough proxy for
        # thinking token count (1 token ≈ 4 chars) when the thinking block is available.
        usage = message.usage
        thinking_block = next((b for b in message.content if b.type == "thinking"), None)
        thinking_chars = len(thinking_block.thinking) if thinking_block else 0
        thinking_tokens_est = thinking_chars // 4
        logger.info(
            f"[{sym}] Full analysis tokens — "
            f"input={usage.input_tokens} output={usage.output_tokens} "
            f"(thinking ~{thinking_tokens_est} est, budget=10000)"
        )
        if thinking_tokens_est >= 9000:
            logger.warning(
                f"[{sym}] Thinking budget near limit (~{thinking_tokens_est}/10000 est) "
                f"— consider raising budget_tokens in full_analysis_prompt.py"
            )

        # With thinking enabled, content is [ThinkingBlock, TextBlock].
        # Find the text block explicitly rather than assuming index 0.
        text_block = next(b for b in message.content if b.type == "text")
        raw = text_block.text.strip()
    except Exception as e:
        logger.error(f"Full analysis LLM call failed after retries: {e}")
        return {
            "signal": "Wait",
            "confidence": "Low",
            "rationale": f"Analysis unavailable: {e}",
        }

    # Strip markdown fences if model wraps output in ```json ... ```
    clean = re.sub(r"^```[a-z]*\n?", "", raw)
    clean = re.sub(r"\n?```$", "", clean).strip()

    try:
        result = json.loads(clean)
        if not isinstance(result, dict):
            raise ValueError("Expected JSON object")
        return result
    except Exception as e:
        logger.error(f"Failed to parse full analysis response: {e}\nRaw: {raw[:1000]}")
        return {
            "signal": "Wait",
            "confidence": "Low",
            "rationale": "Parse error — check logs for raw LLM output",
            "_raw": raw[:2000],
        }
