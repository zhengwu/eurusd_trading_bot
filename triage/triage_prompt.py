"""Tier 1 triage — fast LLM scoring of headlines using Claude Haiku.

Scores each headline 1-10 for EUR/USD impact and assigns a tag.
Uses a cheap/fast model to run every 30 minutes without burning budget.
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


_SYSTEM = "You are a forex market analyst specialising in EUR/USD."

_PROMPT_TEMPLATE = """\
Score each headline for its potential impact on EUR/USD in the next 4 hours.

Scoring guide:
  9-10: Immediate major move likely (war, emergency CB rate decision, sovereign default,
        major geopolitical shock)
  6-8:  Significant — warrants full analysis (CB speeches, CPI/NFP surprises,
        escalating geopolitical tension, major risk-off moves)
  3-5:  Moderate — log only (scheduled data in line with forecasts, routine CB commentary)
  1-2:  Minimal impact (unrelated news, market colour, opinion pieces)

Also assign one tag per headline:
  geopolitical | CB_speech | macro_data | risk_off | risk_on | other

Headlines:
{headlines_json}

Return ONLY valid JSON, no other text:
[
  {{"headline": "...", "score": 8, "tag": "CB_speech", "reason": "..."}}
]"""


def triage_headlines(headlines: list[str]) -> list[dict[str, Any]]:
    """
    Score a list of headlines using Claude Haiku.
    Returns list of {headline, score, tag, reason}.
    Falls back to score=5 / tag="other" on LLM or parse failure.
    """
    if not headlines:
        return []

    prompt = _PROMPT_TEMPLATE.format(
        headlines_json=json.dumps(headlines, ensure_ascii=False, indent=2)
    )

    try:
        client = _get_client()
        message = client.messages.create(
            model=config.TRIAGE_MODEL,
            max_tokens=1024,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
    except Exception as e:
        logger.error(f"Triage LLM call failed: {e}")
        return [
            {"headline": h, "score": 5, "tag": "other", "reason": "triage unavailable"}
            for h in headlines
        ]

    # Strip markdown fences if the model wraps in ```json ... ```
    clean = re.sub(r"^```[a-z]*\n?", "", raw)
    clean = re.sub(r"\n?```$", "", clean).strip()

    try:
        results = json.loads(clean)
        if not isinstance(results, list):
            raise ValueError("Expected JSON array")
        return results
    except Exception as e:
        logger.error(f"Failed to parse triage response: {e}\nRaw: {raw[:500]}")
        return [
            {"headline": h, "score": 5, "tag": "other", "reason": "parse error"}
            for h in headlines
        ]
