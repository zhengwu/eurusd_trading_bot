# Forex Multi-Pair Agent

An automated trading assistant that monitors multiple currency pairs, watches open positions, and executes trades on MetaTrader 5 — all controlled through Slack.

**Active pairs (default):** EUR/USD · GBP/USD · USD/JPY

Adding or removing pairs requires one line change in `config.py`.

---

## Table of Contents

1. [What is this?](#what-is-this)
2. [How it works — the 4 jobs](#how-it-works--the-4-jobs)
3. [Multi-agent ensemble debate](#multi-agent-ensemble-debate)
4. [Trade journal](#trade-journal)
5. [Requirements](#requirements)
6. [Installation and setup](#installation-and-setup)
   - [1. Download the code](#1-download-the-code)
   - [2. Create the Conda environment](#2-create-the-conda-environment)
   - [3. Create the .env file](#3-create-the-env-file)
   - [4. Set up a Slack app](#4-set-up-a-slack-app)
7. [Running the agent](#running-the-agent)
8. [Slack commands](#slack-commands)
9. [Approval workflow — a full example](#approval-workflow--a-full-example)
10. [File structure](#file-structure)
11. [Configuration reference](#configuration-reference)
12. [Troubleshooting](#troubleshooting)

---

## What is this?

A Python-based AI agent that runs 24/5 (while the forex market is open) and does three things automatically:

1. **Reads the news.** A fast news watcher polls RSS feeds every 2 minutes for breaking headlines; the main scanner runs every 30 minutes across all providers. Headlines are scored 1–10 using a two-pass triage system (Azure GPT-5.2 for both passes, Claude Haiku fallback), and — when something important is detected — runs a deep analysis using Claude Sonnet to generate a trading signal (Long, Short, or Wait). Each pair has its own news queries, cooldown timer, and deduplication cache so they never interfere with each other.

2. **Watches your open positions.** Every 30 minutes it reads all open MT5 positions across all active pairs and uses Claude Sonnet to recommend whether to Hold, Trim, or Exit each one based on current market conditions.

3. **Executes trades.** When you approve a signal (via Slack or the command line), the agent places or closes the order on MT5 directly. Position sizing is calculated automatically from your account equity and the stop loss distance — scaled by the signal's uncertainty score so high-conviction trades get full size and uncertain trades get reduced size. High-conviction signals (uncertainty ≤ 30) are **auto-executed without manual approval**.

You stay in control. The agent never trades without your approval. Signals arrive in Slack with **clickable Execute and Reject buttons** — no typing required. You can also approve at a specific limit price (`approve XXXXXXXX limit 1.0820`), start a pair-specific chat session, and have a full conversation with the agent about market conditions — all from Slack.

**Multi-agent ensemble debate.** Every Long or Short signal passes through a three-persona debate before reaching you. A Bull analyst, a Bear analyst, and a Risk Assessor evaluate the trade concurrently (via `asyncio.gather`, Azure GPT-5.2). A Judge model (Claude Sonnet) synthesises the debate, scores uncertainty 0–100 based on the evidence gap between Bull and Bear, and can veto signals above the threshold or adjust SL/TP. The debate summary — including the top risk rated High/Medium/Low and an R:R warning if below 1.5 — is included in every Slack signal alert. You can also trigger a separate Hold/Trim/Exit debate on your open positions at any time with `debate positions`.

**Trade journal.** Every Long/Short signal is automatically recorded in `data/trade_journal.csv` the moment it is notified, capturing the agent's suggested entry, SL, TP, rationale, debate verdict, and uncertainty score. When a signal is executed, the MT5 ticket is linked. The daily validation job checks MT5 deal history for closed positions and computes actual P&L; for signals you skipped, it replays M15 price bars to compute a hypothetical outcome (which level — TP or SL — was hit first). Use `journal report` in Slack to see win rates, pip totals, and P&L by pair.

**Debate outcome tracking.** Every signal (Long, Short, or Wait) is automatically recorded in `data/outcome_log.json` with the Sonnet pre-debate decision, the final post-debate decision, the full uncertainty score, and persona scores. A background thread fetches the actual MT5 price at 8h, 16h, 24h, and 48h after signal generation and computes the pip move. This builds a calibration dataset: you can see "debate overrode Long to Wait → price moved +40 pips" (false negative) vs "debate let Long through → price dropped" (false positive). Use `outcomes` in Slack for a summary. Disable with `DEBATE_OUTCOME_TRACKING = False` in `config.py` — zero token cost, uses MT5 ticks only.

**Market regime engine.** Before every full analysis the agent computes a four-dimensional market regime:

- **Risk Sentiment** — VIX level + pair realized volatility from M15 bars
- **Technical Trend** — daily SMA20/50/200 stack alignment + M15 bar momentum ratio
- **Volatility** — ATR-14 vs 30-day ATR baseline (are stops standard or need widening?)
- **Macro Bias** — live 10Y government bond yield spreads (fetched from ECB, BOE, and Japan MOF) combined with central bank rate cycle stances and forward guidance

Claude Sonnet sees this regime summary alongside the news and price data, and is instructed to explain how its trade direction and stop loss width align with (or conflict with) the current regime.

**Automatic central bank policy tracking.** Whenever the news scanner detects a central bank headline (Fed, ECB, BOE, or BOJ — any speech, press conference, or rate decision), the agent automatically fetches the latest official statement from the bank's website, uses Claude Haiku to extract the current stance and forward guidance, and writes the result to `data/rate_cycles.json`. The regime engine picks up the update immediately on the next analysis run. A Slack notification is sent, with `[STANCE CHANGED]` flagged when the stance differs from the previous reading.

**Low-latency news pipeline.** Headlines are sourced from seven providers in order of latency: ForexLive/FXStreet RSS (~2–5 min), FMP (~5 min), Finnhub (~5 min), then NewsAPI, AlphaVantage, EODHD, and Yahoo Finance. Articles older than 8 hours are dropped automatically. The fast watcher polls RSS every 2 minutes in a dedicated background thread, so breaking news triggers analysis in ~5 minutes rather than up to 30.

---

## How it works — the 4 jobs

The system is organised into four jobs. All four start automatically when you run the agent.

### Job 1 — Opportunity Scanner

Job 1 runs two parallel news loops and an end-of-day collector:

#### Fast news watcher (every 2 minutes)

A dedicated background thread polls ForexLive and FXStreet RSS feeds every 2 minutes — the lowest-latency sources available (~2–5 minutes from publication). When a high-score article is found it immediately triggers full analysis, bypassing the 30-minute scan wait. Uses a separate short cooldown (`FAST_COOLDOWN_MINUTES`, default 20 min) so it never blocks the main scan.

#### Main scanner (every 30 minutes)

Every 30 minutes during market hours it:

1. **Refreshes the economic calendar** — fetches today's ForexFactory events (USD, EUR, GBP, JPY) with the latest actual vs forecast data. This happens once per cycle so Claude always has up-to-date beat/miss information during the trading day.

2. **Scans news for each active pair** — for EURUSD, GBPUSD, and USDJPY independently:
   - Fetches headlines from FMP (forex news API), Finnhub (forex news with `minId` cursor), ForexLive/FXStreet RSS, NewsAPI, AlphaVantage, and EODHD — in order of latency (fastest first)
   - Drops articles older than `NEWS_MAX_AGE_HOURS` (default 8h) to filter stale delayed articles
   - Filters new articles through a per-pair deduplication cache (shared with the fast watcher)
   - Sends headlines through a two-pass triage system:
     - **Pass 1** — Azure GPT-5.2 scores all headlines 1–10 with calibrated examples (fallback: Claude Haiku)
     - **Pass 2** — Azure GPT-5.2 re-evaluates borderline scores (4–6), pushing them decisively up or down to reduce clustering around 5 (fallback: Claude Haiku)
   - Tags include `cb_decision`, `CB_speech`, `macro_data`, `geopolitical`, `risk_off`, `risk_on`, `other`. CB headlines also carry a `cb_bank` field (`Fed`, `ECB`, `BOE`, or `BOJ`)

3. **Triggers CB policy update** — any headline tagged `cb_decision` or `CB_speech` launches a background fetch of that bank's latest official statement and a Haiku-powered extraction of the current stance and guidance. Updates `data/rate_cycles.json` automatically, with per-bank per-day deduplication to avoid redundant fetches.

4. **Escalates high-score articles** — if any headline scores 6 or above and the pair is not in cooldown, Job 1 triggers a full deep analysis using Claude Sonnet with extended thinking enabled. It feeds the model a rich context package:
   - Today's scored headlines
   - Economic events with actuals vs forecasts
   - Current market regime (risk sentiment, technical trend, volatility, macro bias with live yield spreads and rate cycle stances)
   - Upcoming 48-hour economic calendar
   - Live price: tick, M15 OHLC bars, daily SMA20/50/200, ATR-14, correlated asset prices
   - Past 7 days of daily summaries

Claude returns a structured signal:

```
GBP/USD Long  [High] — 1-3 days
Trigger [8]: UK CPI beats forecast at 3.2%, BOE rate cut bets fade

Price: 1.2743  Open 1.2698  H 1.2748  L 1.2691  +0.31%
Trend: Bullish — above SMA20/50, positive momentum on M15

Rationale: UK CPI surprise (3.2% vs 2.9% forecast) reduces BOE cut
probability. Combined with soft US retail sales, fundamental setup
favours GBP/USD upside.

Key Levels: Support 1.2700 | Resistance 1.2800
Invalidation: Close below 1.2680 (SMA20)
Risk Note: Fed speakers at 15:00 UTC could reverse USD weakness

Order Details (live)
Entry 1.2743  SL 1.2680 (-63p)  TP 1.2820 (+77p)
Lot 0.12  Risk $100  R:R 1:1.2

approve A3F9BC12    |    reject A3F9BC12
```

After triggering, Job 1 sets a per-pair cooldown (default 45 minutes). EURUSD cooldown does not block GBPUSD escalation — each pair is fully independent.

Job 1 also runs an end-of-day collector at 22:00 UTC in a background thread: fetches closing prices and events, generates daily summary files, and cleans up old signals.

### Job 2 — Position Monitor (runs every 30 minutes + 60-second phase watcher)

Job 2 runs as two coordinated background threads:

- **Main loop (every 30 min):** reads all open positions and runs a full LLM analysis for each.
- **Phase watcher (every 60 sec):** polls MT5 ticks and detects when a position crosses a phase threshold (e.g. reaches breakeven R, enters loss territory). On a transition, it immediately triggers a full analysis — bypassing the 30-minute wait. A 5-minute per-position cooldown prevents storm-triggering when price oscillates around a boundary.

For each position the analysis calls Claude Sonnet with:
- Position details: direction, volume, entry price, current P&L in dollars and pips, SL/TP levels
- Account health: equity, balance, free margin, margin level, drawdown
- Current market snapshot: live bid/ask, spread
- Recent market context for the position's pair

Claude returns one of three recommendations:

- **Hold** — position is on track; informational alert only, no approval needed.
- **Trim** — risk/reward has deteriorated but direction still valid; partially close.
- **Exit** — original thesis is broken; close the full position.

Trim and Exit signals are saved as pending and posted to Slack for your approval.

### Job 3 — Trade Executor

Job 3 runs when you approve a signal. It routes each signal type to the correct MT5 action:

| Signal source | Action | MT5 operation |
|---|---|---|
| Job 1 | Long | Open BUY market order |
| Job 1 | Short | Open SELL market order |
| Job 2 / Job 4 | Exit | Close full position |
| Job 2 / Job 4 | Trim | Close partial position (default 50%) |
| Job 4 | SetSL / SetTP / SetSLTP | Modify stop loss / take profit |

For new positions (Long/Short), Job 3 calculates lot size using **uncertainty-scaled risk budgeting**:

1. **Base risk** — `JOB3_RISK_PCT` (default 2%) of current account equity
2. **Uncertainty multiplier** — scaled down by the ensemble debate uncertainty score:

| Uncertainty score | Conviction | Risk multiplier | Effective risk (base 2%) |
|---|---|---|---|
| ≤ 30 | High | 1.00× | 2.0% |
| 31 – 55 | Medium | 0.75× | 1.5% |
| 56 – 75 | Low (passed veto) | 0.50× | 1.0% |
| > 75 | — | blocked (vetoed) | — |

Lot = `(equity × effective_risk%) / (sl_pips × pip_value_per_lot)`, clamped to `[JOB3_MIN_LOT, JOB3_MAX_LOT]` (default 0.01 – 5.0). Pip values are pair-aware — USD-quote pairs use $10/pip per standard lot; JPY pairs use the approximate JPY pip value configured in `config.py`.

**Minimum R:R gate.** Before placing any order, Job 3 computes `R:R = TP distance / SL distance` from the live prices. If R:R < `JOB3_MIN_RR` (default 1.0), the signal is rejected — even if already approved. The analysis prompt enforces the same threshold upstream so the LLM outputs `Wait` instead of generating unviable setups in the first place.

### Job 4 — Chat Assistant

Job 4 is a conversational interface that can be seeded with a pair's market analysis. Start a session with `chat`, `chat GBPUSD`, or `chat USDJPY`. Claude opens with the full market analysis pre-loaded and then answers questions with live tool access:

| Tool | What Claude fetches |
|---|---|
| `get_positions` | All open MT5 positions across all active pairs with live P&L and pips |
| `get_account` | Equity, balance, free margin, margin level, drawdown |
| `get_news` | Today's scored headlines for the session's pair |
| `get_market_context` | Full context: prices, technicals, correlated assets, events, regime |
| `get_pending_signals` | Current pending signal queue |
| `modify_order` | Adjust SL, TP, or lot size on a pending signal |
| `propose_order` | Create a new pending signal for your approval |

Chat sessions are threaded and pair-aware. Each thread keeps 2 hours of history. The pair context (system prompt, news fetch, market context) matches the pair the session was started for. Say `end chat` to close the session.

---

## Multi-agent ensemble debate

Every Long or Short signal — whether generated by the automatic news scanner or an on-demand `analyze` — passes through a multi-model debate before being posted to Slack.

### Signal debate (Bull / Bear / Risk Assessor → Judge)

Three persona calls run concurrently via `asyncio.gather` using Azure GPT-5.2 (Claude Haiku fallback):

| Persona | Role |
|---|---|
| **Bull analyst** | Evaluates the long case based on evidence actually present in the context — reflects strength honestly, including mixed signals |
| **Bear analyst** | Evaluates the short case based on evidence actually present — does not argue for a short if the data doesn't support it |
| **Risk Assessor** | Identifies the 2-3 most concrete risks to the proposed setup and rates each High / Medium / Low — does not automatically argue the trade will fail |

Personas are evidence-based rather than adversarial by design. When the context clearly favours one direction, the winning persona scores high (75+) and the opposing persona scores low (40s), giving the Judge a clear signal. Only genuinely ambiguous contexts produce similar scores on both sides.

A Judge model (Claude Sonnet) then reads all three arguments alongside the signal's entry, SL, and TP and returns:

```json
{
  "evaluations": {
    "bull_score": 72,
    "bear_score": 45,
    "winning_case": "Bull",
    "top_risk": "CPI release in 6h could reverse move; SL below 1.0795 vulnerable to stop hunt",
    "risk_level": "Medium"
  },
  "uncertainty_score": 38,
  "uncertainty_rationale": "Bull 72 vs Bear 45, 27-pt gap; medium conviction; Sonnet agreement -10pts applied",
  "final_decision": "Long",
  "confidence_override": "Medium",
  "trade_setup": {
    "entry_assessment": "Entry near support — reasonable",
    "sl_assessment": "SL slightly tight for current ATR",
    "tp_assessment": "TP at key resistance — well-placed",
    "suggested_sl_adjustment": 1.0795,
    "suggested_tp_adjustment": null
  }
}
```

The Judge scores uncertainty based on the **gap** between Bull and Bear scores: if Bull scores 72 and Bear scores 45 (gap = 27), uncertainty is low. If both score 75+ or the gap is under 10 points, uncertainty is high. If the primary Sonnet analysis agrees with the debate winner, the Judge reduces uncertainty by 10 points as a confirmation bonus.

**`final_decision` rules** (strictly enforced in the judge prompt and code):
- Uncertainty ≤ 70: judge returns `Long` or `Short` — the stronger of Bull/Bear. Never `Wait`.
- Uncertainty 71–75: `Wait` only if both Bull and Bear score below 55. Otherwise returns direction.
- Uncertainty > 75: `Wait` used freely — situation genuinely ambiguous.
- Code guard: even if the judge returns `Wait` with uncertainty ≤ `DEBATE_MAX_UNCERTAINTY`, the original Sonnet signal is preserved. The uncertainty threshold is the single gate for Wait downgrades.

If `uncertainty_score` exceeds `DEBATE_MAX_UNCERTAINTY` (default 75), the signal is automatically downgraded to **Wait** and a veto reason is added to the Slack alert. The Judge can also adjust SL/TP and override confidence.

The debate summary in Slack shows the actual outcome — not a static header:

```
Ensemble Debate — Confirmed Long (uncertainty 38/100)
📈 Bull 72 | 📉 Bear 45 | Winner: Bull
🟢 Uncertainty 38/100 — Bull 72 vs Bear 45, 27-pt gap; Sonnet agreement -10pts
🟡 Top risk [Medium]: CPI release in 6h could reverse move; SL below 1.0795 vulnerable

Order Details (live)
Entry 1.0820  SL 1.0795 (-25p)  TP 1.0900 (+80p)
Lot 0.12  Risk $100  R:R 1:3.2

[ 📈 Execute Long ]  [ ✕ Reject ]
```

When R:R falls below 1.5, an inline ⚠️ warning appears on the order line. The debate header reflects what actually happened: `Confirmed Long`, `Direction flipped to Short`, `Proceeded with caution`, or `VETOED`.

### Position debate (Hold / Trim / Exit → Judge)

Run on demand with `debate positions` (all open positions) or `debate positions USDJPY` (one pair). Uses a separate set of personas, also running on Azure GPT-5.2 (Claude Haiku fallback) concurrently:

| Persona | Model | Role |
|---|---|---|
| **Hold analyst** | Azure GPT-5.2 | Defends staying in — thesis still intact |
| **Exit analyst** | Azure GPT-5.2 | Argues for cutting — thesis broken or risk/reward deteriorated |
| **Devil's Advocate** | Azure GPT-5.2 | Surfaces cognitive bias (sunk-cost thinking, fear of giving back gains) |
| **Judge** | Claude Sonnet 4.6 | Synthesises all three; returns Hold/Trim/Exit verdict with SL/TP adjustments |

The position Devil's Advocate focuses specifically on emotional decision-making biases, not trade destruction — distinguishing it from the signal debate's Risk Assessor.

### Model configuration

```python
# config.py
DEBATE_PERSONA_MODEL = "claude-haiku-4-5-20251001"  # fallback only — active personas use Azure GPT-5.2
DEBATE_JUDGE_MODEL   = "claude-sonnet-4-6"           # synthesis step — quality matters
USE_MULTI_AGENT_DEBATE   = True
DEBATE_MAX_UNCERTAINTY   = 75   # veto threshold (0–100); target 78-80 after calibration
DEBATE_OUTCOME_TRACKING  = True # record pre/post-debate signals + 8/16/24/48h price outcomes
```

Personas run on Azure GPT-5.2 (same deployment used for triage) with Claude Haiku as a per-call fallback. The judge remains on Claude Sonnet. Total debate latency is approximately one persona call (parallel) plus one judge call — typically 20–40 seconds.

---

## Trade journal

Every Long/Short signal is recorded automatically in `data/trade_journal.csv` the moment it is notified. The journal tracks the full lifecycle from suggestion to outcome.

### What is recorded

At **notify time** (Option A — agent's original suggestion):
- Signal ID, symbol, direction, confidence, time horizon, rationale, risk note, invalidation
- `suggested_entry`, `suggested_sl`, `suggested_tp`, `suggested_lot` — from the order preview
- `limit_price` — if the signal was proposed as a limit order
- `debate_uncertainty`, `debate_verdict`, `debate_veto` — ensemble debate results
- `source` — `job1` (auto scanner), `manual` (on-demand `analyze`), `job4` (chat session)

At **execution time**:
- `mt5_ticket` — the MT5 position ticket linked to this signal

At **validation time** (runs nightly via the daily collector, or on demand with `journal validate`):
- `actual_entry`, `actual_sl`, `actual_tp`, `actual_lot` — read from MT5 deal history
- `outcome` — `TP_hit`, `SL_hit`, `manual_close`, `still_open`
- `pnl_pips`, `pnl_usd`, `close_price`, `closed_at`

### Hypothetical outcomes for skipped signals

For signals you rejected or that expired, the journal replays M15 price bars from the signal time to determine which level was hit first. This lets you evaluate whether skipping a signal was the right call.

- **`hypothetical_tp`** — TP would have been hit first
- **`hypothetical_sl`** — SL would have been hit first
- **`hypothetical_open`** — neither level was reached within the validation window

The bar replay is conservative: if both TP and SL are breached within the same M15 bar (a spike), SL is assumed to have hit first.

### User modifications

If you modify entry, SL, or TP after the signal is created (directly in MT5 or via the terminal), the journal handles this automatically:
- `suggested_*` fields preserve the agent's original recommendation
- `actual_*` fields are populated from MT5 deal history at validation time — capturing what you actually traded

The delta between suggested and actual is stored side by side in the CSV, making it easy to measure whether your manual adjustments improved or hurt outcomes.

### Slack commands

| Command | What it does |
|---|---|
| `journal` | Last 10 journal entries with outcome and P&L |
| `journal report` | Performance summary: win rate, total pips, P&L by category and pair |
| `journal validate` | Force validation against MT5 right now |

### Storage

| File | Purpose |
|---|---|
| `data/trade_journal.jsonl` | Append-only source of truth (one JSON object per line) |
| `data/trade_journal.csv` | Regenerated from JSONL on every write — open in Excel or pandas |

---

## Requirements

**Operating system:** Windows only. The MetaTrader 5 Python package only runs on Windows.

**MetaTrader 5 terminal:** The MT5 desktop application must be running and logged into your broker account before you start the agent. All active pair symbols must be visible in the Market Watch.

**Python environment:** Anaconda or Miniconda. The instructions below use a Conda environment named `mt5_env`.

**External accounts required:**

| Service | Used for | Free tier? |
|---|---|---|
| Anthropic | Claude Haiku 4.5 (CB policy extraction, triage fallback, Slack NLP, persona fallback) + Claude Sonnet 4.6 (full analysis, position monitor, debate judge, chat, daily summary, pre-event brief) | No — pay per token |
| Azure OpenAI | GPT-5.2 — two-pass triage scoring + ensemble debate personas (Bull/Bear/Risk Assessor + position Hold/Exit/Risk) | Workplace/institutional access |
| FMP | Low-latency forex news (~5 min) | Paid |
| Finnhub | Low-latency forex news (~5 min) | Yes (free tier) |
| NewsAPI | Financial news headlines per pair | Yes (100 req/day) |
| AlphaVantage | Financial news + sentiment | Yes (25 req/day) |
| EODHD | Pair-specific news feed | Yes (limited) |
| Slack | Notifications + two-way command interface | Yes |
| ForexFactory | Economic calendar with actuals/forecasts | Free (public JSON) |
| yfinance | Daily prices + US10Y (^TNX) | Free |
| ECB Data Portal | DE10Y daily yield (EUR/USD macro bias) | Free (no key needed) |
| Bank of England API | UK10Y daily yield (GBP/USD macro bias) | Free (no key needed) |
| Japan Ministry of Finance | JP10Y daily yield (USD/JPY macro bias) | Free (no key needed) |

---

## Installation and setup

### 1. Download the code

```bash
git clone <repository-url> C:\Users\<yourname>\Documents\eurusd_agent
```

Or download the ZIP and extract to `C:\Users\<yourname>\Documents\eurusd_agent`.

### 2. Create the Conda environment

Open Anaconda Prompt and run:

```bash
conda create -n mt5_env python=3.11 -y
conda activate mt5_env
pip install MetaTrader5
pip install slack-bolt
pip install pdfminer.six
pip install -r C:\Users\<yourname>\Documents\eurusd_agent\requirements.txt
```

`MetaTrader5` is Windows-only and installed separately. `slack-bolt` is only needed for the two-way Slack command interface. `pdfminer.six` is used to extract text from Bank of Japan PDF policy statements.

Verify the installation:

```bash
cd C:\Users\<yourname>\Documents\eurusd_agent
python verify_imports.py
```

### 3. Create the .env file

Create a file named `.env` in the `eurusd_agent` folder (filename starts with a dot, no extension). In Notepad, save it as type "All Files" and name it `.env`.

```
# Anthropic — https://console.anthropic.com/
ANTHROPIC_API_KEY=sk-ant-...

# Azure OpenAI — triage scoring (both passes), free via workplace
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com
AZURE_OPENAI_API_VERSION=2025-04-01-preview
AZURE_OPENAI_DEPLOYMENT=gpt-5.2
AZURE_OPENAI_API_KEY=...

# FMP — https://financialmodelingprep.com/ (low-latency forex news)
FMP_API_KEY=...

# Finnhub — https://finnhub.io/ (low-latency forex news, free tier)
FINNHUB_API_KEY=...

# NewsAPI — https://newsapi.org/register
NEWS_API_KEY=...

# AlphaVantage — https://www.alphavantage.co/support/#api-key
ALPHA_VANTAGE_API_KEY=...

# EODHD — https://eodhd.com/register
EODHD_API_KEY=...

# Slack — incoming webhook (one-way signal alerts posted to a channel)
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...

# Slack — Socket Mode bot (two-way commands: approve, reject, chat, etc.)
# SLACK_BOT_TOKEN: Bot User OAuth Token (starts with xoxb-)
# SLACK_APP_TOKEN: App-Level Token (starts with xapp-)
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...

# Email alerts (optional — leave blank to disable)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your@gmail.com
SMTP_PASSWORD=your-app-password
```

### 4. Set up a Slack app

#### Step 4a — Create the app

1. Go to https://api.slack.com/apps and click **Create New App → From scratch**.
2. Name it (e.g. `Forex Agent`) and select your workspace.

#### Step 4b — Add bot token scopes

Under **OAuth & Permissions → Bot Token Scopes**, add:
- `chat:write`
- `im:history`
- `channels:history`
- `app_mentions:read`

#### Step 4c — Enable incoming webhooks

1. **Incoming Webhooks → Activate Incoming Webhooks → On**.
2. Click **Add New Webhook to Workspace**, select your alerts channel.
3. Copy the webhook URL into `.env` as `SLACK_WEBHOOK_URL`.

#### Step 4d — Enable Socket Mode

1. **Socket Mode → Enable Socket Mode → On**.
2. Create an App-Level Token with the `connections:write` scope.
3. Copy the `xapp-` token into `.env` as `SLACK_APP_TOKEN`.

#### Step 4d.1 — Enable Interactivity (required for buttons)

1. **Interactivity & Shortcuts → toggle On**.
2. No Request URL is needed for Socket Mode — button clicks are routed over the same websocket automatically.

This enables the Execute and Reject buttons on signal alerts.

#### Step 4e — Subscribe to bot events

Under **Event Subscriptions → Subscribe to bot events**, add:
- `message.im`
- `message.channels`
- `app_mention`

Save changes.

#### Step 4f — Install the app and get the bot token

1. **OAuth & Permissions → Install to Workspace**.
2. Copy the **Bot User OAuth Token** (`xoxb-`) into `.env` as `SLACK_BOT_TOKEN`.

#### Step 4g — Invite the bot to your channel

In Slack, open your alerts channel and type `/invite @Forex Agent`.

---

## Running the agent

Make sure MetaTrader 5 is open and all active pair symbols are visible in Market Watch.

```bash
conda activate mt5_env
cd C:\Users\<yourname>\Documents\eurusd_agent
python -m agents.job1_opportunity
```

Startup output:

```
============================================================
Forex Multi-Pair Agent — Job 1
  Active pairs  : ['EURUSD', 'GBPUSD', 'USDJPY']
  Triage model  : Azure gpt-5.2 (Haiku fallback)
  Analysis model: claude-sonnet-4-6
  Scan interval : 30 min
  Fast watch    : 2 min (RSS)
  Market hours  : 7:00 – 23:00 UTC
  Daily collect : 22:00 UTC
  Job 2 interval: 30 min
  Notifications : ['print', 'slack']
============================================================
Daily collection scheduler running in background
Job 2 position monitor starting in background
Phase threshold watcher starting in background (interval: 60s)
Fast news watcher starting in background (interval: 2 min)
Slack bot starting in background
```

All four jobs start from this single command. Job 1 runs in the foreground; everything else runs as background threads. Press `Ctrl+C` to stop.

**Managing signals from the command line:**

```bash
# List pending, approved, and recently executed signals
python -m agents.job3_executor --list

# Approve and execute a signal
python -m agents.job3_executor --approve A1B2C3D4

# Approve a Trim signal and close 25% (default is 50%)
python -m agents.job3_executor --approve A1B2C3D4 --pct 25

# Reject a signal
python -m agents.job3_executor --reject A1B2C3D4
```

---

## Slack commands

Send commands as a DM to the bot, or @mention it in your alerts channel.

### Scanning and analysis

| Command | What it does |
|---|---|
| `scan` | Refresh economic calendar, scan news for **all** active pairs, triage with GPT-5.2 (two-pass), escalate high-score articles to Sonnet |
| `scan GBPUSD` | Same but for one pair only |
| `analyze` | Full analysis for the primary pair (EURUSD) — builds context, runs Sonnet, returns signal with order details |
| `analyze USDJPY` | Full analysis for a specific pair |
| `positions` | Run Job 2 position check right now across all pairs |
| `collect` | Trigger end-of-day data collection immediately |

### Signal management

Signal alerts include **Execute** and **Reject** buttons — click to act without typing. Text commands still work as a fallback.

| Command | What it does |
|---|---|
| `pending` | List all signals waiting for approval |
| `approve <ID>` | Approve and execute a signal as a market order |
| `approve <ID> limit <price>` | Approve a Long/Short signal as a pending limit order at the given price |
| `approve <ID> <pct>` | Approve a Trim signal and close `<pct>`% of the position (e.g. `approve A1B2C3D4 25`) |
| `reject <ID>` | Reject a signal — no trade placed |

### Chat sessions

| Command | What it does |
|---|---|
| `chat` | Start an analysis-seeded chat session for the primary pair |
| `chat GBPUSD` | Start a chat session seeded with GBP/USD analysis |
| `chat USDJPY` | Start a chat session seeded with USD/JPY analysis |
| `end chat` | Close the current chat session |

Inside a chat session Claude has live tool access: positions, account info, news, market context, and the ability to propose orders.

### Ensemble debate

| Command | What it does |
|---|---|
| `debate positions` | Run Hold/Trim/Exit ensemble debate on all open positions |
| `debate positions GBPUSD` | Debate a specific pair's open position(s) |

### Trade journal

| Command | What it does |
|---|---|
| `journal` | Last 10 journal entries with outcome and P&L |
| `journal report` | Performance summary: win rate, total pips, P&L by category and pair |
| `journal validate` | Force MT5 validation of pending journal records right now |

### Debate calibration

| Command | What it does |
|---|---|
| `outcomes` | Debate accuracy summary: overrides to Wait, false negatives / false positives at 24h |

### System

| Command | What it does |
|---|---|
| `status` | Per-pair cooldown timers, MT5 connection state, pending signal count |
| `help` | Full command reference |
| `hi` / `hello` / `introduce` | Bot explains itself |
| _Any other message_ | Natural language → Haiku classifies intent → routes to handler or falls back to chat |

**Natural language works too.** "Scan the news for pound dollar", "check my positions", "any signals waiting?" are all understood without exact syntax.

---

## Approval workflow — a full example

### 09:15 UTC — Job 1 detects a high-impact event

The fast watcher picks up: "UK CPI beats forecast at 3.2%, BOE rate cut bets fade" from FXStreet RSS (published 3 minutes ago). Azure GPT-5.2 scores it 8/10 for GBP/USD and tags it `macro_data`. Full analysis triggers immediately — no wait for the 30-minute scan cycle.

Claude Sonnet builds the GBP/USD context — today's news, economic events including the UK CPI actual vs forecast, current market regime (VIX, SMA stack, ATR, macro bias showing GBP yield advantage narrowing), live M15 bars, 30-day price trend, FTSE and EURGBP correlations — and returns a Long signal.

### 09:15 UTC — Slack alert

```
📈 GBP/USD Long 🟡 Medium — 1-3 days
2026-03-20 09:15 UTC
Trigger [8]: UK CPI beats forecast at 3.2%, BOE rate cut bets fade (macro_data)

Price: 1.2743  Open 1.2698  H 1.2748  L 1.2691  +0.31%
Trend: Bullish — above SMA20/50, positive momentum on M15
Today: UK CPI surprise beat pushed GBP/USD 60 pips higher.
This week: GBP/USD up ~100 pips; BOE hold last Wednesday set the bullish tone.

Rationale: UK CPI (3.2% vs 2.9%) reduces BOE cut probability. Soft US
retail sales adds dollar weakness tailwind. Structure breaking above 1.2740.
Key Levels: Support 1.2700 | Resistance 1.2800
Invalidation: Close below 1.2680 (SMA20)
Risk Note: Fed speakers at 15:00 UTC could reverse USD weakness

Order Details (live)
Entry 1.2743  SL 1.2680 (-63p)  TP 1.2820 (+77p)
Lot 0.12  Risk $100  R:R 1:1.2  ⚠️ R:R below 1.5

Ensemble Debate — Confirmed Long (uncertainty 42/100)
📈 Bull 68 | 📉 Bear 44 | Winner: Bull
🟡 Uncertainty 42/100 — Bull 68 vs Bear 44; Sonnet agrees
🟡 Top risk [Medium]: Fed speakers at 15:00 UTC could reverse USD weakness

[ 📈 Execute Long ]  [ ✕ Reject ]
✏️ To modify SL/TP/lot before executing: type `chat` and ask me
```

### 09:17 UTC — You click Execute Long

The button replaces itself instantly:

```
⏳ Executing signal A3F9BC12… (approved by @zhengwu)
```

### 09:17 UTC — Job 3 executes

```
✅ Executed by @zhengwu — Signal A3F9BC12 | Ticket 12345678 | Fill 1.27431 | Volume 0.12 lots
```

Behind the scenes Job 3: fetched the live ask price, calculated SL/TP from the signal's key levels, computed lot size (1% of equity ÷ (63 pips × $10/pip) = 0.12 lots), and sent a BUY market order to MT5.

### 11:00 UTC — Job 2 checks the position

GBP/USD is at 1.2790, trade is up $56. Claude recommends Hold — position on track, no approval needed. Informational message posted to Slack.

### What if the recommendation were Exit?

```
✂️ Position Monitor — #12345678 BUY 0.12L @ 1.27431 | P&L: -$38
GBP/USD Exit [Medium]
Rationale: Price broke below 1.2680 invalidation on hourly close.

✅ approve 9D2E4B11    ❌ reject 9D2E4B11
```

Type `approve 9D2E4B11` to close the full position, or `approve 9D2E4B11 50` to close only half.

### Using chat to propose a trade

```
chat GBPUSD
```

The bot opens with the full GBP/USD analysis pre-loaded. You can then ask:

```
The setup looks clean. Can you propose a long with SL at 1.2650?
```

Claude fetches the live price, calculates the risk, and proposes a card with buttons:

```
📝 Order proposed — Signal B7C3D912 (Long)
To modify SL/TP/lot: ask me in this thread before executing.

[ 📈 Execute Long ]  [ ✕ Cancel ]
```

---

## File structure

```
eurusd_agent/
├── config.py                  # All tunable parameters — pairs, models, risk, timing, rate cycles
├── requirements.txt
├── verify_imports.py
│
├── agents/
│   ├── job1_opportunity.py    # Entry point — starts all four jobs
│   ├── job2_position.py       # Position monitor (all active pairs)
│   ├── job3_executor.py       # Trade execution engine + CLI
│   ├── job4_chat.py           # Conversational assistant (pair-aware, tool calling)
│   └── slack_bot.py           # Slack bot — command dispatcher + NLP fallback
│
├── triage/
│   ├── scanner.py             # 30-min scan loop — refreshes events, scans all pairs,
│   │                          # triggers CB policy update on CB-tagged headlines
│   ├── triage_prompt.py       # Two-pass triage: Azure GPT-5.2 (pass 1+2), Haiku fallback
│   ├── intraday_logger.py     # Logs today's scored headlines to disk
│   └── cooldown.py            # Per-pair cooldown: data/.cooldown_EURUSD, etc.
│
├── analysis/
│   ├── context_builder.py     # Assembles context window (news + events + regime + prices)
│   ├── full_analysis_prompt.py # Claude Sonnet prompt with extended thinking
│   ├── signal_formatter.py    # Validates and normalises signal output
│   ├── ensemble_agent.py      # Multi-agent debate: Bull/Bear/Risk Assessor + Judge (signal & position)
│   ├── outcome_tracker.py     # Records pre/post-debate signals; fetches MT5 prices at 8/16/24/48h
│   └── trade_journal.py       # Signal recorder, MT5 validator, hypothetical bar replay
│
├── pipeline/
│   ├── signal_store.py        # Pending signal queue
│   ├── daily_collector.py     # End-of-day: prices, events, summaries (all pairs)
│   ├── fast_news_watcher.py   # 2-min RSS poll loop — immediate escalation on breaking news
│   ├── news_fetcher.py        # RSS / FMP / Finnhub / NewsAPI / AlphaVantage / EODHD
│   ├── price_fetcher.py       # yfinance closing prices
│   ├── price_agent.py         # Live price summary: tick, M15, daily, technicals,
│   │                          # yield spreads (ECB / BOE / MOF Japan), macro bias
│   ├── regime_agent.py        # Market regime engine: risk sentiment, technical trend,
│   │                          # volatility, macro bias with conflict detection
│   ├── cb_policy_updater.py   # Auto-fetches CB statements (Fed/ECB/BOE/BOJ),
│   │                          # extracts stance with Haiku, writes data/rate_cycles.json
│   ├── event_fetcher.py       # ForexFactory calendar (USD/EUR/GBP/JPY) + FRED
│   └── dedup_cache.py         # Per-pair dedup: data/.dedup_cache_EURUSD, etc.
│
├── mt5/
│   ├── connector.py           # MT5 connect / disconnect / health check
│   ├── position_reader.py     # Open positions, account info, live ticks, OHLC bars
│   ├── order_manager.py       # Open, close, and modify positions; _clamp_stops() enforces broker min stop distance
│   └── risk_manager.py        # Pair-aware lot sizing and SL/TP calculation
│
├── notifications/
│   └── notifier.py            # Dispatches signals and plain-text alerts to Slack,
│                              # email, and console
│
├── utils/
│   ├── logger.py              # Rotating file + console logging
│   ├── retry.py               # Exponential back-off for API calls
│   └── date_utils.py          # Market hours, UTC/EST helpers
│
└── data/                      # Created automatically (not committed to git)
    ├── pending_signals.json   # Live signal queue
    ├── rate_cycles.json       # Auto-updated CB policy overrides (stance, guidance, expected)
    ├── trade_journal.jsonl    # Append-only trade journal source of truth
    ├── trade_journal.csv      # Trade journal as CSV — open in Excel or pandas
    ├── outcome_log.json       # Debate calibration data: pre/post-debate signals + 8/16/24/48h price outcomes
    ├── .cooldown_EURUSD       # Main scanner per-pair cooldown timestamps
    ├── .cooldown_GBPUSD
    ├── .cooldown_USDJPY
    ├── .cooldown_fast_EURUSD  # Fast watcher per-pair cooldown timestamps
    ├── .cooldown_fast_GBPUSD
    ├── .cooldown_fast_USDJPY
    ├── .dedup_cache_EURUSD    # Per-pair article dedup caches
    ├── .dedup_cache_GBPUSD
    ├── .dedup_cache_USDJPY
    └── YYYY-MM-DD/            # One folder per trading day
        ├── intraday.jsonl     # Today's scored headlines (all pairs)
        ├── events.json        # Today's economic events with actuals (refreshed each scan)
        ├── prices.json        # End-of-day closing prices
        ├── news.jsonl         # Merged daily news archive
        └── summary.md         # LLM-generated end-of-day summary (all active pairs)
```

---

## Configuration reference

All tunable parameters are in `config.py`. Edit it and restart the agent for changes to take effect.

### Active pairs

```python
ACTIVE_PAIRS: list[str] = ["EURUSD", "GBPUSD", "USDJPY"]
```

To add AUD/USD: add `"AUDUSD"` to this list. Its full configuration (pip value, correlated assets, news queries) is already defined in the `PAIRS` dict and `_PAIR_NEWS` dict in `news_fetcher.py`.

To remove a pair: remove it from `ACTIVE_PAIRS`. Everything else adapts automatically.

### Key parameters

| Parameter | Default | What it controls |
|---|---|---|
| `SCAN_INTERVAL_MINUTES` | 30 | How often the main scanner runs |
| `FAST_WATCH_INTERVAL_MINUTES` | 2 | How often the fast watcher polls RSS feeds |
| `FAST_COOLDOWN_MINUTES` | 20 | Per-pair cooldown for the fast watcher (shorter than main) |
| `TRIAGE_SCORE_THRESHOLD` | 6 | Minimum score (1–10) to trigger full analysis |
| `COOLDOWN_MINUTES` | 45 | Per-pair silence period after a trigger fires |
| `NEWS_MAX_AGE_HOURS` | 8 | Drop articles older than this many hours |
| `JOB2_CHECK_INTERVAL_MINUTES` | 30 | How often Job 2 main loop checks open positions |
| `JOB2_PHASE_WATCH_INTERVAL_SEC` | 60 | How often the phase watcher polls MT5 ticks for threshold crossings |
| `JOB2_PHASE_WATCH_COOLDOWN_SEC` | 300 | Min seconds between watcher-triggered analyses per position |
| `JOB3_RISK_PCT` | 2.0 | Base percentage of equity risked per trade (scaled down by uncertainty) |
| `JOB3_MAX_LOT` | 5.0 | Maximum lot size cap |
| `JOB3_UNCERTAINTY_TIERS` | see config | Tiered risk multipliers: ≤30→100%, ≤55→75%, ≤75→50% |
| `JOB3_AUTO_APPROVE_MAX_UNCERTAINTY` | 30 | Auto-execute without approval when uncertainty ≤ this. Set to `None` to disable. |
| `JOB3_MIN_RR` | 1.0 | Minimum R:R (TP÷SL) to execute. Signals below this are blocked at execution and filtered in the analysis prompt. Raise to 1.2–1.5 once win-rate data is available. |
| `JOB3_SIGNAL_EXPIRY_MINUTES` | 60 | How long a signal stays valid before auto-expiring |
| `MARKET_HOURS_START` / `END` | 7 / 23 | Active scanning window (UTC hour) |
| `DAILY_COLLECTION_TIME_UTC` | `"22:00"` | When end-of-day collection runs |
| `TRIAGE_MODEL` | `claude-haiku-4-5-20251001` | Fallback triage model (active triage uses Azure GPT-5.2) |
| `ANALYSIS_MODEL` | `claude-sonnet-4-6` | Full analysis, position monitor, chat |
| `USE_REGIME_ANALYSIS` | `True` | Inject regime summary into the LLM context window |
| `USE_MULTI_AGENT_DEBATE` | `True` | Run ensemble debate on every Long/Short signal |
| `DEBATE_MAX_UNCERTAINTY` | `75` | Signals with uncertainty score above this are downgraded to Wait |
| `DEBATE_OUTCOME_TRACKING` | `True` | Record pre/post-debate signals + MT5 price at 8/16/24/48h for calibration |
| `DEBATE_PERSONA_MODEL` | `claude-haiku-4-5-20251001` | Fallback persona model (active: Azure GPT-5.2) |
| `DEBATE_JUDGE_MODEL` | `claude-sonnet-4-6` | Model for the Judge synthesis step |

### Central bank rate cycles

`RATE_CYCLES` in `config.py` sets the baseline policy stance for each central bank. Update it manually after major meetings. The agent also auto-updates `data/rate_cycles.json` from live official statements — that file takes precedence over the config baseline.

```python
RATE_CYCLES: dict[str, dict] = {
    "Fed": {
        "stance":   "Pausing",          # "Hiking" | "Pausing" | "Cutting"
        "guidance": "No rush to cut - watching inflation; tariff uncertainty clouds outlook",
        "expected": "1-2 cuts in 2025 (CME FedWatch ~55%)",
        "updated":  "2026-03-25",       # update this date when you edit the entry
    },
    "ECB": { ... },
    "BOE": { ... },
    "BOJ": { ... },
}
```

The agent warns `[!] STALE (Xd old)` in the regime output if an entry has not been updated in more than 14 days.

---

## Troubleshooting

**"MT5 initialize() failed" on startup**

MetaTrader 5 must be open and fully loaded with a live broker connection. The agent connects to the already-running terminal — it does not launch MT5 itself.

**"SLACK_APP_TOKEN not set — Slack bot will not start"**

Check that both `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` are in `.env` with non-empty values, and that you ran `conda activate mt5_env` before starting the agent.

**Signal alerts appear in Slack but approve/reject commands do nothing**

The webhook is working but Socket Mode is not. Confirm the startup log shows "Slack bot connected". Check that both Slack tokens are set in `.env`.

**No Slack messages appear at all**

Check `SLACK_WEBHOOK_URL` in `.env`. Confirm the bot has been invited to the channel with `/invite @Forex Agent`.

**"Symbol GBPUSD not found in MT5"**

The symbol is not in your MT5 Market Watch. In MT5: View → Market Watch → right-click → Show All, or search for the symbol and add it. Repeat for each active pair.

**"ModuleNotFoundError: No module named 'MetaTrader5'"**

```bash
conda activate mt5_env
pip install MetaTrader5
```

**"ModuleNotFoundError: No module named 'slack_bolt'"**

```bash
conda activate mt5_env
pip install slack-bolt
```

**"ModuleNotFoundError: No module named 'pdfminer'"**

```bash
conda activate mt5_env
pip install pdfminer.six
```

This is required for Bank of Japan PDF statement extraction.

**CB policy update shows "statement fetch failed" in the logs**

The central bank website may be temporarily unavailable or have changed its URL structure. The agent falls back to the last known values in `data/rate_cycles.json` (or `config.RATE_CYCLES` if the file is absent). This is non-critical — the regime engine continues to function with cached values.

**Regime shows `[!] STALE` for a central bank**

Open `config.py`, update the relevant `RATE_CYCLES` entry with the latest stance, guidance, and expected moves, and update the `"updated"` date. The auto-updater may have failed to fetch the official statement; you can also manually trigger a fetch by restarting the agent after a CB headline appears in the news.

**Job 2 is not checking positions**

Job 2 only runs during market hours (07:00–23:00 UTC on weekdays). Force an immediate check with the `positions` Slack command.

**Signals expire before you can approve them**

Increase `JOB3_SIGNAL_EXPIRY_MINUTES` in `config.py`. Be aware that a very old signal may propose entry at prices that no longer reflect current conditions.

**One pair keeps triggering repeatedly on the same news**

Increase `COOLDOWN_MINUTES` in `config.py`. Cooldowns are per-pair, so changing this affects all pairs equally.

**You want to monitor only EUR/USD (original single-pair mode)**

Set `ACTIVE_PAIRS = ["EURUSD"]` in `config.py`. The system falls back to single-pair behaviour automatically.
