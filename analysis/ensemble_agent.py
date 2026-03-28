"""Multi-Agent Debate / Ensemble — market uncertainty scorer.

Three Persona Agents run concurrently:
  Bull             — argues aggressively for a Long, finding all supporting data.
  Bear             — argues aggressively for a Short, finding all supporting data.
  Devil's Advocate — argues that THIS SPECIFIC trade setup is a trap and will fail.

A Meta-Judge (CIO) then evaluates all three arguments against the full context,
scores each case, calculates an uncertainty score, and issues a final verdict.

Key design decisions:
  - Personas do NOT see the original signal's conclusion to avoid anchoring.
    The Devil's Advocate is an exception — it receives the proposed entry/SL/TP
    specifically to critique the setup.
  - Persona calls use asyncio.gather for concurrency (3 → latency of 1 call).
  - Persona model is cheap/fast (Haiku by default); judge uses a stronger model.
  - run_ensemble_debate() mutates the signal dict in-place, adding:
      signal["_debate"]            — full debate result dict
      signal["_uncertainty_score"] — int 1-100
    and optionally overriding:
      signal["signal"]             — if judge disagrees or uncertainty > threshold
      signal["confidence"]         — from judge's confidence_override
      signal["sl"] / signal["tp"]  — from judge's suggested adjustments

Model toggle (edit config.py):
  DEBATE_PERSONA_MODEL  — model for the 3 persona agents
  DEBATE_JUDGE_MODEL    — model for the meta-judge
"""
from __future__ import annotations

import asyncio
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


def _safe_fmt(template: str, **kwargs) -> str:
    """
    Safe string substitution for prompts that contain LLM-generated content.

    Python's str.format() interprets {anything} in the *values* as format
    placeholders, raising KeyError when a persona argument contains price ranges
    like {1.0820-1.0850} or JSON notation. This helper does plain sequential
    replacement instead, so curly braces in values are never interpreted.

    After replacing named placeholders, {{ and }} are unescaped to { and }
    so that JSON schema examples in the prompt templates render correctly.
    """
    for key, val in kwargs.items():
        template = template.replace("{" + key + "}", str(val))
    # Unescape doubled braces — these are literal { } in format()-style templates
    template = template.replace("{{", "{").replace("}}", "}")
    return template


def _repair_json(s: str) -> str:
    """
    Best-effort repair of common LLM JSON formatting mistakes:
      - Trailing commas before } or ]
      - JS-style // line comments
      - Unescaped literal newlines inside string values
    """
    # Remove // comments (only outside quoted strings — approximation)
    s = re.sub(r'//[^\n"]*\n', '\n', s)
    # Remove trailing commas before } or ]
    s = re.sub(r',(\s*[}\]])', r'\1', s)
    # Replace literal newlines inside JSON strings with \n escape
    # Match anything between " ... " that contains a raw newline
    def _escape_newlines(m: re.Match) -> str:
        return m.group(0).replace('\n', '\\n').replace('\r', '')
    s = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', _escape_newlines, s, flags=re.DOTALL)
    return s


def _parse_judge_json(raw: str) -> dict:
    """
    Parse the judge's JSON response robustly.
    Strips markdown fences, tries direct parse, then repair, then regex extraction.
    Logs the raw response on failure to aid debugging.
    """
    clean = re.sub(r"^```[a-z]*\n?", "", raw.strip())
    clean = re.sub(r"\n?```$", "", clean).strip()

    # Pass 1: direct parse
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass

    # Pass 2: repair common issues then parse
    try:
        return json.loads(_repair_json(clean))
    except json.JSONDecodeError:
        pass

    # Pass 3: extract outermost {...} block (strips preamble/postamble)
    m = re.search(r"\{.*\}", clean, re.DOTALL)
    if m:
        extracted = m.group(0)
        try:
            return json.loads(extracted)
        except json.JSONDecodeError:
            try:
                return json.loads(_repair_json(extracted))
            except json.JSONDecodeError:
                pass

    # All attempts failed — log the raw response so we can diagnose
    logger.error(f"_parse_judge_json: all parse attempts failed. Raw response (first 800 chars):\n{raw[:800]}")
    raise json.JSONDecodeError("Could not parse judge response", raw, 0)


