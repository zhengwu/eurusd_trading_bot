"""Tier 1 triage — fast LLM scoring of headlines.

Two-pass scoring to reduce score clustering around 5:
  Pass 1: Score all headlines — uses Azure GPT-5.2 if available, else Claude Haiku.
  Pass 2: Re-evaluate borderline scores (4–6) — same model preference.
  Both passes fall back to Claude Haiku if Azure is unavailable.
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

_claude_client: anthropic.Anthropic | None = None
_azure_client = None   # openai.AzureOpenAI, lazy-initialised


def _get_claude_client() -> anthropic.Anthropic:
    global _claude_client
    if _claude_client is None:
        _claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _claude_client


def _get_azure_client():
    """Return AzureOpenAI client if key+endpoint are configured, else None."""
    global _azure_client
    if _azure_client is not None:
        return _azure_client
    key      = os.getenv("AZURE_OPENAI_API_KEY", "").strip()
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip()
    version  = os.getenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview").strip()
    if not key or not endpoint:
        return None
    try:
        from openai import AzureOpenAI
        _azure_client = AzureOpenAI(
            api_key=key,
            azure_endpoint=endpoint,
            api_version=version,
        )
        logger.info("Azure OpenAI client initialised for triage pass 2")
    except Exception as e:
        logger.warning(f"Azure OpenAI init failed: {e} — pass 2 will use Haiku fallback")
        _azure_client = None
    return _azure_client


_SYSTEM_TEMPLATE = "You are a professional forex market analyst specialising in {display}."

# ── Pass 1: score all headlines ────────────────────────────────────────────────

_PASS1_TEMPLATE = """\
Score each headline for its potential impact on {display} in the next 4 hours.
Be decisive — commit to a score. Avoid defaulting to 5.

SCORING SCALE with calibrated examples:

  9-10 IMMEDIATE MAJOR MOVE (rare — maybe 2-3 times per year)
       Examples: Emergency Fed/ECB rate decision outside scheduled meeting;
                 major sovereign default; surprise military escalation;
                 "Fed cuts 75bps emergency" / "ECB halts QT immediately"
       → Price will gap or move 80+ pips within minutes

  7-8  HIGH IMPACT — full analysis required
       Examples: NFP/CPI print significantly off forecast (±30K jobs, ±0.2pp CPI);
                 Fed/ECB chair hawkish/dovish surprise in live speech;
                 major geopolitical shock (new sanctions, escalation, ceasefire);
                 unexpected policy shift; flash crash / circuit breaker event
       → Likely 30-80 pip move within 4 hours

  6    MODERATE-HIGH — warrants analysis
       Examples: CB official comment that shifts rate expectations;
                 data slightly off forecast (NFP ±15K, CPI ±0.1pp);
                 trade deal / tariff headline with direct USD/EUR impact;
                 risk-off trigger (VIX spike, equity flash sell-off)
       → Possible 15-30 pip directional move

  4-5  MODERATE — log only, do not escalate
       Examples: Scheduled data in line with forecasts;
                 routine CB speaker reiterating known policy;
                 general macro commentary without new information;
                 cross-pair technicals with indirect pair relevance
       → Minor drift, no clear directional bias

  1-3  MINIMAL — log only
       Examples: Opinion pieces, market recap, TA-only articles;
                 news about unrelated assets (crypto, individual stocks);
                 duplicate/reworded coverage of existing headlines
       → No expected impact

IMPORTANT RULES:
- If a headline mentions a SPECIFIC NUMBER (rate change, data print, pip move),
  score it at least 6 unless the number is expected/in-line with forecasts.
- Geopolitical events: score based on USD/EUR direct impact, not general drama.
- "Trump tariff" headlines: score 7-8 if new/escalating, 4-5 if restatement.
- Score 9-10 only for genuinely unexpected emergency events.
- Score 1-2 for pure technical analysis articles (EMA, support/resistance).

Assign one tag per headline:
  geopolitical | cb_decision | CB_speech | macro_data | risk_off | risk_on | other

  cb_decision : confirmed rate decision or policy statement from a central bank meeting
  CB_speech   : governor speech, press conference, interview, or unofficial commentary

For ANY cb_decision or CB_speech headline, identify the central bank:
  cb_bank: "Fed" | "ECB" | "BOE" | "BOJ" | null

Headlines to score:
{headlines_json}

Return ONLY valid JSON — no prose, no markdown fences:
[
  {{"headline": "...", "score": 7, "tag": "macro_data", "cb_bank": null, "reason": "..."}}
]"""

# ── Pass 2: re-evaluate borderline scores ─────────────────────────────────────

_PASS2_TEMPLATE = """\
You are reviewing borderline triage scores (4-6) for {display} headlines.
These scores are uncertain — your job is to push each one decisively up or down.

