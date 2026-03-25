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
You have been given a time-series context of recent news, economic events, price data, and a pre-computed market regime summary.

Your goal is to identify high-probability trading setups.
CRITICAL RULES FOR STOP LOSS: Markets are noisy. Never place a Stop Loss exactly on an obvious support/resistance line. You MUST pad the Stop Loss using the provided volatility (ATR) context to avoid being stopped out by liquidity sweeps/fake-outs.

{context_window}

Provide your analysis in the following JSON format. Use chain-of-thought reasoning: complete the analytical fields first to build your thesis, then output the signal and trade setup.

{{
  "today_summary": "1-2 sentences: what drove price action today, key news/events",
  "regime_alignment": "Explain how your trade direction and Stop Loss width align with the current Risk Sentiment, Technical Trend, and Volatility regimes provided in the context. If no regime data is available, write N/A.",
  "technical_rationale": "Analyze the price action. Where is the liquidity? Where are the traps? What structure are you trading?",
  "signal": "Long | Short | Wait",
  "confidence": "High | Medium | Low",
  "time_horizon": "Intraday | 1-3 days | This week",
  "trade_setup": {{
    "entry_price": 0.0000,
    "stop_loss": 0.0000,
    "take_profit": 0.0000,
    "risk_reward_ratio": "e.g., 1:2.5",
    "stop_loss_reasoning": "Explain why this SL placement avoids standard market noise/sweeps based on ATR and volatility regime"
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
    "trend": "e.g., Bullish — above SMA50/200, positive momentum on M15"
  }},
  "invalidation": "What fundamentally or technically would prove this signal wrong",
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
    prompt = _PROMPT_TEMPLATE.format(display=display, context_window=context_window)

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