# ── Persona prompt templates ──────────────────────────────────────────────────

_BULL_SYSTEM = (
    "You are a highly aggressive Bullish forex analyst at a macro hedge fund. "
    "Your entire career is built on finding long opportunities. "
    "You are relentlessly optimistic about the base currency."
)

_BULL_PROMPT = """\
Make the strongest possible BULL CASE for going Long {display} right now.

Do NOT hedge. Do NOT mention risks to the upside view. Your job is to find \
EVERY piece of evidence supporting a rally:

- Central bank policy divergence that strengthens the base currency
- Technical structure: support zones, bullish patterns, unfilled liquidity above,
  break-and-retest levels, higher-lows structure
- News and macro catalysts creating buying pressure
- Correlated asset moves confirming bullish bias (DXY, yields, risk sentiment)
- Rate cycle dynamics: who is hiking or pausing vs who is cutting aggressively?
- Positioning and sentiment: is the market under-positioned for a rally?

Be specific with price levels and data from the context below.
State your proposed entry, SL, and TP on the final line.

=== MARKET CONTEXT ===
{context}

Output your BULL CASE (250-400 words), then on the final line:
SETUP: Entry X.XXXXX | SL X.XXXXX | TP X.XXXXX | Conviction: High/Medium/Low
"""

_BEAR_SYSTEM = (
    "You are a highly aggressive Bearish forex analyst at a macro hedge fund. "
    "Your entire career is built on finding short opportunities. "
    "You are relentlessly pessimistic about the base currency."
)

_BEAR_PROMPT = """\
Make the strongest possible BEAR CASE for going Short {display} right now.

Do NOT hedge. Do NOT mention risks to the downside view. Your job is to find \
EVERY piece of evidence supporting a decline:

- Central bank policy divergence that weakens the base currency
- Technical structure: resistance zones, bearish patterns, unfilled liquidity below,
  break-and-retest levels, lower-highs structure
- News and macro catalysts creating selling pressure
- Correlated asset moves confirming bearish bias (DXY, yields, risk-off moves)
- Rate cycle dynamics: who is cutting aggressively vs who is hiking or pausing?
- Positioning and sentiment: is the market over-positioned for a rally (crowded long)?

Be specific with price levels and data from the context below.
State your proposed entry, SL, and TP on the final line.

=== MARKET CONTEXT ===
{context}

Output your BEAR CASE (250-400 words), then on the final line:
SETUP: Entry X.XXXXX | SL X.XXXXX | TP X.XXXXX | Conviction: High/Medium/Low
"""

_DEVIL_SYSTEM = (
    "You are the Devil's Advocate at a top hedge fund — a ruthless risk officer "
    "whose specialty is identifying why proposed trades will fail. "
    "You have saved the fund from dozens of bad trades by destroying weak theses."
)

_DEVIL_PROMPT = """\
A trade has been proposed on {display}. Your job is to DESTROY this thesis.

Argue specifically why this market setup is a TRAP and this trade will FAIL:

- What stop hunts or liquidity sweeps are likely near the proposed entry/SL?
- What conflicting signals are being ignored or cherry-picked?
- What event risk, news trap, or scheduled release could invalidate this immediately?
- Which technical level is likely to be a fake-out rather than a real breakout?
- Is the market in a regime where this type of trade historically fails
  (low-volatility chop, news-driven reversal zones, end-of-session traps)?
- Is the proposed SL too tight — will it get hunted before the move plays out?
- Is the proposed TP too ambitious — where is the realistic take-profit given
  current liquidity and structure?
- What would need to be true for BOTH the Bull and Bear cases to be wrong?

Be specific with price levels and data from the context below.

=== MARKET CONTEXT ===
{context}

=== PROPOSED TRADE ===
Direction : {direction}
Entry     : {entry}
Stop Loss : {sl}
Take Profit: {tp}

Output your DEVIL'S CASE (250-400 words).
Conclude with: Failure Probability: High/Medium/Low — and a one-sentence verdict.
"""


