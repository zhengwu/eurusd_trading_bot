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

load_dotenv()
logger = get_logger(__name__)

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _client


_SYSTEM = (
    "You are a professional EUR/USD forex analyst with deep expertise in "
    "macroeconomics, central bank policy, and technical analysis."
)

_PROMPT_TEMPLATE = """\
You are a professional EUR/USD forex analyst. You have been given a time-series
context of recent news, economic events, and price data.

A significant event has triggered this analysis. Review all context carefully,
paying attention to trends, not just the latest data point.

{context_window}

Provide your analysis in the following JSON format:
{{
  "signal": "Long | Short | Wait",
  "confidence": "High | Medium | Low",
  "time_horizon": "Intraday | 1-3 days | This week",
  "rationale": "2-3 sentence explanation referencing specific data points",
  "key_levels": {{"support": 0.0000, "resistance": 0.0000}},
  "invalidation": "What would prove this signal wrong",
  "risk_note": "Any asymmetric risks or upcoming events that could disrupt"
}}

Return ONLY valid JSON, no other text."""


def run_full_analysis(context_window: str) -> dict[str, Any]:
    """
    Run full EUR/USD analysis using Claude Sonnet.
    Returns parsed signal dict. Returns a safe default on failure.
    """
    prompt = _PROMPT_TEMPLATE.format(context_window=context_window)

    try:
        client = _get_client()
        message = client.messages.create(
            model=config.ANALYSIS_MODEL,
            max_tokens=1024,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
    except Exception as e:
        logger.error(f"Full analysis LLM call failed: {e}")
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
