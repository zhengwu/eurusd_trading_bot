# Technical Report: Forex Multi-Pair AI Trading Agent

**Version:** April 2026 (updated 2026-04-21)  
**Pairs:** EUR/USD · GBP/USD · USD/JPY  
**Platform:** Windows / MetaTrader 5 / Python 3.11

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture](#2-architecture)
3. [Pipeline 1 — News Acquisition](#3-pipeline-1--news-acquisition)
4. [Pipeline 2 — News Triage (Two-Pass Scoring)](#4-pipeline-2--news-triage-two-pass-scoring)
5. [Pipeline 3 — Context Assembly](#5-pipeline-3--context-assembly)
6. [Pipeline 4 — Full Analysis & Signal Generation](#6-pipeline-4--full-analysis--signal-generation)
7. [Pipeline 5 — Multi-Agent Ensemble Debate](#7-pipeline-5--multi-agent-ensemble-debate)
8. [Pipeline 6 — Position Sizing & Order Parameters](#8-pipeline-6--position-sizing--order-parameters)
9. [Pipeline 7 — Position Monitor](#9-pipeline-7--position-monitor)
10. [Pipeline 8 — Pre-Event Analysis](#10-pipeline-8--pre-event-analysis)
11. [Pipeline 9 — Trade Execution](#11-pipeline-9--trade-execution)
12. [Supporting Systems](#12-supporting-systems)
13. [LLM Model Assignments](#13-llm-model-assignments)
14. [Configuration Reference](#14-configuration-reference)
15. [Design Decisions](#15-design-decisions)

---

## 1. System Overview

The system is an automated multi-pair forex trading assistant that monitors financial news and market conditions 24/5 (during forex market hours), generates structured trading signals using a chain of AI models, and executes approved trades on MetaTrader 5. Human approval is required before any trade executes.

### Core principles

- **Human in the loop.** The system never trades without explicit approval via Slack or CLI.
- **Multi-pair independence.** EURUSD, GBPUSD, and USDJPY each have separate news queries, deduplication caches, cooldown timers, and analysis contexts. A trigger on one pair does not affect the others.
- **Defense-in-depth signal quality.** A single LLM call does not determine a trade. Signals pass through: triage → full analysis → ensemble debate → human approval.
- **Latency reduction.** News is sourced from six providers ordered by publication latency. A fast-poll thread reduces reaction time from 30 minutes to ~5 minutes for breaking news.

---

## 2. Architecture

### Process layout

The entire system runs from a single command (`python -m agents.job1_opportunity`). All components run as threads within the same process:

```
agents/job1_opportunity.py  (main process)
│
├── Main thread:        Tier 1 scanner loop (30-min interval)
├── Thread: fast-news-watcher   (2-min RSS poll)
├── Thread: job2-position       (30-min position monitor)
├── Thread: phase-watcher       (60-sec phase transition detector)
├── Thread: daily-collector     (22:00 UTC end-of-day)
├── Thread: outcome-checker     (30-min debate calibration price fetch, MT5)
└── Thread: slack-bot           (Socket Mode, event-driven)
```

### Data flow — from news to trade

```
News APIs / RSS
      │
      ▼
[news_fetcher.py]  ←── FMP, Finnhub, RSS, NewsAPI, AlphaVantage, EODHD, Yahoo
      │
      ▼
[DedupCache]  ←── per-pair file cache: data/.dedup_cache_{SYMBOL}
      │  (filters already-seen articles)
      ▼
[triage_prompt.py]  ←── Pass 1: Azure GPT-5.2 scores 1-10
      │              ←── Pass 2: Azure GPT-5.2 reviews borderline 4-6
      ▼
[intraday_logger.py]  ←── appends ALL scored articles to data/YYYY-MM-DD/intraday.jsonl
      │
      │  score >= TRIAGE_SCORE_THRESHOLD (6) AND cooldown clear?
      ▼
[context_builder.py]  ←── assembles: regime + today's news + events + price summary + 7-day history
      │
      ▼
[full_analysis_prompt.py]  ←── Claude Sonnet (extended thinking, 10K budget tokens)
      │  returns structured signal: Long / Short / Wait
      ▼
[ensemble_agent.py]  ←── 3 Azure GPT-5.2 personas (concurrent, Haiku fallback) + Sonnet judge
      │  may veto, downgrade, or adjust SL/TP
      ▼
[signal_store.py]  ←── saved as pending in data/pending_signals.json
      │
      ▼
[notifier.py]  ←── Slack + email alert with approve/reject buttons
      │
      │  User types: approve <ID>
      ▼
[job3_executor.py]  ←── computes lot size, calls MT5 order_manager
      │
      ▼
[MetaTrader 5]  ←── BUY / SELL market or limit order
```

### File structure

```
eurusd_agent/
├── config.py                     All tunable parameters
├── agents/
│   ├── job1_opportunity.py       Entry point — starts all threads
│   ├── job2_position.py          Position monitor (Hold/Trim/Exit)
│   ├── job3_executor.py          Trade execution + CLI
│   ├── job4_chat.py              Conversational assistant (tool calling)
│   └── slack_bot.py              Command dispatcher + NLP routing
├── triage/
│   ├── scanner.py                30-min scan orchestrator
│   ├── triage_prompt.py          Two-pass Azure GPT-5.2 + Haiku scoring
│   ├── intraday_logger.py        Appends scored articles to disk
│   └── cooldown.py               Per-pair cooldown file locks
├── analysis/
│   ├── context_builder.py        Assembles LLM context window
│   ├── full_analysis_prompt.py   Claude Sonnet signal generation
│   ├── signal_formatter.py       Output normalisation
│   ├── ensemble_agent.py         Bull/Bear/Risk Assessor + Judge debate
│   ├── outcome_tracker.py        Debate calibration: records signals, fetches MT5 prices at 8/16/24/48h
│   └── trade_journal.py          Signal lifecycle recorder
├── pipeline/
│   ├── fast_news_watcher.py      2-min RSS poll loop (background thread)
│   ├── news_fetcher.py           All news provider fetchers
│   ├── price_agent.py            Live price + TA indicators
│   ├── regime_agent.py           Market regime engine
│   ├── pre_event_agent.py        Pre/post-event scenario analysis
│   ├── cb_policy_updater.py      CB statement fetcher + stance extractor
│   ├── event_fetcher.py          ForexFactory economic calendar
│   └── dedup_cache.py            Per-pair URL deduplication
├── mt5/
│   ├── connector.py              MT5 connect/disconnect
│   ├── position_reader.py        Open positions, account, ticks, bars
│   ├── order_manager.py          Open, close, modify orders
│   ├── risk_manager.py           Lot sizing and SL/TP calculation
│   └── portfolio_risk.py         Portfolio-level risk aggregation and entry gate
└── data/                         Auto-created runtime data
    ├── pending_signals.json
    ├── rate_cycles.json
    ├── trade_journal.jsonl / .csv
    ├── ticket_map.json            ticket → signal_id reverse lookup for Job 2
    ├── outcome_log.json           Debate calibration: pre/post-debate + 8/16/24/48h price outcomes
    ├── .dedup_cache_{SYMBOL}      Per-pair article dedup caches
    ├── .cooldown_{SYMBOL}         Main scanner cooldowns
    ├── .cooldown_fast_{SYMBOL}    Fast watcher cooldowns
    └── YYYY-MM-DD/
        ├── intraday.jsonl         All scored headlines for the day
        ├── events.json            Economic calendar with actuals
        ├── prices.json            End-of-day closing prices
        ├── news.jsonl             Merged daily archive
        └── summary.md             LLM-generated end-of-day summary
```

---

## 3. Pipeline 1 — News Acquisition

**File:** `pipeline/news_fetcher.py`  
**Triggered by:** Main scanner (every 30 min) and fast news watcher (every 2 min)

### Providers and latency

| Provider | Function | Latency | Requires Key |
|----------|----------|---------|--------------|
| ForexLive RSS | `fetch_rss()` | ~2–5 min | No |
| FXStreet RSS | `fetch_rss()` | ~2–5 min | No |
| FMP | `fetch_fmp()` | ~5 min | Yes (paid) |
| Finnhub | `fetch_finnhub()` | ~5 min | Yes (free tier) |
| NewsAPI | `fetch_newsapi()` | 1–3 hours | Yes (free tier) |
| Alpha Vantage | `fetch_alphavantage()` | 1–6 hours | Yes (free tier) |
| EODHD | `fetch_eodhd()` | 1–6 hours | Yes (paid) |
| Yahoo Finance | `fetch_yahoo_finance()` | 1–6 hours | No (yfinance) |

Providers are called in latency order. The first provider to return an article wins deduplication — so RSS articles (2–5 min old) are always preferred over NewsAPI articles (hours old) for the same story.

### Staleness filter

After combining all provider results, any article with a `published_dt` older than `NEWS_MAX_AGE_HOURS` (default: 8 hours) is dropped. This prevents stale delayed articles from triggering analysis on old news.

```python
# config.py
NEWS_MAX_AGE_HOURS = 8
```

### Per-pair news queries

Each pair has its own search query configuration in `_PAIR_NEWS`:

- **NewsAPI:** pair-specific keyword query (e.g. `'"EUR/USD" OR "EURUSD" OR (euro AND dollar)'`) combined with CB and macro terms
- **Alpha Vantage:** regex keyword filter on returned titles
- **EODHD:** pair-specific ticker (`EURUSD.FOREX`, `GBPUSD.FOREX`, etc.)
- **RSS:** pair-relevant keyword filter with a macro override list

### RSS keyword filtering

RSS feeds cover all forex news. The `fetch_rss()` function filters by pair-relevant keywords to avoid flooding the log with irrelevant cross-pair technical analysis:

```python
_RSS_PAIR_KEYWORDS = {
    "EURUSD": [
        r"\bEUR\b", r"\beuro\b", r"\bECB\b", r"\bFed\b",
        r"\bEurozone\b", r"\bGermany\b",
        r"\bUSD\b", r"\bdollar\b", r"\bCPI\b", r"\bNFP\b", r"\btariff",
        r"\bgeopolit", r"\brisk.off\b", r"\brisk.on\b",
    ],
    ...
}
```

### Finnhub `minId` cursor

Finnhub supports incremental fetching via a `minId` parameter. The module stores the highest article ID seen in `_finnhub_last_id` so each poll only retrieves new articles:

```python
_finnhub_last_id: dict[str, int] = {}
# After each fetch:
_finnhub_last_id[symbol] = max_id_seen
```

### FMP pagination

FMP returns forex news paginated by `page` index. The fetcher increments the page until it has `max_items` or receives an empty page:

```python
while len(out) < max_items:
    params = {"page": page, "limit": 50, "apikey": key}
    # ... fetch and append ...
    page += 1
```

### Output normalisation

All providers return the same dict structure for downstream processing:

```python
{
    "source": "RSS:ForexLive",       # provider:publication
    "title": "...",                   # headline text
    "url": "https://...",             # canonical URL (used for dedup)
    "published_dt": datetime(...),   # UTC-aware datetime or None
    "snippet": "...",                # article summary (up to 600 chars)
}
```

### Deduplication

Two layers:

1. **Within-batch dedup** (in `fetch_news()`): URL-based dedup across all providers in the same fetch call. First occurrence wins.
2. **Cross-scan dedup** (`DedupCache`): per-pair file-based cache (`data/.dedup_cache_{SYMBOL}`). Articles seen in previous scans are excluded from triage. Cache is cleared at 22:00 UTC daily by the end-of-day collector.

---

## 4. Pipeline 2 — News Triage (Two-Pass Scoring)

**File:** `triage/triage_prompt.py`  
**Triggered by:** Scanner (`scanner.py`) and fast news watcher (`fast_news_watcher.py`)

### Design rationale

The original single-pass Claude Haiku triage clustered too many scores around 5, resulting in under-escalation of market-moving news (scores just below the threshold of 6) and over-escalation of irrelevant articles. The two-pass system with Azure GPT-5.2 reduces score clustering and improves discrimination at the threshold boundary.

### Pass 1 — Score all headlines (Azure GPT-5.2)

All new headlines are scored in a single batch call. The prompt includes calibrated examples for each score range to anchor the model's judgement:

**System prompt:**
```
You are a professional forex market analyst specialising in {display}.
```

**User prompt (Pass 1):**
```
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
["...", "...", ...]

Return ONLY valid JSON — no prose, no markdown fences:
[
  {"headline": "...", "score": 7, "tag": "macro_data", "cb_bank": null, "reason": "..."}
]
```

### Pass 2 — Review borderline scores (Azure GPT-5.2)

Any headline scoring 4, 5, or 6 in Pass 1 is sent to Pass 2 for a second opinion. The model is instructed to push scores decisively up or down, reducing the pile-up at the boundary.

**User prompt (Pass 2):**
```
You are reviewing borderline triage scores (4-6) for {display} headlines.
These scores are uncertain — your job is to push each one decisively up or down.

For each headline below, decide:
  - Score UP to 7+ if: the headline contains a specific number, implies a policy shift,
    describes an unexpected event, or could cause a 15+ pip move.
  - Keep at 5-6 if: genuinely ambiguous, unclear impact, or borderline relevance.
  - Score DOWN to 1-3 if: purely technical analysis, opinion, market recap,
    or clearly no directional impact on {display}.

Borderline headlines to re-evaluate:
[{"headline": "...", "score": 5, "reason": "..."}, ...]

Return ONLY valid JSON — no prose, no markdown fences:
[
  {"headline": "...", "score": 7, "tag": "macro_data", "cb_bank": null, "reason": "..."}
]
```

### Fallback chain

```
Pass 1: Azure GPT-5.2
  → on failure: Claude Haiku (config.TRIAGE_MODEL)

Pass 2: Azure GPT-5.2
  → on failure: Claude Haiku (config.TRIAGE_MODEL)
```

"Failure" includes both API exceptions and empty/null response content. Azure GPT-5.2 can return a 200 OK with `choices[0].message.content = None` (e.g. content filter refusal). `_call_azure()` now explicitly checks for empty content and raises `RuntimeError(f"Azure returned empty content (finish_reason={...})")` so the Haiku fallback is correctly triggered rather than passing an empty string to `json.loads()`.

### Output

Every article receives a triage record regardless of score. All records are appended to `data/YYYY-MM-DD/intraday.jsonl`:

```json
{
  "date": "2026-04-08",
  "time": "09:15",
  "symbol": "GBPUSD",
  "source": "RSS:FXStreet",
  "headline": "UK CPI beats forecast at 3.2%",
  "url": "https://...",
  "sentiment": null,
  "tags": ["macro_data"],
  "triage_score": 8,
  "triage_tag": "macro_data",
  "triage_reason": "CPI beat of 0.3pp above forecast...",
  "triggered_full_analysis": false
}
```

### Escalation decision

After triage, the scanner checks:

```python
high_score_items = [i for i in merged if (i.get("score") or 0) >= config.TRIAGE_SCORE_THRESHOLD]
# TRIAGE_SCORE_THRESHOLD = 6

if not high_score_items:
    return []   # log only, no escalation

if is_cooling_down(symbol):
    return []   # logged, suppressed

set_cooldown(symbol)
trigger = high_score_items[0]   # highest-score article becomes the trigger
```

### Cooldown mechanism

Cooldowns are stored as timestamp files: `data/.cooldown_{SYMBOL}`. After any escalation, the file is written with the current UTC timestamp. Subsequent checks compare `now` against `lock_time + COOLDOWN_MINUTES`. The fast watcher uses separate files: `data/.cooldown_fast_{SYMBOL}` with a shorter `FAST_COOLDOWN_MINUTES` (default 20 vs 45 for the main scanner).

---

## 5. Pipeline 3 — Context Assembly

**File:** `analysis/context_builder.py`  
**Called by:** Scanner (on escalation) and all direct `analyze` commands

The context window is the complete information package sent to the analysis LLM. It is assembled in fixed sections in the following order:

### Section 1 — BREAKING EVENT (conditional)

Only present when triggered by a specific headline (as opposed to a manual `analyze` command):

```
=== BREAKING EVENT [09:15 UTC] ===
Trigger: "UK CPI beats forecast at 3.2%, BOE rate cut bets fade" [score: 8, tag: macro_data]
Recent corroborating headlines (last 2 hrs): GBP/USD rallies 40 pips..., BOE hold expected...
```

If the trigger came from the pre-event agent, the pre-event context (expected scenarios) is also injected here.

### Section 2 — CURRENT MARKET REGIME

Four-dimensional regime assessment computed in real time by `pipeline/regime_agent.py`:

- **Risk Sentiment:** VIX level + pair realised volatility from M15 bars
- **Technical Trend:** D1 SMA20/50/200 stack alignment + M15 momentum ratio
- **Volatility:** ATR-14 vs 30-day ATR baseline
- **Macro Bias:** live 10Y yield spreads (ECB, BOE, Japan MOF) + CB rate cycle stances

### Section 3 — TODAY

Today's scored intraday headlines (last 30, filtered by pair), economic events with actuals vs forecasts, and current price action.

### Section 4 — UPCOMING EVENTS (next 48h)

ForexFactory high-impact events for the next two days with forecast and previous values.

### Section 5 — PAST 7 DAYS

LLM-generated daily `summary.md` files for the last 7 trading days. These are generated at 22:00 UTC each day by the end-of-day collector using Claude Sonnet.

### Section 6 — PRICE SUMMARY (live + technicals)

Generated by `pipeline/price_agent.py`. Includes:

- Live tick data (bid/ask/spread) from MT5
- M15 OHLC bars (last 8 bars = 2 hours)
- Daily OHLC + SMA20/50/200 + ATR-14
- H4 bars with EMA20/50, RSI-14, ATR-14
- D1 MACD (12/26/9)
- D1 Bollinger Bands (20, 2σ) with band position %
- Multi-timeframe confluence summary: explicit D1/H4/M15 trend directions + ALIGNED/LEANING/CONFLICTED verdict
- 30-day daily price trend table
- Correlated assets: DXY, US10Y, Gold, VIX (+ pair-specific: FTSE for GBPUSD, Nikkei for USDJPY)
- Live 10Y yield spreads: DE10Y–US10Y (EURUSD), UK10Y–US10Y (GBPUSD), JP10Y–US10Y (USDJPY)

`price_agent.py` also exposes `get_indicators(symbol)` — a lightweight function that returns raw indicator values (ATR-D1, RSI-D1, MACD line/signal/hist, ATR-M15, RSI-M15, M15 momentum bias) for use by Job 2's phase engine. This avoids re-running the full text summary and returns only the numeric values needed for trail distance and exhaustion checks.

### Token budget enforcement

The context builder estimates token count (1 token ≈ 4 chars). If the assembled context exceeds `CONTEXT_MAX_TOKENS`, the price summary section is trimmed to fit:

```python
if _estimate_tokens(full) > config.CONTEXT_MAX_TOKENS:
    budget_chars = config.CONTEXT_MAX_TOKENS * 4
    used = sum(len(s) for s in sections[:-1])
    remaining = max(budget_chars - used, 500)
    sections[-1] = price_summary[:remaining] + "\n...[price summary truncated]\n"
```

---

## 6. Pipeline 4 — Full Analysis & Signal Generation

**File:** `analysis/full_analysis_prompt.py`  
**Model:** Claude Sonnet (`claude-sonnet-4-6`) with extended thinking enabled

### Design rationale

Extended thinking (10,000 budget tokens) was enabled because:
1. The context window is ~5,000–6,000 tokens of structured data
2. Signal quality requires sequential reasoning: macro → regime → MTF structure → momentum → entry precision → SL placement
3. The model frequently needs to reconcile conflicting signals across timeframes and data sources

### System prompt

```
You are a professional {display} forex analyst with deep expertise in
macroeconomics, central bank policy, and technical analysis.
```

### Analysis prompt

```
You are a professional {display} forex analyst and institutional trader.
You have been given a rich context including: recent news, economic events, market regime data,
and pre-computed technical indicators across three timeframes (D1, H4, M15).

Your goal is to identify high-probability trading setups using a top-down multi-timeframe approach.

ANALYTICAL PROCESS — follow this order:
1. MACRO CONTEXT: What is the dominant macro/news narrative driving the pair right now?
2. MULTI-TIMEFRAME STRUCTURE: Read the "Multi-timeframe confluence" section in the price summary.
   This is your primary structural anchor. A BEARISH/BULLISH ALIGNED verdict significantly raises
   signal confidence. A CONFLICTED verdict should result in Wait unless the macro catalyst is overwhelming.
3. MOMENTUM CHECK: Use MACD (D1) — is momentum confirming the MTF direction?
   Use H4 RSI — overbought (>70) warns against longs, oversold (<30) warns against shorts.
4. MEAN-REVERSION vs TREND-CONTINUATION: Use Bollinger Bands (D1).
   Price above 80% of band range = extended, mean-reversion risk. Price below 20% = potential bounce.
   Combine with ADX: ADX > 25 favours trend continuation; ADX < 20 favours range / mean-reversion.
5. ENTRY PRECISION: Use H4 EMA 20/50 and M15 structure for entry zone.
6. STOP LOSS RULE: Never place SL at an obvious level. Pad by at least 1×ATR-14 (D1) beyond
   the nearest key level. Wider SL in high-volatility regimes.
7. RISK/REWARD CHECK: Scan 2-3 structural TP levels and pick the one that gives the best
   executable R:R. Always aim to produce a tradeable Long or Short. If the best available R:R
   is below 1:{rr_warning}, still output the signal but flag it in risk_note with a warning
   such as "⚠ R:R X.X below {rr_warning} threshold — size down or monitor closely".

{context_window}

Provide your analysis in the following JSON format. Use chain-of-thought reasoning:
complete the analytical fields first to build your thesis, THEN output the signal and trade setup.

{
  "today_summary": "1-2 sentences: what drove price action today",
  "regime_alignment": "How trade direction and SL width align with Risk Sentiment, Technical Trend, Volatility",
  "mtf_confluence": "D1/H4/M15 trend directions and ALIGNED/LEANING/CONFLICTED verdict",
  "technical_rationale": "Full TA picture: MTF structure, MACD, Bollinger, H4 EMA, key levels",
  "signal": "Long | Short | Wait",
  "wait_reason": "ONLY populate if signal=Wait. Primary reason: 'mtf_conflicted', 'macro_unclear', or 'risk_event_pending'. Include a 1-sentence explanation. Empty string for Long/Short.",
  "confidence": "High | Medium | Low",
  "time_horizon": "Intraday | 1-3 days | This week",
  "trade_setup": {
    "entry_price": 0.0000,
    "stop_loss": 0.0000,
    "take_profit": 0.0000,
    "risk_reward_ratio": "e.g., 1:2.5",
    "stop_loss_reasoning": "Why this SL avoids sweeps — reference ATR-14, nearest key level, volatility regime"
  },
  "key_levels": {
    "support": 0.0000,
    "resistance": 0.0000
  },
  "price_snapshot": {
    "current": 0.0000,
    "session_open": 0.0000,
    "session_high": 0.0000,
    "session_low": 0.0000,
    "session_change_pct": "+0.00%",
    "trend": "e.g., Bearish — BEARISH ALIGNED across D1/H4/M15, MACD bearish, below EMA20 H4"
  },
  "invalidation": "What macro or technical development would invalidate this signal",
  "risk_note": "Any asymmetric risks or upcoming events that could disrupt the setup"
}

Return ONLY valid JSON, no other text.
```

**`wait_reason` field:** When the signal is `Wait`, the model must identify the primary reason using one of three categories: `mtf_conflicted` (D1/H4/M15 alignment verdict is CONFLICTED), `macro_unclear` (no clear directional catalyst), or `risk_event_pending` (high-impact event imminent). R:R no longer blocks signal generation — low R:R only triggers a warning in `risk_note`. This field is passed through `signal_formatter.py`, shown in Slack notifications for Wait signals, and logged by the scanner.

### Continuation signal classification

When an open position already exists on the pair being analysed, `triage/scanner.py` calls `analysis/context_builder.get_open_position_theories(symbol)` before running full analysis. This function:
- Reads all open MT5 positions for the symbol
- Cross-references each ticket against `data/trade_journal.jsonl` to retrieve the original rationale and invalidation condition
- Builds an `=== OPEN POSITIONS ===` context block showing entry price, SL/TP, unrealised P&L, and the original thesis for each position

The full analysis LLM receives this block and must classify each new signal:

| Classification | Conditions |
|---|---|
| `continuation` | Same direction AND macro/technical thesis is materially the same as an existing position |
| `new_theory` | Different direction OR a distinct catalyst (e.g. existing thesis: ECB hold; new signal: US CPI surprise) |

When the scanner receives `theory_classification = "continuation"`:
- Sets `signal["_is_continuation"] = True` and `signal["_related_ticket"]`
- **Auto-execution is suppressed** — continuation signals always require manual approval regardless of uncertainty score
- Slack notification shows a `:link:` badge and "Theory: Continuation of open position (ticket #X), manual approval required"

This prevents the agent from inadvertently doubling a position on news that merely restates the original catalyst, while still surfacing the signal for the trader to evaluate.

### Signal output

The signal formatter (`analysis/signal_formatter.py`) normalises the raw JSON into a standardised dict with fields: `signal`, `confidence`, `time_horizon`, `today_summary`, `key_levels`, `price_snapshot`, `invalidation`, `risk_note`, `wait_reason`, `theory_classification`, `related_ticket`, etc.

Only `Long` and `Short` signals proceed to the ensemble debate and order preview. `Wait` signals are notified to Slack (with the `wait_reason` shown if populated) and logged by the scanner.

---

## 7. Pipeline 5 — Multi-Agent Ensemble Debate

**File:** `analysis/ensemble_agent.py`  
**Triggered by:** Any `Long` or `Short` signal from full analysis  
**Models:** Azure GPT-5.2 personas (concurrent, Claude Haiku fallback) + Claude Sonnet (judge)

### Architecture

```
Full analysis signal (Long/Short)
      │
      ├──────────────────────────────────┐
      │  [concurrent via asyncio.gather] │
      ▼                                  ▼                          ▼
[Bull Azure]                    [Bear Azure]              [Risk Assessor Azure]
Evidence-based long case        Evidence-based short case  Top 2-3 risks rated H/M/L
      │                                  │                          │
      └──────────────────────────────────┴──────────────────────────┘
                                         │
                                         ▼
                               [Meta-Judge Sonnet]
                          Scores each case by evidence gap,
                          applies Sonnet-agreement bonus,
                          computes uncertainty score,
                          optionally adjusts SL/TP
                                         │
                               uncertainty > 75?
                               ┌─────────┴─────────┐
                              Yes                   No
                               │                   │
                          Downgrade             Keep signal
                          to Wait              (may adjust
                                               SL/TP/confidence)
```

### Key design decisions

**Evidence-based personas (not adversarial):** Early versions instructed Bull and Bear to make the "strongest possible case regardless of data." This caused both personas to always score high (~70+), which by the judge's own "both ≥65 = high uncertainty" rule produced near-universal high uncertainty. Personas were rewritten to evaluate the evidence fairly — when context clearly favours one direction, the opposing persona produces a weak argument (score 40-50), creating a large gap that the judge reads as low uncertainty.

**Persona anchoring:** Bull and Bear do not receive the original signal's direction. This prevents anchoring — each analyst starts from first principles. The Risk Assessor is the exception: it receives the proposed entry, SL, and TP to critique the specific setup mechanics.

**Risk Assessor (not Devil's Advocate):** The original Devil's Advocate was instructed to "DESTROY this thesis" unconditionally. This added a permanent "vote no" voice, biasing the judge toward high uncertainty for every signal. The Risk Assessor instead rates each identified risk as High/Medium/Low — if the risks are genuinely low, it says so.

### Bull persona

**System:**
```
You are a senior bullish forex analyst at a macro hedge fund.
Your job is to evaluate whether the data genuinely supports a long position —
not to argue for one regardless of the evidence.
```

**Prompt:**
```
Assess the BULL CASE for going Long {display} based strictly on the evidence in the context below.

Your score should reflect the actual strength of the data, not a pre-formed view:
- If the macro, technical, and news data clearly support a rally, make a strong, specific case.
- If the evidence is mixed, say so honestly — identify what supports a long AND where it is weak.
- Do not invent bullish arguments that are not grounded in the context.

Evaluate each angle with the data actually present:
- Central bank policy: is divergence genuinely favoring the base currency right now?
- Technical structure: are support zones, patterns, and momentum actually bullish?
- News and macro catalysts: do recent headlines create real buying pressure?
- Correlated assets (DXY, yields, VIX, equities): are they confirming or contradicting?
- Positioning: is there evidence of under-positioning or short-covering potential?

=== MARKET CONTEXT ===
{context}

Output your assessment (200-350 words), then on the final line:
SETUP: Entry X.XXXXX | SL X.XXXXX | TP X.XXXXX | Conviction: High/Medium/Low
```

### Bear persona

**System:**
```
You are a senior bearish forex analyst at a macro hedge fund.
Your job is to evaluate whether the data genuinely supports a short position —
not to argue for one regardless of the evidence.
```

**Prompt (parallel structure to Bull):**
```
Assess the BEAR CASE for going Short {display} based strictly on the evidence in the context below.

[same evidence-based framing as Bull]

- Central bank policy: is divergence genuinely weakening the base currency right now?
- Technical structure: are resistance zones, patterns, and momentum actually bearish?
- News and macro catalysts: do recent headlines create real selling pressure?
- Correlated assets: are they confirming or contradicting?
- Positioning: is there evidence of over-positioning or long-unwinding potential?

Output your assessment (200-350 words), then on the final line:
SETUP: Entry X.XXXXX | SL X.XXXXX | TP X.XXXXX | Conviction: High/Medium/Low
```

### Risk Assessor persona

**System:**
```
You are the Risk Assessor at a top hedge fund.
Your job is not to block trades — it is to identify the specific risks that could
derail this setup and give an honest probability for each.
If the risks are genuinely low, say so. If they are high, flag them clearly.
```

**Prompt:**
```
A trade has been proposed on {display}. Assess the top risks to this setup RIGHT NOW.

Identify the 2-3 most concrete, specific risks that could cause this trade to fail:
- Stop hunt / liquidity sweep: is the proposed SL sitting in an obvious hunt zone?
- Event risk: is there a scheduled release or speech in the next 4-8 hours that could reverse the move?
- Technical invalidation: which specific level, if broken, would negate the thesis entirely?
- Regime mismatch: is the current volatility / session / market structure unfavorable for this trade type?
- Setup quality: is the entry chasing, or is there a clean level with a well-defined invalidation?

For each risk, rate its current probability: High / Medium / Low — and explain why
based on the actual data in the context. Do not invent risks that are not present in the data.

=== PROPOSED TRADE ===
Direction : {direction}
Entry     : {entry}
Stop Loss : {sl}
Take Profit: {tp}

Output your risk assessment (200-350 words).
Conclude with: Overall failure probability: High / Medium / Low — and a one-sentence summary.
```

### Meta-Judge

**System:**
```
You are the Chief Investment Officer of a global macro hedge fund.
You have 25 years of experience trading G10 forex.
You are known for brutal honesty — you have vetoed many trades that looked
good on paper, and you have overridden hesitant analysts when the data was clear.

OUTPUT RULES (strictly enforced):
- Return ONLY the JSON object. No preamble, no explanation, no markdown fences.
- Every string value must be on a single line.
- Use only standard double quotes. No trailing commas. No JS comments.
```

**Prompt:**
```
Three analysts have presented their cases for {display}. Evaluate them ruthlessly.

=== BULL ANALYST ===
{bull_argument}

=== BEAR ANALYST ===
{bear_argument}

=== RISK ASSESSOR ===
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
  80-100 : Airtight — specific, data-backed, evidence clearly dominates
  60-79  : Solid — good logic but some gaps or conflicting signals
  40-59  : Weak — plausible but mixed evidence, relies on assumption
  1-39   : Poor — contradicted by the data, vague, or unsupported

Uncertainty guide — base your score on the GAP between the winning and losing case:
  Low uncertainty (1-39)    : Winning case scores 70+ AND losing case scores below 50.
                               One direction clearly dominates. High conviction.
  Medium uncertainty (40-69): Winning case is stronger, but losing case scores 50-69.
                               Genuine mixed signals — trade with reduced confidence.
  High uncertainty (70-100) : Both Bull and Bear score 75+, OR the gap between them
                               is under 10 points. Data is genuinely ambiguous.

Primary analysis adjustment: The original signal was produced by a primary model using
extended thinking (high-quality baseline). If its direction agrees with the winning case
from the debate, reduce your uncertainty score by 10 points.

Return ONLY valid JSON:
{
  "evaluations": {
    "bull_score": <int 1-100>,
    "bear_score": <int 1-100>,
    "winning_case": "Bull | Bear",
    "top_risk": "<one sentence: highest-priority risk from the Risk Assessor>",
    "risk_level": "High | Medium | Low"
  },
  "uncertainty_score": <int 1-100>,
  "uncertainty_rationale": "<one sentence: gap, Sonnet agreement, why this score>",
  "final_decision": "Long | Short | Wait",
  "confidence_override": "High | Medium | Low | null",
  "trade_setup": {
    "entry_assessment": "<validate or critique entry level>",
    "sl_assessment": "<validate or critique SL — wide enough? stop-hunt zone?>",
    "tp_assessment": "<validate or critique TP — realistic given structure?>",
    "suggested_sl_adjustment": <float or null>,
    "suggested_tp_adjustment": <float or null>
  }
}
```

**`neutral_score` / `winning_case: Neutral` were removed.** The original schema included a `neutral_score` field and allowed `winning_case: "Neutral"`. In practice `neutral_score` consistently won every debate (61-71), causing the judge to return `final_decision: Wait` on every signal regardless of the uncertainty score. This bypassed `DEBATE_MAX_UNCERTAINTY` entirely. The fix: uncertainty score now drives Wait/proceed decisions exclusively; `winning_case` is binary Bull or Bear; the Risk Assessor's output is surfaced as `risk_level` + `top_risk` (a single sentence), which is more actionable than a paragraph.

**`final_decision` enforcement rules** (in prompt and code):
- Uncertainty ≤ 70: return `Long` or `Short` (stronger of Bull/Bear). Never `Wait`.
- Uncertainty 71–75: `Wait` only if both Bull and Bear score below 55. Otherwise return direction.
- Uncertainty > 75: `Wait` used freely.
- **Code guard** (`ensemble_agent.py`): if judge returns `Wait` but `uncertainty ≤ DEBATE_MAX_UNCERTAINTY`, the original Sonnet signal is preserved. The threshold is the single gate for Wait downgrades.

### Signal mutation

The debate mutates the signal dict in-place:

```python
signal["_debate"]            = debate_result        # full debate JSON
signal["_uncertainty_score"] = uncertainty_score    # int 1-100

# Optional mutations:
signal["confidence"]  = judge_confidence_override
signal["sl"]          = suggested_sl_adjustment
signal["tp"]          = suggested_tp_adjustment

# Veto path:
if uncertainty > DEBATE_MAX_UNCERTAINTY:   # default: 75
    signal["signal"] = "Wait"
    signal["_debate_veto"] = "Uncertainty X/100 exceeds threshold..."

# Code guard — judge returned Wait but uncertainty is below threshold: preserve original signal
elif final_dec == "Wait" and uncertainty <= DEBATE_MAX_UNCERTAINTY:
    logger.info(f"Judge returned Wait but uncertainty {uncertainty} <= threshold — keeping {action}")
    # signal["signal"] unchanged
```

### Concurrency

The three persona calls run concurrently via `asyncio.gather` using `openai.AsyncAzureOpenAI` (with `anthropic.AsyncAnthropic` as a per-call fallback). Both clients are instantiated once per debate round and shared across all three concurrent calls. Total latency ≈ one persona call + one judge call — typically 20–40 seconds.

---

## 8. Pipeline 6 — Position Sizing & Order Parameters

**Files:** `mt5/risk_manager.py`, `agents/job3_executor.py`  
**Triggered by:** Any `Long` or `Short` signal that survives the ensemble debate

At this point in the pipeline, the signal has a confirmed direction, LLM-proposed SL/TP (potentially adjusted by the debate judge), and an `uncertainty_score`. This section describes how those raw values are turned into a fully-specified order: final SL/TP prices, lot size, and the order preview shown in Slack.

### SL and TP derivation

SL and TP prices are derived in `calculate_sl_tp()` (`mt5/risk_manager.py`) from the signal's `key_levels` dict:

| Direction | SL source | TP source |
|-----------|-----------|-----------|
| Long (buy) | `key_levels.support` | `key_levels.resistance` |
| Short (sell) | `key_levels.resistance` | `key_levels.support` |

If the debate judge suggested adjusted SL or TP values, those are written onto the signal before this step and take priority. If `key_levels` are missing entirely, fixed pip fallbacks apply: `JOB3_DEFAULT_SL_PIPS` (30) and `JOB3_DEFAULT_TP_PIPS` (60).

### Lot size — `recommend_position_size()`

Lot size is computed by `recommend_position_size()` in `mt5/risk_manager.py`. It runs after the debate so the `uncertainty_score` is available, and before the signal is saved to the pending store.

**Formula:**

```
remaining_pct = JOB3_MAX_PORTFOLIO_RISK_PCT − current_portfolio_risk_pct
allocated_pct = remaining_pct × uncertainty_multiplier
final_pct     = min(allocated_pct, JOB3_RISK_PCT)     # cap at per-trade base limit
lot_size      = (equity × final_pct) / (sl_pips × pip_value_per_lot)
lot_size      = clamp(lot_size, JOB3_MIN_LOT, JOB3_MAX_LOT)
```

The key design principle: lot size is allocated from the **remaining portfolio risk budget**, scaled by conviction. A high-conviction signal on an account that is already 2.5% deployed gets less capital than the same signal on a fresh account.

**Uncertainty multiplier tiers** (from `JOB3_UNCERTAINTY_TIERS`):

| Uncertainty score | Conviction | Multiplier | Example (1.5% remaining) |
|-------------------|------------|------------|--------------------------|
| ≤ 30 | High | 1.00× | 1.5% → capped at 2.0% base |
| 31–55 | Medium | 0.75× | 1.5% × 0.75 = 1.1% |
| 56–75 | Low (passed veto) | 0.50× | 1.5% × 0.50 = 0.75% |
| None (no debate) | — | fallback | flat `JOB3_RISK_PCT` (2.0%) |

**Pip values** are pair-aware:
- USD-quote pairs (EURUSD, GBPUSD): $10 per pip per standard lot
- JPY pairs (USDJPY): approximate JPY pip value from config

**Fallback behaviour:** If MT5 is disconnected when sizing runs (portfolio data unavailable), `execute_signal()` falls back to `calculate_lot_size(equity, JOB3_RISK_PCT, sl_pips)` — flat base risk, no uncertainty scaling. The portfolio risk gate still applies at execution time.

The result is attached to the signal as `_lot_override` (lot) and `_size_reason` (human-readable rationale). `execute_signal()` reads `_lot_override` directly; the `uncertainty_risk_pct()` scaling that previously ran at execution time has been removed — uncertainty is now fully captured here.

### Order preview

`compute_order_preview()` (`agents/job3_executor.py`) assembles the full order picture using live MT5 tick data (or key-level midpoint if MT5 is offline):

| Field | Source |
|-------|--------|
| `entry_price` | Live ask (buy) or bid (sell) |
| `sl` / `tp` | From `calculate_sl_tp()` |
| `sl_pips` / `tp_pips` | Distance from entry in pips |
| `lot_size` | `_lot_override` if set, else `calculate_lot_size(base_risk)` |
| `risk_amount` | `equity × _recommended_risk_pct` (or `JOB3_RISK_PCT`) |
| `risk_reward` | `tp_pips / sl_pips` |
| `rr_warning` | `True` when R:R < `config.JOB3_RR_WARNING` (default 1.5) |

The preview is stored on the signal as `_order_preview` and rendered in the Slack notification. The sizing rationale appears as an italic line beneath the lot/risk line:

```
Order Details (live)
>Entry 1.0851  SL 1.0821 (-30p)  TP 1.0951 (+100p)
>Lot 0.05  Risk $75  R:R 1:3.3
>Sizing: 1.2% of 3.0% portfolio risk in use (1.8% remaining). Medium conviction (uncertainty=48) → 75% of remaining = 1.4%, capped at 2.0% → 1.4% → 0.05 lots
```

### Auto-execution for high-conviction signals

After the signal is saved and the Slack notification is sent, the scanner checks `uncertainty_score` against `JOB3_AUTO_APPROVE_MAX_UNCERTAINTY` (default: 30). If the score is at or below this threshold — meaning the debate judged the signal as high conviction — the signal is **automatically approved and executed** in a background thread without waiting for a human button click.

| Uncertainty score | Action |
|---|---|
| ≤ 30 (`JOB3_AUTO_APPROVE_MAX_UNCERTAINTY`) | Auto-approved and executed; Slack notified with ticket and fill price |
| 31–75 | Sent to Slack for manual approval via Execute / Reject buttons |
| > 75 (`DEBATE_MAX_UNCERTAINTY`) | Vetoed by debate; downgraded to Wait; never saved |

Set `JOB3_AUTO_APPROVE_MAX_UNCERTAINTY = None` in `config.py` to disable auto-execution entirely and require manual approval for all signals.

---

## 9. Pipeline 7 — Position Monitor (Dynamic Position Management)

**File:** `agents/job2_position.py`  
**Models:** Azure GPT-5.2 (routine checks, Haiku fallback) + Claude Sonnet (priority analysis)  
**Interval:** Every `JOB2_CHECK_INTERVAL_MINUTES` (default: 30 min) + 60-second phase watcher

Job 2 applies a two-layer decision process to every open position — a hard-rules phase engine first, then a two-tier LLM prompt tailored to that phase.

### Two-layer architecture

```
Open position (MT5)
      │
      ▼
[Operational safeguards]
  ├── Spread check: current_spread ≤ JOB2_MAX_SPREAD_MULT × normal_spread_pips?
  ├── News blackout: high-impact event < JOB2_NEWS_BLACKOUT_MIN (15 min) away?
  └── Minimum lot: volume > 2 × JOB3_MIN_LOT before Trim is suggested?
      │
      ▼
[Phase Engine — hard rules, no LLM]
  Computes: R-multiple, time-in-trade, breakeven status, net P&L (profit + swap)
  Assigns one of 9 phases based on objective thresholds
      │
      ├── BREAKEVEN / TRAIL phase + safeguards OK?
      │       → Auto-execute SL modification (no approval)
      │       → Notify Slack
      │
      ▼
[Two-tier LLM Judgment]

  ┌─ Routine (every 30 min) ───────────────────────────────────┐
  │  Model: Azure GPT-5.2 (Haiku fallback)                     │
  │  Prompt: position + indicators + session prices +          │
  │          last 6 headlines + structured assessment approach  │
  │  Output: reasoning_summary + action + confidence           │
  │                                                             │
  │  action=Exit AND confidence=High?                           │
  │    yes → escalate to full Sonnet analysis                   │
  │    no  → Hold: compact one-liner Slack notification        │
  │          Exit/Trim: save pending, full Slack alert          │
  └─────────────────────────────────────────────────────────────┘

  ┌─ Priority (phase transitions, escalations) ────────────────┐
  │  Model: Claude Sonnet (ANALYSIS_MODEL)                      │
  │  Prompt: position + account + tick + indicators +           │
  │          original thesis + portfolio context +              │
  │          targeted news context (_build_job2_news_context):  │
  │            last 8 headlines + released events + 5 assets   │
  │  Non-loss phases: theory invalidation check (see below)    │
  │  Output: action + confidence + rationale + suggested SL/TP │
  │                                                             │
  │  theory_invalidated=true → force Exit regardless of phase  │
  │  Exit / Trim → save pending, full Slack alert              │
  │  Hold → full Slack alert with ⚡ priority badge            │
  └─────────────────────────────────────────────────────────────┘
```

### Phase engine

Nine phases, ordered from most adverse to most profitable:

| Phase | Trigger condition | Auto-action | LLM question |
|-------|------------------|-------------|--------------|
| **LOSS_CRITICAL** | `r < JOB2_LOSS_DEEP_R` (near SL) | None | Binary Exit/Hold — thesis broken or directional move? |
| **LOSS_DEEP** | `JOB2_LOSS_DEEP_R ≤ r < JOB2_LOSS_MODERATE_R` | None | Partial close to reduce dollar risk? Full exit if thesis broken |
| **LOSS_MODERATE** | `JOB2_LOSS_MODERATE_R ≤ r < JOB2_LOSS_MILD_R` | None | Is pullback noise or directional? Two deterioration signs → Exit |
| **LOSS_MILD** | `JOB2_LOSS_MILD_R ≤ r < 0` | None | Normal noise? Thesis still intact? |
| **EARLY** | `0 ≤ r < JOB2_BREAKEVEN_R` | None | Is thesis still valid? Any early exit signals? |
| **BREAKEVEN** | `r ≥ JOB2_BREAKEVEN_R` | Move SL to `entry + 3p buffer` (if safeguards pass) | Is the move stalling or continuing? |
| **PROFIT** | `r ≥ JOB2_PARTIAL_TP_R` | None | Trim 33/50/75% or hold for bigger target? |
| **TRAIL** | `r ≥ JOB2_TRAIL_R` | Trail SL at `1.5 × ATR-M15` behind price (never backwards) | Is momentum still strong enough to trail, or take full profit? |
| **OVERDUE** | `hours_open > 1.5 × expected_horizon` | None | Compelling reason to hold past horizon, or exit/tighten now? |
| **UNKNOWN** | No original SL data | None | Assess on current P&L and market conditions only |

R-multiple is computed as: `pips_in_profit / original_sl_pips`. `original_sl_pips` is recovered from the trade journal via the `ticket_map.json` reverse lookup. If not available, the current SL distance from entry is used as a fallback. The in-memory `_sl_cache` freezes this value at first sight so it remains stable even after breakeven SL or trail moves that would corrupt a current-SL fallback.

### Auto-execute (no approval required)

**Breakeven SL move** (Phase BREAKEVEN):
- Computes `new_sl = open_price + JOB2_BREAKEVEN_BUFFER_PIPS × pip` (long) or `open_price - buffer` (short)
- Calls `modify_sl_tp(ticket, sl=new_sl)` directly
- Posts a `🔒 Auto Breakeven` Slack notification
- Only fires if safeguards pass (spread OK, no news blackout)

**Trail SL update** (Phase TRAIL):
- Computes `trail_sl = current_price - (ATR_M15 × JOB2_TRAIL_ATR_MULT)` (long)
- Only advances SL — never moves it backwards toward entry
- Calls `modify_sl_tp(ticket, sl=trail_sl)` directly
- Posts a `🔒 Auto Trail SL` Slack notification

### Operational safeguards

| Safeguard | Check | Action on fail |
|-----------|-------|----------------|
| Spread check | `current_spread ≤ 2.0 × normal_spread_pips` | Skip all SL/TP modifications; LLM told not to recommend them |
| News blackout | No high-impact event within 15 min | Skip all SL/TP modifications; LLM told not to recommend them |
| Minimum lot | `volume > 2 × JOB3_MIN_LOT (0.02 lots)` | Trim recommendation downgraded to Hold in `run_position_check()` |

`normal_spread_pips` is stored per pair in `config.PAIRS` (EURUSD: 1.0p, GBPUSD: 1.5p, USDJPY: 1.0p, AUDUSD: 1.5p).

### Two-tier prompt design

#### Routine prompt (`_build_routine_prompt`)

Sent to Azure GPT-5.2 / Haiku. Estimated ~600 tokens. Sections:

1. **Position** — direction, volume, entry, current price, pips, R-multiple, net P&L, SL/TP, time in trade
2. **Technical Indicators** — ATR-M15, RSI-M15, MACD histogram, M15 momentum bias
3. **Session Prices** — symbol + DXY + US10Y with today's % change (compact single line)
4. **Recent News** — last 6 scored headlines for this pair today (`[time] [score] headline`)
5. **Original Trade Thesis** — rationale, invalidation condition, time horizon from trade journal
6. **Phase** — current phase + R + phase-specific question
7. **Assessment Approach** — three explicit questions the model must evaluate:
   - Indicator alignment: do RSI, MACD, M15 bias support or oppose the trade?
   - News impact: does any headline materially reverse the original catalyst?
   - Phase fit: is the R-multiple consistent with holding to target?

JSON output includes `reasoning_summary` (3 sentences covering all three assessments) alongside `action`, `confidence`, `rationale`, `suggested_trim_pct`. The `reasoning_summary` provides an audit trail of why the model reached its decision.

#### Priority prompt (`_build_prompt`)

Sent to Claude Sonnet. Sections:

1. **Position** — full enriched detail (same as routine)
2. **Account** — equity, balance, free margin, margin level, drawdown
3. **Market Snapshot** — live bid/ask/spread, current EST time
4. **Technical Indicators** — same as routine
5. **Original Trade Thesis** — full rationale, invalidation, risk note (up to 300 chars)
6. **Portfolio Context** — other open positions with USD-direction bucket and risk %
7. **Recent Market Context** (`_build_job2_news_context`):
   - Last 8 scored headlines for this pair today
   - Released economic events today with actual/forecast/surprise (ForexFactory)
   - Session prices for 5 assets: symbol + DXY + US10Y + Gold + VIX
   This replaces the former `build_context()[:2000]` truncation, which was designed
   for signal generation (7-day summaries, full price tables) — not position management.
8. **Phase Assessment** — phase-specific question block (see below)
9. **Theory Invalidation Check** (non-loss phases only — see dedicated section)

#### Phase-specific prompt design

Rather than a single generic prompt, Job 2 sends a phase-specific question block:

```
=== PHASE ASSESSMENT ===
Current phase: PROFIT
The trade has reached 1.67R — partial profit zone.
NOTE: Position can be trimmed (volume allows partial close).
Focus on: Should we take partial profit now (and what %)? Is momentum
continuing or showing exhaustion signals?

Allowed actions: "Hold", "Exit", or "Trim" (with suggested_trim_pct 25-75).
"SetTP" also allowed if you see a better target.
```

### Enriched position data (both tiers)

- **R-multiple and phase** — `current_r = 1.32R | phase: BREAKEVEN`
- **Net P&L** — `profit + swap` so carry costs are visible
- **Breakeven status** — whether SL is already at or above entry
- **Time in trade vs expected horizon** — `opened 6.2h ago | expected horizon: 8h | OVERDUE flag`
- **Live technical indicators** (from `pipeline/price_agent.get_indicators()`):
  - ATR-M15 — intraday noise level, used for trail distance
  - RSI-M15 and D1 — momentum exhaustion check
  - MACD histogram — momentum direction and strength
  - M15 momentum bias — bullish / bearish / neutral (last 8 bars vote)
- **Original signal context** (from `trade_journal.py` via `ticket_map.json`):
  - Original rationale, time horizon, invalidation condition, risk note
- **Portfolio exposure context** (from `mt5/portfolio_risk.py`):
  - Other open positions, their USD-direction bucket, and aggregate risk %

### Theory invalidation check

Fires at every non-loss phase (EARLY, BREAKEVEN, PROFIT, TRAIL, OVERDUE). Suppressed in loss phases because the loss-phase question blocks already explicitly ask about thesis validity — running both would double-prompt the model toward Exit on what might be normal noise.

```
=== THEORY INVALIDATION CHECK ===
Original invalidation condition: Close below 1.2680 (SMA20)
Original thesis summary: UK CPI surprise (3.2% vs 2.9%) reduces BOE cut probability...
Current position: BREAKEVEN at 1.03R

Thesis is INVALIDATED if ANY of these apply:
  1. Price has convincingly closed beyond the stated invalidation level
  2. A news/macro event has directly and definitively reversed the original catalyst
  3. The structural setup has been negated (cited support/resistance fully broken)

Thesis is NOT invalidated by:
  - Normal pullbacks within ATR range
  - Temporary adverse moves that haven't breached key levels

Set theory_invalidated=true only when you are confident the thesis is definitively broken.
If true, your action MUST be Exit — this overrides phase-based logic.
```

When `theory_invalidated=true` is returned and `action != "Exit"`, the code forces `action = "Exit"`, prepends `[THESIS INVALIDATED — overriding {original_action}]` to the rationale, and sets `_theory_override=True` on the record. The Slack notification uses a 💡🚨 emoji rather than the standard 🚨 to distinguish thesis-driven exits from drawdown exits.

For positions opened manually (no journal entry): the model defaults to `theory_invalidated=false` and can only override this if price action alone is catastrophically adverse (price blown through where a stop should be, or a major macro reversal underway in that direction).

The priority Sonnet LLM JSON output includes `theory_invalidated` and `theory_invalidation_reason` fields in all phases. The `reasoning_summary` in the routine prompt serves an analogous purpose for auditability.

```json
{
  "action": "Trim | Hold | Exit | SetSL | SetTP | SetSLTP",
  "confidence": "High | Medium | Low",
  "rationale": "2-3 sentences referencing specific position and market data",
  "risk_note": "Any immediate risks",
  "suggested_sl": null,
  "suggested_tp": null,
  "suggested_trim_pct": 50,
  "theory_invalidated": false,
  "theory_invalidation_reason": ""
}
```

### Signal routing and notification

| LLM action | Tier | Routing | Notification | Approval |
|------------|------|---------|--------------|----------|
| Hold | Routine | None | 🕐 compact one-liner (low noise) | None |
| Hold | Priority | None | ⚡ full Slack alert | None |
| Theory-invalidated Exit | Priority | Saved to pending | 💡🚨 full alert with `[THESIS INVALIDATED]` prefix | Required |
| Exit | Either | Saved to pending | 🚨 full Slack alert | Required |
| Trim | Either | Saved to pending | ✂️ full Slack alert | Required |
| SetSL / SetTP / SetSLTP | Priority | Saved to pending | 🔒/🎯 full Slack alert | Required |
| Breakeven SL (auto) | Phase engine | `modify_sl_tp()` direct | 🔒 Auto Breakeven | None |
| Trail SL (auto) | Phase engine | `modify_sl_tp()` direct | 🔒 Auto Trail SL | None |

Routine Hold notifications (`_notify_routine_hold`) are compact single-line messages:
```
🕐 Routine #12345678 BUY EURUSD | +$23.40 | 0.87R | EARLY | Hold [Medium] — RSI-M15 neutral at 52, no adverse headlines today, R within expected range for EARLY phase
```

---

## 10. Pipeline 8 — Pre-Event Analysis

**File:** `pipeline/pre_event_agent.py`  
**Models:** Claude Sonnet (pre-event brief), full analysis pipeline (post-event trigger)

### Pre-event brief (30–90 min before release)

When `run_pre_event_check()` detects a high-impact event between 30 and 90 minutes away, it generates a scenario brief:

**Prompt:**
```
A high-impact {currency} economic release is coming up in ~60 minutes:
**{event_name}** at {time} UTC.
Forecast: {forecast}  |  Previous: {previous}

The active trading pair is {display}.

Write a concise pre-event scenario brief (max 120 words) for a forex trader.
Cover two scenarios:
(1) if actual beats forecast — what does it mean for {currency} and {display}?
    What price direction?
(2) if actual misses forecast — same questions.
End with: 'Monitor price reaction at release for entry.'
Be specific: mention buy/sell direction, not abstract macro commentary.
Format as plain text (no markdown headers).
```

### Post-event trigger (after release, significant beat/miss)

When the scan cycle following a scheduled release detects that `actual` is now populated and the surprise exceeds the configured threshold, it bypasses the pair cooldown and triggers a full analysis with the event outcome injected into the context:

```python
PRE_EVENT_MIN_SURPRISE = {
    "NFP":    20_000,    # Nonfarm Payrolls
    "CPI":    0.1,       # inflation — 0.1 pp
    "GDP":    0.1,
    "RATE":   0.05,      # 5 bps
    "DEFAULT": 0.05,
}
```

The context injection notes the elapsed time since release and explicitly instructs the model to look for **second-wave** setups rather than chasing the initial spike:

```
=== BREAKING EVENT [14:03 UTC] ===
POST-EVENT: NFP released 3 minutes ago.
Actual: 185K vs Forecast: 220K (MISS by -35K) — USD bearish surprise.
Look for second-wave pullback entry after initial reaction settles.
```

### Deduplication

Both briefs and triggers are deduplicated per event per day via `data/.pre_event_dedup.json`. Each event key fires at most once per mode (brief/trigger) per calendar day.

---

## 11. Pipeline 9 — Trade Execution

**File:** `agents/job3_executor.py`  
**Triggered by:** User approval (`approve <ID>` in Slack or CLI)

### Portfolio risk gate

Before any new Long or Short order is sent to MT5, `execute_signal()` calls `mt5/portfolio_risk.check_portfolio_risk()`. Three limits are checked in sequence:

| Limit | Config param | Default | Description |
|-------|-------------|---------|-------------|
| Max open trades | `JOB3_MAX_OPEN_TRADES` | `3` | Hard cap on concurrent positions across all pairs |
| Max total risk | `JOB3_MAX_PORTFOLIO_RISK_PCT` | `3.0%` | Aggregate risk (all open SLs + new trade) as % of equity |
| Max correlated risk | `JOB3_MAX_CORRELATED_RISK_PCT` | `2.0%` | Risk within one USD-direction bucket |

**USD-direction buckets** (`mt5/portfolio_risk.py`):

| Bucket | Positions |
|--------|-----------|
| `usd_short` | EURUSD Long, GBPUSD Long, AUDUSD Long, USDJPY Short |
| `usd_long` | EURUSD Short, GBPUSD Short, AUDUSD Short, USDJPY Long |

Risk per open position is estimated as `sl_pips × lot × pip_value_per_lot`. If no SL is set, `JOB3_DEFAULT_SL_PIPS` (30) is used as a conservative fallback. If any limit is breached, execution returns `{"ok": False, "error": "Portfolio risk guard: ..."}` and the Slack bot surfaces the reason to the user.

### Ticket map

On every successful execution, `approve_and_execute()` writes `{ticket: signal_id}` to `data/ticket_map.json`. This lets Job 2 reverse-lookup the original signal for any open position — providing the original thesis, time horizon, key levels, and invalidation condition to the LLM without storing redundant data in the position itself.

### Lot size

See [§8 Pipeline 6 — Position Sizing & Order Parameters](#8-pipeline-6--position-sizing--order-parameters). Lot size arrives at execution time as `signal["_lot_override"]`, pre-computed by `recommend_position_size()` after the debate. `execute_signal()` uses it directly. If absent (fallback path), `calculate_lot_size(equity, JOB3_RISK_PCT, sl_pips)` is used.

### Order routing

| Signal source | Signal type | MT5 action |
|---------------|-------------|------------|
| Job 1 / Fast watcher | Long | `ORDER_TYPE_BUY` market |
| Job 1 / Fast watcher | Short | `ORDER_TYPE_SELL` market |
| Job 1 with `limit_price` | Long/Short | `ORDER_TYPE_BUY_LIMIT` / `SELL_LIMIT` |
| Job 2 / Job 4 | Exit | Close full position |
| Job 2 / Job 4 | Trim | Close N% of position |
| Job 4 | SetSL / SetTP | Modify existing position |

### Minimum stop distance enforcement

MT5 rejects orders with `retcode=10016` (`TRADE_RETCODE_INVALID_STOPS`) when SL or TP is closer to the execution price than the broker's `stops_level` (a per-symbol setting in points). This commonly occurs when the LLM's `key_levels.support/resistance` happens to sit very close to the current price.

`mt5/order_manager.py` contains `_clamp_stops()`, called inside both `open_position()` and `place_limit_order()` before the request is sent:

```python
stops_level = mt5.symbol_info(symbol).stops_level   # broker minimum in points
min_dist    = (stops_level + 3 * 10) * info.point   # +3 pip safety buffer
# For BUY: push SL further below price, push TP further above price if too close
# For SELL: push SL further above price, push TP further below price if too close
```

A 3-pip buffer (`_MIN_STOP_BUFFER_PIPS = 3`) is added on top of the broker minimum to handle the broker's floating stop level during volatile conditions. Adjustments are logged as warnings showing the original and adjusted price.

### R:R thresholds — two levels with distinct roles

Two config values govern R:R checks, each with a separate purpose:

**`JOB3_RR_WARNING` (default 1.5) — soft warning.**  
`compute_order_preview()` sets `rr_warning = True` when R:R < `JOB3_RR_WARNING`. This surfaces as a ⚠️ inline on the Slack order line. The analysis prompt (step 7) also instructs the model to flag R:R below this threshold in `risk_note`. The trade can still be approved and executed.

**`JOB3_MIN_RR` (default 0.5) — hard execution block.**  
A backstop gate in `execute_signal()` rejects any Long/Short where R:R falls below this value — even if the signal was manually approved from Slack. R:R is computed live from the signal's SL and TP prices at execution time:

```python
tp_pips = abs(tp_price - current_price) / pip
rr      = tp_pips / sl_pips
if rr < config.JOB3_MIN_RR:
    return {"ok": False, "error": f"R:R {rr:.2f} below minimum {config.JOB3_MIN_RR} — signal rejected"}
```

**Design intent:** The analysis prompt (step 7) always tries to produce a tradeable Long or Short — R:R no longer causes `Wait`. Low R:R is treated as a risk warning, not a disqualifier. The execution gate (`JOB3_MIN_RR = 0.5`) catches only genuinely negative-edge setups where TP is less than half the SL distance. Raise `JOB3_MIN_RR` to 1.0–1.5 once `outcome_log.json` has sufficient calibration data to support a tighter floor.

### Signal expiry

Signals older than `JOB3_SIGNAL_EXPIRY_MINUTES` (default 60) are automatically expired and cannot be approved. This prevents executing stale signals at prices that no longer reflect current conditions.

### Slack interactive buttons

Signal alert messages are sent as **Slack Block Kit** payloads with two action buttons: **Execute** (primary, green) and **Reject** (danger, red). This replaces the text-based `approve <ID>` / `reject <ID>` instructions.

**How it works:**

The bot uses Slack Bolt with Socket Mode — button clicks are routed over the same persistent WebSocket connection as messages. No public URL or webhook receiver is needed.

- `notifications/notifier.py` — `_build_signal_blocks()` wraps the signal text in Block Kit sections and appends an `actions` block with two buttons. The signal ID is embedded in each button's `value` field.
- `agents/slack_bot.py` — `@_app.action("approve_signal")` and `@_app.action("reject_signal")` handle button clicks. Both handlers: `ack()` immediately (Slack requires acknowledgement within 3s), then replace the buttons with a status line using `respond(replace_original=True)`, then execute the action in a background thread and update again with the result.

**Message lifecycle:**

```
Signal alert posted
  ↓
[ 📈 Execute Long ]  [ ✕ Reject ]
  ↓ (click Execute)
⏳ Executing signal A3F9BC12… (approved by @zhengwu)
  ↓ (MT5 fill confirmed)
✅ Executed by @zhengwu — Ticket 12345678 | Fill 1.27431 | Volume 0.12 lots
```

The original signal analysis above the buttons is preserved — only the action row changes. `respond(replace_original=True)` uses Slack's `response_url` from the button payload, which works regardless of whether the message was posted via Incoming Webhook or bot token.

**Slack App requirement:** `Interactivity & Shortcuts → On` in api.slack.com. For Socket Mode apps no Request URL is needed — interactions are received over the socket automatically.

---

## 12. Supporting Systems

### Central bank policy auto-updater

**File:** `pipeline/cb_policy_updater.py`

When the scanner detects a headline tagged `cb_decision` or `CB_speech`, a background thread fetches the latest official statement from the bank's website (Fed, ECB, BOE, or BOJ), uses Claude Haiku to extract the current stance and forward guidance, and writes to `data/rate_cycles.json`.

The extraction prompt returns:
```json
{
  "stance": "Pausing",
  "guidance": "No rush to cut; watching inflation and labour market",
  "expected": "1-2 cuts in 2025 per CME FedWatch",
  "source_date": "2026-04-08"
}
```

### Trade journal

**File:** `analysis/trade_journal.py`

Every Long/Short signal is recorded in `data/trade_journal.jsonl` at notify time with the agent's full recommendation. At execution the MT5 ticket is linked. At end-of-day validation the journal either reads MT5 deal history (for executed signals) or replays M15 price bars to compute hypothetical outcomes (for rejected/expired signals).

### Market regime engine

**File:** `pipeline/regime_agent.py`

The regime engine produces a structured text description of four dimensions:

1. **Risk Sentiment:** `Risk-ON`, `Risk-OFF`, or `Neutral` based on VIX level and pair realised volatility
2. **Technical Trend:** `Bullish`, `Bearish`, or `Ranging` based on SMA stack (price vs SMA20/50/200) and M15 momentum ratio
3. **Volatility:** `Elevated`, `Normal`, or `Low` compared to 30-day ATR baseline
4. **Macro Bias:** `Base Currency Bullish/Bearish/Neutral` based on 10Y yield spread + CB rate cycle stances

The regime text is injected into the context window before full analysis. The analysis prompt explicitly instructs the model to align its SL width with the volatility regime.

### Fast news watcher

**File:** `pipeline/fast_news_watcher.py`  
**Interval:** Every `FAST_WATCH_INTERVAL_MINUTES` (default: 2 min)

Polls only RSS feeds (ForexLive + FXStreet) — the two lowest-latency sources. Uses the same DedupCache as the main scanner to prevent double-processing. On a high-score article:

1. Calls `triage_headlines()` (same two-pass Azure GPT-5.2 system)
2. If score ≥ 6 and fast cooldown clear: immediately triggers full analysis
3. Sets `data/.cooldown_fast_{SYMBOL}` for `FAST_COOLDOWN_MINUTES` (default 20)

This reduces worst-case reaction time from 30 minutes (main scan interval) to ~5 minutes (2-min poll + ~3 min processing).

### Debate outcome tracker

**File:** `analysis/outcome_tracker.py`  
**Config flag:** `DEBATE_OUTCOME_TRACKING` (default: `True`)  
**Token cost:** Zero — uses MT5 tick data only

Every signal that reaches full analysis is recorded in `data/outcome_log.json` with:
- `pre_debate_signal` / `pre_debate_confidence` — Sonnet's raw output before debate
- `post_debate_signal` / `post_debate_confidence` — final output after debate override
- `debate_changed` — boolean, True when debate overrode Sonnet
- `uncertainty_score`, `bull_score`, `bear_score`, `risk_level`, `top_risk`, `winning_case`
- `entry_price` — live MT5 mid-price `(bid + ask) / 2` at signal generation time
- `outcomes.{8,16,24,48}` — MT5 mid-price at each checkpoint, pip move in signal direction, approximate SL/TP hit flag

The `outcome-checker` background thread runs every 30 minutes and fills in elapsed checkpoints. The `outcomes` Slack command shows a live summary of false negatives (debate blocked a signal that would have succeeded) and false positives (debate let through a signal that failed). This dataset is the primary input for future calibration of `DEBATE_MAX_UNCERTAINTY` and persona prompts.

### Phase threshold watcher

**File:** `agents/job2_position.py` — `run_phase_watcher_loop()`  
**Thread name:** `phase-watcher`  
**Interval:** Every `JOB2_PHASE_WATCH_INTERVAL_SEC` (default: 60 s)

The 30-minute Job 2 main loop is too coarse to react promptly when a position crosses a phase boundary (e.g. hits breakeven R or drops into loss territory). The phase watcher is a lightweight background thread that polls MT5 ticks every 60 seconds and triggers an immediate `analyze_position()` call the moment a transition is detected — reducing worst-case reaction time from 30 minutes to ~60 seconds.

**How it works:**

1. On first sight of a ticket, the watcher records the original SL distance into `_sl_cache[ticket]`. This value is frozen for the life of the trade so R is always computed relative to the original risk, not a trailing SL.
2. Each cycle computes a live R-multiple from the current MT5 price using `_watcher_compute_r()`.
3. The live phase is mapped via `_r_to_phase_simple()` (R-only, no overdue check — the main loop handles overdue).
4. If the phase has changed since the last cycle, `analyze_position()` is invoked in a sub-thread so the watcher poll loop is never blocked.
5. A per-ticket cooldown (`JOB2_PHASE_WATCH_COOLDOWN_SEC`, default: 300 s) prevents storm-triggering if price oscillates around a threshold.
6. Tickets that no longer appear in the open position list are evicted from all caches.

**State dicts (module-level):**
```python
_phase_state: dict[int, str]   # last known phase per ticket
_sl_cache:    dict[int, float]  # frozen original SL distance per ticket
_last_watcher_trigger: dict[int, float]  # timestamp of last watcher-triggered analysis per ticket
```

### Logger — Windows-safe rotation

**File:** `utils/logger.py`

The standard `RotatingFileHandler` raises `PermissionError` on Windows when it tries to rename the active log file during rotation, because Windows locks open files. This crashes the agent on startup if a previous instance is still running with the log file open.

The logger uses `_SafeRotatingFileHandler`, a thin subclass that catches `PermissionError` in `doRollover()` and skips the rotation silently — the process continues writing to the current file and retries rotation on the next write cycle. The handler also sets `delay=True` to defer file open until the first write, eliminating the startup lock race.

```python
class _SafeRotatingFileHandler(RotatingFileHandler):
    def doRollover(self) -> None:
        try:
            super().doRollover()
        except PermissionError:
            pass  # file locked by another process — skip this rotation cycle
```

### End-of-day collector

**File:** `pipeline/daily_collector.py`  
**Triggered:** Daily at 22:00 UTC

1. Fetches closing prices for all pairs + correlated assets (yfinance)
2. Saves ForexFactory events with any end-of-day actuals
3. Merges `intraday.jsonl` → `news.jsonl` (dedup by URL)
4. Generates `summary.md` via Claude Sonnet using headlines, events, and prices
5. Runs trade journal validation against MT5 deal history
6. Clears per-pair dedup caches for next trading day

---

## 13. LLM Model Assignments

| Task | File | Model | Rationale |
|------|------|-------|-----------|
| Triage pass 1 — score all headlines | `triage/triage_prompt.py` | Azure GPT-5.2 → Claude Haiku 4.5 fallback | Better calibration than Haiku; free via workplace |
| Triage pass 2 — review borderline 4–6 | `triage/triage_prompt.py` | Azure GPT-5.2 → Claude Haiku 4.5 fallback | Same; decisive boundary scoring |
| Pre-event scenario brief | `pipeline/pre_event_agent.py` | Claude Sonnet 4.6 | User-facing text; quality matters |
| Full signal analysis (Job 1) | `analysis/full_analysis_prompt.py` | Claude Sonnet 4.6 + extended thinking (10K budget) | Complex multi-factor reasoning; extended thinking forces sequential chain-of-thought before output |
| Signal debate personas (Bull/Bear/Risk Assessor) | `analysis/ensemble_agent.py` | Azure GPT-5.2 → Claude Haiku 4.5 fallback | Same Azure deployment; evidence-based personas benefit from calibrated model; 3 concurrent async calls |
| Signal debate judge (CIO) | `analysis/ensemble_agent.py` | Claude Sonnet 4.6 | Synthesis, scoring, and SL/TP adjustment; quality matters more than speed here |
| Position debate personas (Hold/Exit/Devil's Advocate) | `analysis/ensemble_agent.py` | Azure GPT-5.2 → Claude Haiku 4.5 fallback | Same `_gather_personas_async` path as signal debate; Devil focuses on cognitive bias (sunk-cost, fear) |
| Position debate judge | `analysis/ensemble_agent.py` | Claude Sonnet 4.6 | Same judge path as signal debate |
| Position monitor — routine 30-min check | `agents/job2_position.py` | Azure GPT-5.2 → Claude Haiku 4.5 fallback | Cost-efficient routine scan; 600-token prompt with news + indicators + reasoning_summary |
| Position monitor — priority / phase transition | `agents/job2_position.py` | Claude Sonnet 4.6 (`ANALYSIS_MODEL`) | Full enriched prompt; theory invalidation; risk management accuracy matters |
| Daily summary generation | `pipeline/daily_collector.py` | Claude Sonnet 4.6 (`ANALYSIS_MODEL`) | User-facing; quality matters |
| CB policy extraction | `pipeline/cb_policy_updater.py` | Claude Haiku 4.5 (`TRIAGE_MODEL`) | Structured JSON extraction; Haiku is sufficient |
| Chat assistant (Job 4) | `agents/job4_chat.py` | Claude Sonnet 4.6 (`ANALYSIS_MODEL`) | Conversational + tool calling; quality matters |
| Slack NLP intent classification | `agents/slack_bot.py` | Claude Haiku 4.5 (`TRIAGE_MODEL`) | Lightweight command routing; only runs when exact-match dispatch fails |

---

## 14. Configuration Reference

All parameters in `config.py`. Key values:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ACTIVE_PAIRS` | `["EURUSD","GBPUSD","USDJPY"]` | Pairs to monitor |
| `TRIAGE_SCORE_THRESHOLD` | `6` | Minimum score to trigger full analysis |
| `COOLDOWN_MINUTES` | `45` | Per-pair silence after main scanner escalation |
| `FAST_COOLDOWN_MINUTES` | `20` | Per-pair silence after fast watcher escalation |
| `SCAN_INTERVAL_MINUTES` | `30` | Main scanner interval |
| `FAST_WATCH_INTERVAL_MINUTES` | `2` | Fast watcher RSS poll interval |
| `NEWS_MAX_AGE_HOURS` | `8` | Drop articles older than this |
| `NEWS_WINDOW_DAYS` | `1` | Days back to fetch from batch APIs |
| `NEWS_PER_PROVIDER_MAX_ITEMS` | `20` | Max articles per provider per scan |
| `DEBATE_MAX_UNCERTAINTY` | `75` | Ensemble veto threshold (0–100); target 78-80 after calibration data |
| `DEBATE_OUTCOME_TRACKING` | `True` | Record pre/post-debate signals + MT5 price at 8/16/24/48h |
| `JOB2_BREAKEVEN_R` | `1.0` | R-multiple to trigger auto breakeven SL move |
| `JOB2_BREAKEVEN_BUFFER_PIPS` | `3` | Pips beyond entry for breakeven SL (avoids exact-entry wicks) |
| `JOB2_PARTIAL_TP_R` | `1.5` | R-multiple at which LLM is asked about partial trim |
| `JOB2_PARTIAL_TP_PCT` | `50.0` | Default trim % suggested to LLM at PROFIT phase |
| `JOB2_TRAIL_R` | `2.0` | R-multiple to start ATR-based trailing |
| `JOB2_TRAIL_ATR_MULT` | `1.5` | Trail SL at this multiple of ATR-M15 behind price |
| `JOB2_OVERDUE_MULT` | `1.5` | Position flagged overdue if open > N × expected horizon |
| `JOB2_MAX_SPREAD_MULT` | `2.0` | Skip modifications if spread > N × normal_spread_pips |
| `JOB2_NEWS_BLACKOUT_MIN` | `15` | Pause modifications if high-impact event < N min away |
| `JOB2_AUTO_BREAKEVEN` | `True` | Auto-execute breakeven SL without approval |
| `JOB2_AUTO_TRAIL` | `True` | Auto-execute trail SL updates without approval |
| `JOB2_PHASE_WATCH_INTERVAL_SEC` | `60` | How often the phase watcher polls MT5 ticks |
| `JOB2_PHASE_WATCH_COOLDOWN_SEC` | `300` | Min seconds between watcher-triggered analyses per ticket |
| `JOB3_RISK_PCT` | `2.0` | Per-trade risk cap (% of equity). `recommend_position_size()` allocates from the remaining portfolio budget and caps the result at this value. Used as flat fallback when portfolio data is unavailable. |
| `JOB3_MAX_LOT` | `5.0` | Maximum lot size cap |
| `JOB3_UNCERTAINTY_TIERS` | see config | Multipliers applied to remaining portfolio risk budget: ≤30→100%, ≤55→75%, ≤75→50% |
| `JOB3_AUTO_APPROVE_MAX_UNCERTAINTY` | `30` | Auto-execute without approval when uncertainty ≤ this. `None` disables. |
| `JOB3_MAX_OPEN_TRADES` | `3` | Hard cap on concurrent positions across all pairs |
| `JOB3_MAX_PORTFOLIO_RISK_PCT` | `3.0` | Max total portfolio risk % before blocking new entry |
| `JOB3_MAX_CORRELATED_RISK_PCT` | `2.0` | Max risk within one USD-direction bucket |
| `JOB3_MIN_RR` | `0.5` | Hard execution block — signals with R:R below this are rejected at execution time |
| `JOB3_RR_WARNING` | `1.5` | Soft warning threshold — ⚠️ shown in Slack preview; model instructed to flag in `risk_note` |
| `JOB3_SIGNAL_EXPIRY_MINUTES` | `60` | Signal validity window |
| `CONTEXT_MAX_TOKENS` | (config) | Context window budget for analysis LLM |
| `CONTEXT_DAYS_SUMMARY` | `7` | Days of daily summaries to include |
| `CONTEXT_DAYS_PRICES` | `30` | Days of price data for trend table |
| `MARKET_HOURS_START` | `7` | UTC hour — start of active scanning |
| `MARKET_HOURS_END` | `23` | UTC hour — end of active scanning |
| `DAILY_COLLECTION_TIME_UTC` | `"22:00"` | End-of-day collection trigger |
| `USE_REGIME_ANALYSIS` | `True` | Inject regime into context |
| `USE_MULTI_AGENT_DEBATE` | `True` | Run ensemble debate on Long/Short |
| `PRE_EVENT_BRIEF_MIN` | `30` | Lower bound for pre-event brief window (min) |
| `PRE_EVENT_BRIEF_MAX` | `90` | Upper bound for pre-event brief window (min) |

---

## 15. Design Decisions

### Why human approval is required

Forex markets can gap, spread, and move sharply on news. Fully automated execution would require robust risk management infrastructure (circuit breakers, max drawdown limits, position size caps enforced at the broker level) that is beyond the scope of this system. Human approval provides a natural circuit breaker and ensures the trader retains full situational awareness.

### Why extended thinking for full analysis

Without extended thinking, Claude Sonnet tends to produce a signal quickly after reading the context without fully resolving conflicts between macro signals and technical signals. Extended thinking forces the model to complete the analytical chain (macro → regime → MTF → momentum → entry) before outputting a signal direction. In practice this reduces the frequency of conflicted signals (where macro says long but TA says short and the model outputs Long without acknowledging the conflict).

### Why two-pass triage instead of one more powerful pass

A single pass with a powerful model on 20 headlines tends to regress toward a median score. Two passes — coarse scoring followed by targeted review of borderline cases — produces better discrimination at the escalation threshold (score 5 vs 6) while keeping the majority of headlines on the cheap fast pass.

### Why RSS is polled separately at 2-minute intervals

The main 30-minute scan interval was designed around batch API rate limits. RSS feeds have no rate limits and update within 2–5 minutes of publication. Running them on a dedicated 2-minute thread means breaking news (NFP surprise, Fed emergency statement, geopolitical shock) is detected and analysed within ~5 minutes rather than waiting up to 30 minutes for the next scan cycle. The shared DedupCache ensures no article is triaged twice.

### Why personas don't see the original signal direction

If the Bull analyst is told "the original signal is Long," it anchors its analysis to confirming that direction. By withholding the direction, each persona starts from first principles using only the market data. This produces more genuine disagreement and makes the Judge's uncertainty score more meaningful. The Devil's Advocate is an exception because it needs the specific entry/SL/TP to critique the trade mechanics.

### Why personas were changed from adversarial to evidence-based

The original design used maximally one-sided personas ("Do NOT hedge, find EVERY piece of evidence"). This was intended to surface the strongest possible bull and bear cases. In practice it produced a systematic failure: both personas always scored 70+ regardless of what the data showed, because they were instructed to produce strong arguments unconditionally. The judge's uncertainty formula ("both score 65+ → high uncertainty") then fired on nearly every signal, causing the debate to veto the majority of Long/Short signals even when market conditions were unambiguous.

The root cause is that adversarial personas are **data-independent** — they produce equally strong arguments whether the context clearly favours one direction or not. Evidence-based personas are **data-dependent** — when context strongly favours a long, the Bull scores 78 and the Bear scores 42. The judge sees a 36-point gap and correctly calls it low uncertainty.

The Devil's Advocate had a related problem: it was unconditionally instructed to argue the trade would fail, adding a permanent "no" vote on top of Bear. The Risk Assessor replacement rates specific risks as High/Medium/Low, so on a clean setup with low event risk and a well-placed SL, it gives a Low failure probability — which the judge weighs appropriately.

### Why `_safe_fmt()` instead of `str.format()`

LLM outputs frequently contain price ranges in curly-brace notation (e.g. `{1.0820–1.0850}`) and JSON snippets. Python's `str.format()` interprets these as format placeholders and raises `KeyError`. The custom `_safe_fmt()` helper performs sequential `str.replace()` substitutions, so curly braces in values are never interpreted as format tokens.

### Why the staleness filter defaults to 8 hours

The oldest significant forex news that would still warrant analysis is roughly the previous trading session (8–10 hours). Articles older than this are either already priced into the market or represent stale background information better handled by the daily summary in the context window. Setting the filter at 8 hours eliminates the majority of indexing-delayed articles from NewsAPI and EODHD while retaining same-session news from early-morning scanner runs.

### Why the debate notification header reflects the outcome, not a static label

The original Slack debate section always showed "Ensemble Debate — Long/Short/Wait?" regardless of what happened. This caused confusion: if the code guard preserved the original Long signal despite the judge returning Wait, the header still showed a static label and the verdict line said "Wait" — contradicting the signal header above it.

The `_debate_header()` helper in `notifier.py` now generates the header dynamically based on the actual outcome: `Confirmed Long (uncertainty 38/100)`, `Direction flipped to Short`, `Proceeded with caution (uncertainty 62/100)`, or `VETOED`. The judge adjustment line is only shown when it differs from the final signal direction, so a code-guard-preserved signal no longer shows a misleading "Wait" verdict.

### Why the scanner Wait block needed an `_signal_id` guard

When the debate downgrades a Long/Short signal to Wait, `scanner.py` runs the following sequence: (1) Long/Short block executes — saves to pending store, sets `signal["_signal_id"]`, records in outcome tracker with correct `pre_debate_signal="Long"`. (2) Debate runs, changes `signal["signal"]` to `"Wait"`. (3) Wait recording block checks `signal.get("signal") == "Wait"` — this is now True, so it calls `record_signal(pre_debate_signal="Wait", ...)`, overwriting the correct record.

The fix: `and not signal.get("_signal_id")` in the Wait block condition. The `_signal_id` is only set when the Long/Short block ran, so any signal that was originally Long/Short (even if debate downgraded it) is correctly skipped by the Wait block.

### Why CB policy updates are triggered by headlines, not on a schedule

Central bank policy shifts are event-driven, not time-driven. Polling the Fed website every 30 minutes would be wasteful and would not improve latency. By triggering the update only when the news scanner detects a `cb_decision` or `CB_speech` headline, the update fires within one scan cycle of the event — typically within 30 minutes of a real policy announcement — without unnecessary API calls.