# ── Meta-Judge prompt ─────────────────────────────────────────────────────────

_JUDGE_SYSTEM = (
    "You are the Chief Investment Officer of a global macro hedge fund. "
    "You have 25 years of experience trading G10 forex. "
    "You are known for brutal honesty — you have vetoed many trades that looked "
    "good on paper, and you have overridden hesitant analysts when the data was clear.\n\n"
    "OUTPUT RULES (strictly enforced):\n"
    "- Return ONLY the JSON object. No preamble, no explanation, no markdown fences.\n"
    "- Every string value must be on a single line — never include raw newline characters "
    "inside a JSON string value. Use a space or semicolon to separate sentences instead.\n"
    "- Use only standard double quotes for strings. No single quotes, no smart quotes.\n"
    "- No trailing commas after the last item in an object or array.\n"
    "- No JavaScript-style comments (// or /* */)."
)

_JUDGE_PROMPT = """\
Three analysts have presented their cases for {display}. Evaluate them ruthlessly.

=== BULL ANALYST ===
{bull_argument}

=== BEAR ANALYST ===
{bear_argument}

=== DEVIL'S ADVOCATE ===
{devil_argument}

=== ORIGINAL SIGNAL (from primary analysis) ===
Direction  : {direction}
Confidence : {confidence}
Entry      : {entry}
Stop Loss  : {sl}
Take Profit: {tp}
Rationale  : {rationale}

=== FULL MARKET CONTEXT ===
{context}

Scoring guide:
  80-100 : Airtight — specific, data-backed, accounts for counter-arguments
  60-79  : Solid — good logic but some gaps or ignored data
  40-59  : Weak — plausible but relies on hope rather than evidence
  1-39   : Poor — contradicted by the data, vague, or logically flawed

Uncertainty guide:
  High uncertainty (70-100) : Bull and Bear both score 65+, OR Neutral/Devil scores highest.
                               The market is genuinely ambiguous — do not force a trade.
  Medium uncertainty (40-69): One case is stronger but the counter-case has merit.
                               Trade with reduced confidence.
  Low uncertainty (1-39)    : One case clearly dominates. High conviction is warranted.

Return ONLY valid JSON, no other text:
{{
  "evaluations": {{
    "bull_score": <int 1-100>,
    "bear_score": <int 1-100>,
    "neutral_score": <int 1-100>,
    "winning_case": "Bull | Bear | Neutral",
    "flaws": "<one paragraph: key logical gaps or ignored data across all three arguments>"
  }},
  "uncertainty_score": <int 1-100>,
  "uncertainty_rationale": "<explain the score — e.g. 'Bull scores 78 and Bear scores 71, genuine disagreement; Neutral highlights a strong stop-hunt risk near the proposed SL'>",
  "final_decision": "Long | Short | Wait",
  "confidence_override": "High | Medium | Low | null",
  "trade_setup": {{
    "entry_assessment": "<validate or critique the proposed entry level with specific reasoning>",
    "sl_assessment": "<validate or critique the proposed SL — is it wide enough? Is it in a stop-hunt zone?>",
    "tp_assessment": "<validate or critique the proposed TP — is it realistic given current structure?>",
    "suggested_sl_adjustment": <float or null>,
    "suggested_tp_adjustment": <float or null>
  }}
}}
"""


# ── Async persona calls ───────────────────────────────────────────────────────