For each headline below, decide:
  - Score UP to 7+ if: the headline contains a specific number, implies a policy shift,
    describes an unexpected event, or could cause a 15+ pip move.
  - Keep at 5-6 if: genuinely ambiguous, unclear impact, or borderline relevance.
  - Score DOWN to 1-3 if: purely technical analysis, opinion, market recap,
    or clearly no directional impact on {display}.

Borderline headlines to re-evaluate:
{borderline_json}

Return ONLY valid JSON — no prose, no markdown fences:
[
  {{"headline": "...", "score": 7, "tag": "macro_data", "cb_bank": null, "reason": "..."}}
]"""


def _call_claude(system: str, prompt: str) -> str:
    client = _get_claude_client()
    message = call_with_retry(
        client.messages.create,
        model=config.TRIAGE_MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


def _call_azure(system: str, prompt: str) -> str:
    """Call Azure OpenAI GPT-5.2. Raises on failure so caller can fallback to Claude."""
    client = _get_azure_client()
    if client is None:
        raise RuntimeError("Azure client not available")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5.2")
    resp = client.chat.completions.create(
        model=deployment,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ],
        max_completion_tokens=1024,
        temperature=0.1,
    )
    content = resp.choices[0].message.content
    if not content:
        finish = resp.choices[0].finish_reason
        raise RuntimeError(f"Azure returned empty content (finish_reason={finish})")
    return content.strip()


def _parse_json_array(raw: str) -> list[dict]:
    clean = re.sub(r"^```[a-z]*\n?", "", raw)
    clean = re.sub(r"\n?```$", "", clean).strip()
    result = json.loads(clean)
    if not isinstance(result, list):
        raise ValueError("Expected JSON array")
    return result


def triage_headlines(headlines: list[str], symbol: str | None = None) -> list[dict[str, Any]]:
    """
    Score headlines in two passes to reduce score clustering around 5.

    Pass 1: score all headlines.
    Pass 2: re-evaluate scores 4-6 to push them decisively up or down.

    Returns list of {headline, score, tag, cb_bank, reason}.
    Falls back to score=5 / tag="other" on failure.
    """
    if not headlines:
        return []

    sym = symbol or config.MT5_SYMBOL
    display = config.PAIRS.get(sym, config.PAIRS[config.MT5_SYMBOL])["display"]
    system = _SYSTEM_TEMPLATE.format(display=display)

    # ── Pass 1: score everything (Azure GPT-5.2 → Haiku fallback) ───────────
    pass1_prompt = _PASS1_TEMPLATE.format(
        display=display,
        headlines_json=json.dumps(headlines, ensure_ascii=False, indent=2),
    )
    try:
        try:
            raw1 = _call_azure(system, pass1_prompt)
            logger.debug("Triage pass 1: used Azure GPT-5.2")
        except Exception as az_err:
            logger.debug(f"Azure pass 1 unavailable ({az_err}), using Haiku fallback")
            raw1 = _call_claude(system, pass1_prompt)
        pass1_results = _parse_json_array(raw1)
    except Exception as e:
        logger.error(f"Triage pass 1 failed: {e}")
        return [
            {"headline": h, "score": 5, "tag": "other", "reason": "triage unavailable"}
            for h in headlines
        ]

    # Build lookup by headline for fast merge
    pass1_map: dict[str, dict] = {r.get("headline", ""): r for r in pass1_results}

    # ── Pass 2: review borderline (4-6) scores (Azure GPT-5.2 → Haiku fallback)
    borderline = [r for r in pass1_results if 4 <= (r.get("score") or 0) <= 6]

    if borderline:
        pass2_prompt = _PASS2_TEMPLATE.format(
            display=display,
            borderline_json=json.dumps(
                [{"headline": r["headline"], "score": r["score"], "reason": r.get("reason", "")}
                 for r in borderline],
                ensure_ascii=False,
                indent=2,
            ),
        )
        try:
            # Prefer Azure GPT-5.2 (free for user); fall back to Claude Haiku
            try:
                raw2 = _call_azure(system, pass2_prompt)
                logger.debug(f"Triage pass 2: used Azure GPT-5.2 for {len(borderline)} borderline scores")
            except Exception as az_err:
                logger.debug(f"Azure pass 2 unavailable ({az_err}), using Haiku fallback")
                raw2 = _call_claude(system, pass2_prompt)
            pass2_results = _parse_json_array(raw2)
            # Overwrite pass1 scores for reviewed headlines
            for r in pass2_results:
                h = r.get("headline", "")
                if h in pass1_map:
                    pass1_map[h] = r
            logger.debug(
                f"Triage pass 2 reviewed {len(borderline)} borderline; "
                f"updated {len(pass2_results)} scores"
            )
        except Exception as e:
            logger.warning(f"Triage pass 2 failed (using pass 1 scores): {e}")

    # Reassemble in original order
    results = []
    for h in headlines:
        if h in pass1_map:
            results.append(pass1_map[h])
        else:
            # headline not found in LLM response — safe fallback
            results.append({"headline": h, "score": 5, "tag": "other", "reason": "not scored"})

    return results
