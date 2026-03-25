# Forex Multi-Pair Agent

An automated trading assistant that monitors multiple currency pairs, watches open positions, and executes trades on MetaTrader 5 — all controlled through Slack.

**Active pairs (default):** EUR/USD · GBP/USD · USD/JPY

Adding or removing pairs requires one line change in `config.py`.

---

## Table of Contents

1. [What is this?](#what-is-this)
2. [How it works — the 4 jobs](#how-it-works--the-4-jobs)
3. [Requirements](#requirements)
4. [Installation and setup](#installation-and-setup)
   - [1. Download the code](#1-download-the-code)
   - [2. Create the Conda environment](#2-create-the-conda-environment)
   - [3. Create the .env file](#3-create-the-env-file)
   - [4. Set up a Slack app](#4-set-up-a-slack-app)
5. [Running the agent](#running-the-agent)
6. [Slack commands](#slack-commands)
7. [Approval workflow — a full example](#approval-workflow--a-full-example)
8. [File structure](#file-structure)
9. [Configuration reference](#configuration-reference)
10. [Troubleshooting](#troubleshooting)

---

## What is this?

A Python-based AI agent that runs 24/5 (while the forex market is open) and does three things automatically:

1. **Reads the news.** Every 30 minutes it scans financial news for each active pair independently, scores headlines 1–10 for relevance to that specific pair, and — when something important is detected — runs a deep analysis using Claude Sonnet to generate a trading signal (Long, Short, or Wait). Each pair has its own news queries, cooldown timer, and deduplication cache so they never interfere with each other.

2. **Watches your open positions.** Every 30 minutes it reads all open MT5 positions across all active pairs and uses Claude Sonnet to recommend whether to Hold, Trim, or Exit each one based on current market conditions.

3. **Executes trades.** When you approve a signal (via Slack or the command line), the agent places or closes the order on MT5 directly. Position sizing is calculated automatically from your account equity and the stop loss distance — using the correct pip value for each pair (USD-quote pairs vs JPY pairs).

You stay in control. The agent never trades without your approval. Signals arrive in Slack, you type `approve XXXXXXXX`, and the trade executes. You can also reject signals, start a pair-specific chat session, and have a full conversation with the agent about market conditions — all from Slack.

**Market regime engine.** Before every full analysis the agent computes a four-dimensional market regime:

- **Risk Sentiment** — VIX level + pair realized volatility from M15 bars
- **Technical Trend** — daily SMA20/50/200 stack alignment + M15 bar momentum ratio
- **Volatility** — ATR-14 vs 30-day ATR baseline (are stops standard or need widening?)
- **Macro Bias** — live 10Y government bond yield spreads (fetched from ECB, BOE, and Japan MOF) combined with central bank rate cycle stances and forward guidance

Claude Sonnet sees this regime summary alongside the news and price data, and is instructed to explain how its trade direction and stop loss width align with (or conflict with) the current regime.

**Automatic central bank policy tracking.** Whenever the news scanner detects a central bank headline (Fed, ECB, BOE, or BOJ — any speech, press conference, or rate decision), the agent automatically fetches the latest official statement from the bank's website, uses Claude Haiku to extract the current stance and forward guidance, and writes the result to `data/rate_cycles.json`. The regime engine picks up the update immediately on the next analysis run. A Slack notification is sent, with `[STANCE CHANGED]` flagged when the stance differs from the previous reading.

---

## How it works — the 4 jobs

The system is organised into four jobs. All four start automatically when you run the agent.

### Job 1 — Opportunity Scanner (runs every 30 minutes)

Job 1 is the core loop. Every 30 minutes during market hours it:

1. **Refreshes the economic calendar** — fetches today's ForexFactory events (USD, EUR, GBP, JPY) with the latest actual vs forecast data. This happens once per cycle so Claude always has up-to-date beat/miss information during the trading day.

2. **Scans news for each active pair** — for EURUSD, GBPUSD, and USDJPY independently:
   - Fetches headlines from NewsAPI (pair-specific API query), AlphaVantage (broad macro + keyword filter), and EODHD (pair-specific ticker feed)
   - Filters new articles through a per-pair deduplication cache
   - Sends headlines to Claude Haiku for fast triage scoring (1–10) and tagging in the context of that specific pair
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

### Job 2 — Position Monitor (runs every 30 minutes)

Job 2 runs as a background thread alongside Job 1. It reads all open positions across all active pairs and for each position calls Claude Sonnet with:
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

For new positions (Long/Short), Job 3 calculates lot size automatically: it risks exactly `JOB3_RISK_PCT` (default 1%) of your current account equity, using the SL distance in pips. Pip values are pair-aware — USD-quote pairs (EUR/USD, GBP/USD) use $10/pip per standard lot; JPY pairs (USD/JPY) use the approximate JPY pip value configured in `config.py`.

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

## Requirements

**Operating system:** Windows only. The MetaTrader 5 Python package only runs on Windows.

**MetaTrader 5 terminal:** The MT5 desktop application must be running and logged into your broker account before you start the agent. All active pair symbols must be visible in the Market Watch.

**Python environment:** Anaconda or Miniconda. The instructions below use a Conda environment named `mt5_env`.

**External accounts required:**

| Service | Used for | Free tier? |
|---|---|---|
| Anthropic | Claude Haiku (triage + CB policy extraction) + Claude Sonnet (analysis, positions, chat) | No — pay per token |
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
  Triage model  : claude-haiku-4-5-20251001
  Analysis model: claude-sonnet-4-6
  Scan interval : 30 min
  Market hours  : 7:00 – 21:00 UTC
  Daily collect : 22:00 UTC
  Job 2 interval: 30 min
  Notifications : ['print', 'slack']
============================================================
Daily collection scheduler running in background
Job 2 position monitor starting in background
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
| `scan` | Refresh economic calendar, scan news for **all** active pairs, triage with Haiku, escalate high-score articles to Sonnet |
| `scan GBPUSD` | Same but for one pair only |
| `analyze` | Full analysis for the primary pair (EURUSD) — builds context, runs Sonnet, returns signal with order details |
| `analyze USDJPY` | Full analysis for a specific pair |
| `positions` | Run Job 2 position check right now across all pairs |
| `collect` | Trigger end-of-day data collection immediately |

### Signal management

| Command | What it does |
|---|---|
| `pending` | List all signals waiting for approval |
| `approve <ID>` | Approve and execute a signal on MT5 |
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

The scanner picks up: "UK CPI beats forecast at 3.2%, BOE rate cut bets fade". Claude Haiku scores it 8/10 for GBP/USD and tags it `macro_data`. Full analysis triggers.

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
Lot 0.12  Risk $100  R:R 1:1.2

✅ approve A3F9BC12    ✏️ chat    ❌ reject A3F9BC12
```

### 09:17 UTC — You approve

```
approve A3F9BC12
```

### 09:17 UTC — Job 3 executes

```
✅ Signal A3F9BC12 executed!
Ticket:     12345678
Fill price: 1.27431
Volume:     0.12 lots
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

Claude fetches the live price, calculates the risk, and proposes:

```
📝 Order proposed — Signal ID: B7C3D912
✅ approve B7C3D912    ❌ reject B7C3D912
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
│   ├── triage_prompt.py       # Claude Haiku prompt — scores, tags, identifies cb_bank
│   ├── intraday_logger.py     # Logs today's scored headlines to disk
│   └── cooldown.py            # Per-pair cooldown: data/.cooldown_EURUSD, etc.
│
├── analysis/
│   ├── context_builder.py     # Assembles context window (news + events + regime + prices)
│   ├── full_analysis_prompt.py # Claude Sonnet prompt with extended thinking
│   └── signal_formatter.py    # Validates and normalises signal output
│
├── pipeline/
│   ├── signal_store.py        # Pending signal queue
│   ├── daily_collector.py     # End-of-day: prices, events, summaries (all pairs)
│   ├── news_fetcher.py        # NewsAPI / AlphaVantage / EODHD (pair-specific queries)
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
│   ├── order_manager.py       # Open, close, and modify positions
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
    ├── .cooldown_EURUSD       # Per-pair cooldown timestamps
    ├── .cooldown_GBPUSD
    ├── .cooldown_USDJPY
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
| `SCAN_INTERVAL_MINUTES` | 30 | How often Job 1 scans news |
| `TRIAGE_SCORE_THRESHOLD` | 6 | Minimum score (1–10) to trigger full analysis |
| `COOLDOWN_MINUTES` | 45 | Per-pair silence period after a trigger fires |
| `JOB2_CHECK_INTERVAL_MINUTES` | 30 | How often Job 2 checks open positions |
| `JOB3_RISK_PCT` | 1.0 | Percentage of equity risked per trade |
| `JOB3_SIGNAL_EXPIRY_MINUTES` | 60 | How long a signal stays valid before auto-expiring |
| `MARKET_HOURS_START` / `END` | 7 / 23 | Active scanning window (UTC hour) |
| `DAILY_COLLECTION_TIME_UTC` | `"22:00"` | When end-of-day collection runs |
| `TRIAGE_MODEL` | `claude-haiku-4-5-20251001` | Fast model for headline scoring and CB extraction |
| `ANALYSIS_MODEL` | `claude-sonnet-4-6` | Full analysis, position monitor, chat |
| `USE_REGIME_ANALYSIS` | `True` | Inject regime summary into the LLM context window |

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