async def _call_async(
    client: anthropic.AsyncAnthropic,
    system: str,
    prompt: str,
    label: str,
) -> str:
    """Single async LLM call for one persona. Returns raw text."""
    try:
        msg = await client.messages.create(
            model=config.DEBATE_PERSONA_MODEL,
            max_tokens=700,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        logger.warning(f"Persona [{label}] failed: {e}")
        return f"[{label} unavailable: {e}]"


async def _gather_personas_async(
    personas: list[tuple[str, str, str]],   # list of (system, prompt, label)
) -> list[str]:
    """Run N persona calls concurrently. Returns responses in the same order."""
    client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return list(await asyncio.gather(*[
        _call_async(client, system, prompt, label)
        for system, prompt, label in personas
    ]))


async def _gather_signal_personas(
    context: str,
    direction: str,
    entry: str,
    sl: str,
    tp: str,
    display: str,
) -> tuple[str, str, str]:
    """Build signal-debate persona prompts and run them concurrently."""
    bull_prompt  = _safe_fmt(_BULL_PROMPT,  display=display, context=context)
    bear_prompt  = _safe_fmt(_BEAR_PROMPT,  display=display, context=context)
    devil_prompt = _safe_fmt(
        _DEVIL_PROMPT,
        display=display, context=context,
        direction=direction, entry=entry, sl=sl, tp=tp,
    )
    results = await _gather_personas_async([
        (_BULL_SYSTEM,   bull_prompt,  "Bull"),
        (_BEAR_SYSTEM,   bear_prompt,  "Bear"),
        (_DEVIL_SYSTEM,  devil_prompt, "Devil"),
    ])
    return results[0], results[1], results[2]


# ── Meta-Judge call ───────────────────────────────────────────────────────────

def _call_judge(
    context: str,
    bull: str,
    bear: str,
    devil: str,
    signal: dict[str, Any],
    display: str,
) -> dict | None:
    """Call the Meta-Judge with all arguments. Returns parsed JSON or None."""
    try:
        direction  = signal.get("signal", "Unknown")
        confidence = signal.get("confidence", "Unknown")
        entry      = signal.get("_order_preview", {}).get("entry_price") or signal.get("trade_setup", {}).get("entry_price", "N/A")
        sl         = signal.get("sl") or signal.get("_order_preview", {}).get("sl", "N/A")
        tp         = signal.get("tp") or signal.get("_order_preview", {}).get("tp", "N/A")
        rationale  = signal.get("rationale", "")

        prompt = _safe_fmt(
            _JUDGE_PROMPT,
            display=display,
            bull_argument=bull,
            bear_argument=bear,
            devil_argument=devil,
            direction=direction,
            confidence=confidence,
            entry=entry,
            sl=sl,
            tp=tp,
            rationale=rationale,
            context=context[:4000],
        )

        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        msg = client.messages.create(
            model=config.DEBATE_JUDGE_MODEL,
            max_tokens=1024,
            system=_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        result = _parse_judge_json(msg.content[0].text)
        if not isinstance(result, dict):
            raise ValueError("Expected JSON object")
        return result
    except Exception as e:
        logger.error(f"Meta-Judge call failed: {e}")
        return None


# ── Position debate prompts ───────────────────────────────────────────────────

_POS_BULL_SYSTEM = (
    "You are a seasoned trade defender at a macro hedge fund. "
    "Your job is to make the strongest case for holding — or adding to — an open position. "
    "You never let a winning trade die on sentiment, and you never exit early out of fear."
)

_POS_BULL_PROMPT = """\
An open {direction} position on {display} is under review. Make the strongest case for HOLDING (or adding).

Argue that the original thesis is still intact and the position should be kept open:
- Is the macro and central bank backdrop still aligned with the trade direction?
- Does the technical structure still support the move continuing?
- Is the current price action within normal noise, or is it a genuine signal to exit?
- Are correlated assets still confirming the bias?
- Is the SL well-placed — not in danger of being hit by routine volatility?
- What catalyst could still push price to TP from here?
- If the position is in profit: argue why banking the whole move is better than trimming early.
- If the position is in drawdown: argue why the thesis hasn't broken and the SL still makes sense.

Be specific with price levels and data from the context.

=== CURRENT POSITION ===
{position_block}

=== MARKET CONTEXT ===
{context}

Output your HOLD CASE (250-400 words).
Conclude: Hold conviction: High/Medium/Low — and a one-sentence verdict.
"""

_POS_BEAR_SYSTEM = (
    "You are a ruthless position liquidator at a macro hedge fund. "
    "Your job is to find every reason to close this trade NOW. "
    "You have seen traders give back entire gains by holding too long. You never make that mistake."
)

_POS_BEAR_PROMPT = """\
An open {direction} position on {display} is under review. Make the strongest case for EXITING NOW.

Argue that the position should be closed immediately:
- Has the macro or central bank backdrop shifted against this trade?
- Has the technical structure broken down — lower-highs/lower-lows against the position, key level lost?
- Is price action signalling a reversal rather than a pullback?
- Are correlated assets diverging from what the position requires?
- Is the risk/reward now unattractive from current price to TP vs distance to SL?
- If in profit: argue that taking the gain now is more prudent than waiting for a full TP.
- If in drawdown: argue that the thesis has failed and a deeper loss is likely.
- What upcoming event could accelerate a move against this position?

Be specific with price levels and data from the context.

=== CURRENT POSITION ===
{position_block}

=== MARKET CONTEXT ===
{context}

Output your EXIT CASE (250-400 words).
Conclude: Exit conviction: High/Medium/Low — and a one-sentence verdict.
"""

_POS_DEVIL_SYSTEM = (
    "You are the Devil's Advocate and risk officer at a top hedge fund. "
    "You specialise in identifying when traders are making emotional decisions — "
    "either holding losers from hope, or cutting winners from fear. "
    "Your job is to find the cognitive bias in this position management decision."
)

_POS_DEVIL_PROMPT = """\
An open {direction} position on {display} is being reviewed. Destroy the case for the current stance.

Identify specifically why the trader is making a MISTAKE — whether that means holding when they should exit,
or being tempted to exit when they should hold:

- Is the trader falling for sunk-cost fallacy — holding a loser because they don't want to realise the loss?
- Is the trader cutting a winner too early because of fear, when the structure is still intact?
- Is the SL currently in a stop-hunt zone that will trigger before the real move plays out?
- Has the time horizon of the original trade already passed — is this position now just gambling?
- Is there an upcoming news event or session transition that creates asymmetric risk?
- Is the current P&L (profit or loss) creating emotional distortion in the management decision?
- What does the market structure around the CURRENT price say that neither the Bull nor Bear has addressed?

Be specific with price levels, time, and data from the context.

=== CURRENT POSITION ===
{position_block}

=== MARKET CONTEXT ===
{context}

Output your DEVIL'S CASE (250-400 words).
Conclude: Bias identified — [sunk-cost / fear-of-giving-back / other] — and a one-sentence verdict.
"""

_POS_JUDGE_PROMPT = """\
Three analysts have reviewed an open {direction} position on {display}. \
Evaluate their arguments and issue your verdict.

=== HOLD ANALYST ===
{bull_argument}

=== EXIT ANALYST ===
{bear_argument}

=== DEVIL'S ADVOCATE ===
{devil_argument}

=== CURRENT POSITION ===
{position_block}

=== FULL MARKET CONTEXT ===
{context}

Scoring guide (same as entry debate):
  80-100 : Airtight — specific, data-backed
  60-79  : Solid — good logic, some gaps
  40-59  : Weak — plausible but not evidence-based
  1-39   : Poor — contradicted by data or vague

Uncertainty guide:
  High (70-100) : Hold and Exit both score 65+ — genuinely unclear, consider Trim.
  Medium (40-69): One case stronger but counter has merit — reduced conviction.
  Low (1-39)    : One case clearly dominates — act with confidence.

Return ONLY valid JSON, no other text:
{{
  "evaluations": {{
    "bull_score": <int 1-100>,
    "bear_score": <int 1-100>,
    "neutral_score": <int 1-100>,
    "winning_case": "Hold | Exit | Neutral",
    "flaws": "<key gaps or emotional biases identified across all three arguments>"
  }},
  "uncertainty_score": <int 1-100>,
  "uncertainty_rationale": "<explain the score>",
  "final_decision": "Hold | Trim | Exit",
  "confidence_override": "High | Medium | Low | null",
  "position_assessment": {{
    "sl_assessment": "<is the current SL well-placed? too tight? in a stop-hunt zone?>",
    "tp_assessment": "<is the TP still realistic from current price?>",
    "suggested_sl_adjustment": <float or null>,
    "suggested_tp_adjustment": <float or null>,
    "trim_recommendation": "<if Trim: explain what % to close and why>",
    "trim_percentage": <int 25/50/75 or null>
  }}
}}
"""


def _build_position_block(pos: dict[str, Any], symbol: str) -> str:
    """Format position data into a readable block for persona prompts."""
    pip      = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["pip"]
    decimals = config.PAIRS.get(symbol, config.PAIRS[config.MT5_SYMBOL])["price_decimals"]
    direction = pos.get("type", "buy").upper()
    open_p    = pos.get("open_price", 0)
    curr_p    = pos.get("current_price", 0)
    sl        = pos.get("sl") or "None"
    tp        = pos.get("tp") or "None"
    profit    = pos.get("profit", 0)
    volume    = pos.get("volume", 0)
    open_time = pos.get("open_time", "Unknown")

    # Pips P&L
    if direction == "BUY":
        pips_pnl = round((curr_p - open_p) / pip, 1)
    else:
        pips_pnl = round((open_p - curr_p) / pip, 1)

    # Distance to SL / TP
    sl_pips = tp_pips = "N/A"
    if isinstance(sl, float) and sl > 0:
        sl_pips = round(abs(curr_p - sl) / pip, 1)
    if isinstance(tp, float) and tp > 0:
        tp_pips = round(abs(tp - curr_p) / pip, 1)

    # How long open
    try:
        from datetime import datetime, timezone
        opened = datetime.fromisoformat(open_time.replace("Z", "+00:00"))
        hours_open = round((datetime.now(timezone.utc) - opened).total_seconds() / 3600, 1)
    except Exception:
        hours_open = "Unknown"

    return (
        f"  Ticket    : #{pos.get('ticket')}\n"
        f"  Direction : {direction}\n"
        f"  Volume    : {volume} lots\n"
        f"  Opened    : {open_time} ({hours_open}h ago)\n"
        f"  Open price: {round(open_p, decimals)}\n"
        f"  Now       : {round(curr_p, decimals)}\n"
        f"  P&L       : {pips_pnl:+.1f} pips  (${profit:.2f})\n"
        f"  Stop Loss : {sl}  ({sl_pips} pips away)\n"
        f"  Take Profit: {tp}  ({tp_pips} pips to go)\n"
    )


async def _gather_position_personas(
    context: str,
    position_block: str,
    direction: str,
    display: str,
) -> tuple[str, str, str]:
    """Build position-debate persona prompts and run them concurrently."""
    kwargs = dict(
        direction=direction, display=display,
        position_block=position_block, context=context,
    )
    bull_prompt  = _safe_fmt(_POS_BULL_PROMPT,  **kwargs)
    bear_prompt  = _safe_fmt(_POS_BEAR_PROMPT,  **kwargs)
    devil_prompt = _safe_fmt(_POS_DEVIL_PROMPT, **kwargs)
    results = await _gather_personas_async([
        (_POS_BULL_SYSTEM,   bull_prompt,  "Hold"),
        (_POS_BEAR_SYSTEM,   bear_prompt,  "Exit"),
        (_POS_DEVIL_SYSTEM,  devil_prompt, "Devil"),
    ])
    return results[0], results[1], results[2]


def _call_position_judge(
    context: str,
    hold_arg: str,
    exit_arg: str,
    devil_arg: str,
    position_block: str,
    direction: str,
    display: str,
) -> dict | None:
    """Call the Meta-Judge for a position review. Returns parsed JSON or None."""
    try:
        prompt = _safe_fmt(
            _POS_JUDGE_PROMPT,
            display=display,
            direction=direction,
            bull_argument=hold_arg,
            bear_argument=exit_arg,
            devil_argument=devil_arg,
            position_block=position_block,
            context=context[:4000],
        )
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        msg = client.messages.create(
            model=config.DEBATE_JUDGE_MODEL,
            max_tokens=1024,
            system=_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        result = _parse_judge_json(msg.content[0].text)
        if not isinstance(result, dict):
            raise ValueError("Expected JSON object")
        return result
    except Exception as e:
        logger.error(f"Position judge call failed: {e}")
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def run_ensemble_debate(
    context: str,
    signal: dict[str, Any],
    symbol: str | None = None,
) -> dict | None:
    """
    Run the full Bull / Bear / Devil's Advocate -> Meta-Judge debate pipeline.

    Mutates signal in-place:
      signal["_debate"]            — full debate result dict
      signal["_uncertainty_score"] — int 1-100

    May also override:
      signal["signal"]             — if judge's final_decision differs from original
                                     OR uncertainty_score > config.DEBATE_MAX_UNCERTAINTY
      signal["confidence"]         — if judge provides confidence_override
      signal["sl"]                 — if judge suggests sl adjustment
      signal["tp"]                 — if judge suggests tp adjustment

    Returns the debate_result dict, or None if the pipeline fails entirely.
    """
    action = signal.get("signal")
    if action not in ("Long", "Short"):
        return None

    sym     = symbol or signal.get("_symbol", config.MT5_SYMBOL)
    display = config.PAIRS.get(sym, config.PAIRS[config.MT5_SYMBOL])["display"]

    direction = action
    preview   = signal.get("_order_preview") or {}
    entry     = str(preview.get("entry_price") or signal.get("trade_setup", {}).get("entry_price", "N/A"))
    sl        = str(preview.get("sl") or signal.get("sl", "N/A"))
    tp        = str(preview.get("tp") or signal.get("tp", "N/A"))

    logger.info(f"[{sym}] Ensemble debate starting — {direction} | persona={config.DEBATE_PERSONA_MODEL} judge={config.DEBATE_JUDGE_MODEL}")

    # 1. Generate personas concurrently
    try:
        bull, bear, devil = asyncio.run(
            _gather_signal_personas(
                context=context[:5000],   # cap to keep persona prompts reasonable
                direction=direction,
                entry=entry,
                sl=sl,
                tp=tp,
                display=display,
            )
        )
    except Exception as e:
        logger.error(f"[{sym}] Persona generation failed: {e}")
        return None

    logger.info(f"[{sym}] Personas complete — running Meta-Judge")

    # 2. Run Meta-Judge
    debate_result = _call_judge(
        context=context,
        bull=bull,
        bear=bear,
        devil=devil,
        signal=signal,
        display=display,
    )
    if not debate_result:
        logger.warning(f"[{sym}] Meta-Judge failed — skipping debate enrichment")
        return None

    # Attach full arguments for traceability
    debate_result["_personas"] = {
        "bull":  bull,
        "bear":  bear,
        "devil": devil,
    }

    uncertainty = debate_result.get("uncertainty_score", 50)
    final_dec   = debate_result.get("final_decision", action)

    # 3. Mutate signal
    signal["_debate"]            = debate_result
    signal["_uncertainty_score"] = uncertainty

    # Confidence override from judge
    conf_override = debate_result.get("confidence_override")
    if conf_override and conf_override != "null":
        signal["confidence"] = conf_override
        logger.info(f"[{sym}] Confidence overridden by judge: {conf_override}")

    # SL/TP adjustments from judge
    setup = debate_result.get("trade_setup") or {}
    if setup.get("suggested_sl_adjustment"):
        signal["sl"] = setup["suggested_sl_adjustment"]
        logger.info(f"[{sym}] SL adjusted by judge: {signal['sl']}")
    if setup.get("suggested_tp_adjustment"):
        signal["tp"] = setup["suggested_tp_adjustment"]
        logger.info(f"[{sym}] TP adjusted by judge: {signal['tp']}")

    # High uncertainty → downgrade to Wait
    if uncertainty > config.DEBATE_MAX_UNCERTAINTY:
        logger.info(
            f"[{sym}] Uncertainty {uncertainty} > threshold {config.DEBATE_MAX_UNCERTAINTY} "
            f"— downgrading {action} to Wait"
        )
        signal["signal"]      = "Wait"
        signal["_debate_veto"] = (
            f"Ensemble debate vetoed: uncertainty {uncertainty}/100 exceeds threshold "
            f"{config.DEBATE_MAX_UNCERTAINTY}. {debate_result.get('uncertainty_rationale', '')}"
        )
    elif final_dec != action:
        # Judge disagrees with original signal direction
        logger.info(
            f"[{sym}] Judge overrides signal: {action} -> {final_dec} "
            f"(uncertainty={uncertainty})"
        )
        signal["signal"]        = final_dec
        signal["_debate_veto"]  = (
            f"Judge overridden from {action} to {final_dec}. "
            f"{debate_result.get('uncertainty_rationale', '')}"
        )

    evals = debate_result.get("evaluations", {})
    logger.info(
        f"[{sym}] Debate complete — Bull:{evals.get('bull_score')} "
        f"Bear:{evals.get('bear_score')} Neutral:{evals.get('neutral_score')} "
        f"Uncertainty:{uncertainty} -> {signal['signal']}"
    )
    return debate_result


def run_position_debate(
    context: str,
    position: dict[str, Any],
    symbol: str | None = None,
) -> dict | None:
    """
    Run the Hold / Exit / Devil's Advocate -> Meta-Judge debate on an open position.

    position: dict from mt5.position_reader.get_open_positions()
      keys: ticket, symbol, type, volume, open_price, current_price, sl, tp, profit, open_time

    Returns the debate_result dict with keys:
      evaluations.bull_score      — Hold case score
      evaluations.bear_score      — Exit case score
      evaluations.neutral_score   — Devil's Advocate score
      uncertainty_score           — 1-100
      uncertainty_rationale
      final_decision              — "Hold" | "Trim" | "Exit"
      confidence_override
      position_assessment
        sl_assessment, tp_assessment
        suggested_sl_adjustment, suggested_tp_adjustment
        trim_recommendation, trim_percentage

    Returns None on failure.
    """
    sym     = symbol or position.get("symbol", config.MT5_SYMBOL)
    display = config.PAIRS.get(sym, config.PAIRS[config.MT5_SYMBOL])["display"]
    direction = position.get("type", "buy").upper()

    position_block = _build_position_block(position, sym)

    logger.info(
        f"[{sym}] Position debate starting — #{position.get('ticket')} {direction} "
        f"| persona={config.DEBATE_PERSONA_MODEL} judge={config.DEBATE_JUDGE_MODEL}"
    )

    # 1. Generate personas concurrently
    try:
        hold_arg, exit_arg, devil_arg = asyncio.run(
            _gather_position_personas(
                context=context[:5000],
                position_block=position_block,
                direction=direction,
                display=display,
            )
        )
    except Exception as e:
        logger.error(f"[{sym}] Position persona generation failed: {e}")
        return None

    logger.info(f"[{sym}] Position personas complete — running Meta-Judge")

    # 2. Run Meta-Judge
    debate_result = _call_position_judge(
        context=context,
        hold_arg=hold_arg,
        exit_arg=exit_arg,
        devil_arg=devil_arg,
        position_block=position_block,
        direction=direction,
        display=display,
    )
    if not debate_result:
        logger.warning(f"[{sym}] Position Meta-Judge failed")
        return None

    # Attach full arguments for traceability
    debate_result["_personas"] = {
        "hold":  hold_arg,
        "exit":  exit_arg,
        "devil": devil_arg,
    }
    debate_result["_position"] = position

    evals       = debate_result.get("evaluations", {})
    uncertainty = debate_result.get("uncertainty_score", 50)
    final_dec   = debate_result.get("final_decision", "Hold")

    logger.info(
        f"[{sym}] Position debate complete — "
        f"Hold:{evals.get('bull_score')} Exit:{evals.get('bear_score')} "
        f"Neutral:{evals.get('neutral_score')} "
        f"Uncertainty:{uncertainty} -> {final_dec}"
    )
    return debate_result
